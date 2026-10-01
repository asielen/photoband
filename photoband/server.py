"""FastAPI backend bound to 127.0.0.1 with a per-launch token."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse,
                               Response)
from starlette.middleware.base import BaseHTTPMiddleware

from captiontokens import TOKENS, resolve, validate

from . import __version__, batch as batchmod, decodegate, dialogs, drafts, fonts, paths, photos, security, templates
from .composite import Tile
from .imageio import ImageError, SUPPORTED_EXT
from .save import SaveRequest, read_log, save
from .security import UserError
from .settings import clean as clean_settings, load_settings, reset_settings, save_settings

log = logging.getLogger(__name__)

APP_MODE = {"mode": "browser"}  # "desktop" when launched in a pywebview window

_maintenance = {"started": False}
# filmstrip thumbnails that need a full decode (see /api/photo/proxy)
_thumb_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="thumbs")
_maintenance_lock = threading.Lock()


def _startup_maintenance() -> None:
    """Once per launch, in the background: prune old drafts and delete temp files that a killed
    save or batch left next to recently used photos (folders from the save log, last folder)."""
    from .util import sweep_temp
    try:
        drafts.prune_drafts()
    except Exception:
        log.debug("draft pruning failed", exc_info=True)
    try:
        folders = set()
        for e in read_log(300):
            for k in ("source", "output", "backup"):
                if e.get(k):
                    folders.add(os.path.dirname(e[k]))
        s = load_settings()
        last = (s.get("session") or {}).get("lastFolder")
        if last:
            folders.add(last)
        bf = (s.get("saving") or {}).get("backupFolder")
        if bf:
            folders.add(bf)
        sweep_temp(sorted(folders))
    except Exception:
        log.debug("startup temp sweep failed", exc_info=True)
    try:
        # source links/copies for ExifTool (metawrite._src_arg) left by a killed save
        import tempfile

        from .util import sweep_app_tmp
        sweep_app_tmp([paths.sub("tmp"), tempfile.gettempdir()])
    except Exception:
        log.debug("app temp sweep failed", exc_info=True)


def _start_maintenance_once() -> None:
    with _maintenance_lock:
        if _maintenance["started"]:
            return
        _maintenance["started"] = True
    threading.Thread(target=_startup_maintenance, daemon=True, name="pb-maintenance").start()


def _browse_roots_offered(last: Optional[str] = None) -> List[str]:
    """The shortcuts the in-app browser shows: home (not on Windows), drives and mounted volumes,
    and the drive, share or mount that holds the folder used last time. Only places
    /api/fs/list will actually open (both as named and resolved: on macOS
    "/Volumes/Macintosh HD" is a link to "/", which is never browsable)."""
    cands = [] if sys.platform == "win32" else [os.path.expanduser("~")]
    cands += security.mounted_volumes()
    lr = security.last_folder_roots(last)
    if lr:
        cands.append(lr[-1])
    out: List[str] = []
    seen = set()
    for r in cands:
        try:
            unc = security.is_unc(r)
            ok = security.may_browse(r, last) and (unc or security.may_browse(os.path.realpath(r), last))
        except (OSError, ValueError):
            ok = False
        key = os.path.normcase(os.path.normpath(r))
        if ok and key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _sweep_async(folders: List[str]) -> None:
    from .util import sweep_temp

    def run():
        try:
            sweep_temp(folders)
        except Exception:
            log.debug("temp sweep failed", exc_info=True)
    threading.Thread(target=run, daemon=True).start()


# GET routes that may take the token as ?t= (images, fonts, downloads); everything else needs the header.
_QUERY_TOKEN_RE = re.compile(
    r"^/(api/photo/(proxy|crop)|fonts/file/[^/]+|files/.*|api/templates/[^/]+/export|api/batch/[^/]+/report\.csv)$")

_HOST_RE = re.compile(r"^(127\.0\.0\.1|localhost)(:\d{1,5})?$")

CSP = ("default-src 'self'; img-src 'self' blob: data:; "
       "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com data: blob:; connect-src 'self'; "
       "frame-ancestors 'none'; base-uri 'none'")


def _secure(resp):
    """Security headers on every response."""
    resp.headers["Content-Security-Policy"] = CSP
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


_SETTINGS_SECTIONS = ("general", "saving", "advanced", "batch", "session")


def _sanitize_settings_patch(patch: Any) -> Dict[str, Any]:
    """Settings from the UI may not widen file access or choose programs to run:
    advanced.exiftoolPath is set only through a native dialog (/api/dialog kind=exiftool),
    session.lastFolder only by /api/open, and saving.fixedFolder / backupFolder only to
    folders the user already chose (a dialog or an in-app browser pick)."""
    if not isinstance(patch, dict):
        raise UserError("Settings must be an object")
    out: Dict[str, Any] = {}
    for sec, val in patch.items():
        if sec in _SETTINGS_SECTIONS and isinstance(val, dict):
            out[sec] = dict(val)
    # every value must have its setting's type and an allowed value (a wrong type stored once
    # would break every later launch); saving.subfolderName must be a plain folder name
    bad: List[str] = []
    out = clean_settings(out, bad=bad)
    if bad:
        raise UserError("Invalid setting: " + ", ".join(sorted(bad)[:10]))
    out.get("advanced", {}).pop("exiftoolPath", None)
    out.get("session", {}).pop("lastFolder", None)
    sav = out.get("saving", {})
    for k in ("fixedFolder", "backupFolder"):
        if k not in sav:
            continue
        v = sav[k]
        if v == "":
            continue
        if isinstance(v, str) and not security.is_unc(v) and security.is_allowed_root(v):
            sav[k] = os.path.realpath(v)
        else:
            sav.pop(k)
            if k == "fixedFolder" and sav.get("location") == "fixed" and not load_settings()["saving"].get(k):
                sav.pop("location", None)
    return out


def _restart_exiftool() -> None:
    try:
        from .exiftool import close_all
        close_all()
    except Exception:
        log.debug("could not stop ExifTool", exc_info=True)


# ValueErrors that these modules raise on purpose for bad input (the API answers 400 quietly);
# a ValueError from anywhere else is still a 400 but is logged with its traceback: it may be a bug.
_USER_VALUE_ERROR_MODULES = {"photoband.templates", "photoband.fonts", "photoband.security", "photoband.photos",
                             "photoband.settings", "photoband.dialogs", "captiontokens.tokens"}


def _expected_value_error(exc: BaseException) -> bool:
    tb = exc.__traceback__
    mod = ""
    while tb is not None:
        mod = tb.tb_frame.f_globals.get("__name__", "")
        tb = tb.tb_next
    return mod in _USER_VALUE_ERROR_MODULES


def _validated_existing(res: Dict[str, Any]) -> Dict[str, Any]:
    """The existing-text analysis as the UI may use it: a template that came from a file's record
    or marker payload (crafted files included) is validated like an imported one, else dropped."""
    st = res.get("state") if isinstance(res, dict) else None
    if not isinstance(st, dict):
        return res
    res = dict(res)
    st = dict(st)
    warn = list(res.get("warnings") or [])
    tpl = st.get("template")
    if tpl is not None:
        try:
            templates.validate_template(tpl)
        except Exception as e:
            st["template"] = None
            warn.append(f"The template stored in this file is not valid ({e}); the current template is used.")
    if st.get("overrides") is not None and not isinstance(st.get("overrides"), dict):
        st["overrides"] = None
    blocks = st.get("blocks")
    if blocks is not None and not (isinstance(blocks, list) and all(isinstance(b, dict) for b in blocks)):
        st["blocks"] = None
    res["state"] = st
    res["warnings"] = warn
    return res


def create_app() -> FastAPI:
    app = FastAPI(title="Photoband", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)

    class TokenMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            # Host first (DNS rebinding), then the token
            if not _HOST_RE.match(request.headers.get("host") or ""):
                return _secure(JSONResponse({"error": "Forbidden host"}, status_code=403))
            p = request.url.path
            if p.startswith("/api/") or p.startswith("/files/") or p.startswith("/fonts/"):
                tok = request.headers.get("x-photoband-token")
                if tok is None and request.method in ("GET", "HEAD") and _QUERY_TOKEN_RE.match(p):
                    # <img>, FontFace url() and plain downloads can't send headers
                    tok = request.query_params.get("t")
                if not security.token_ok(tok):
                    return _secure(JSONResponse({"error": "Forbidden"}, status_code=403))
            resp = await call_next(request)
            resp.headers["Cache-Control"] = resp.headers.get("Cache-Control", "no-store")
            return _secure(resp)

    app.add_middleware(TokenMiddleware)

    @app.exception_handler(PermissionError)
    async def _perm(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=403)

    @app.exception_handler(ImageError)
    async def _img(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=422)

    @app.exception_handler(UserError)
    async def _user(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(ValueError)
    async def _val(request, exc):
        if not _expected_value_error(exc):
            log.warning("ValueError in %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(batchmod.BatchNotFound)
    async def _nobatch(request, exc):
        return JSONResponse({"error": "This batch no longer exists."}, status_code=404)

    @app.exception_handler(FileNotFoundError)
    async def _nf(request, exc):
        return JSONResponse({"error": f"File not found: {exc.filename or exc}"}, status_code=404)

    # ------------------------------------------------------------------ static UI
    dist = paths.ui_dist()

    @app.get("/", response_class=HTMLResponse)
    def index(b: str = ""):
        if b:
            # the launcher opened /?b=<nonce> (the token never goes on the browser's command line):
            # trade the nonce, once, for the token in the fragment, which the UI reads and removes
            if security.redeem_handoff(b):
                from urllib.parse import quote
                return RedirectResponse(f"/#t={quote(security.TOKEN, safe='')}", status_code=303,
                                        headers={"Cache-Control": "no-store"})
            return HTMLResponse("<h1>Photoband</h1><p>This start link was already used or has expired. Use the "
                                "Photoband tab that is already open, or start Photoband again.</p>", status_code=410)
        p = os.path.join(dist, "index.html")
        if not os.path.exists(p):
            return HTMLResponse("<h1>Photoband</h1><p>The UI is not built. Run <code>npm run build</code> in ui/.</p>")
        return FileResponse(p, headers={"Cache-Control": "no-store"})

    @app.get("/assets/{name:path}")
    def assets(name: str):
        p = os.path.realpath(os.path.join(dist, "assets", name))
        if not p.startswith(os.path.realpath(os.path.join(dist, "assets"))) or not os.path.isfile(p):
            raise HTTPException(404)
        return FileResponse(p, headers={"Cache-Control": "public, max-age=31536000, immutable"})

    @app.get("/favicon.svg")
    def favicon():
        p = os.path.join(dist, "favicon.svg")
        if os.path.exists(p):
            return FileResponse(p)
        raise HTTPException(404)

    # ------------------------------------------------------------------ state
    @app.get("/api/state")
    def state():
        from . import ocr
        _start_maintenance_once()
        s = load_settings()
        security.allow_settings_folders(s)
        try:
            from .exiftool import get, warm_async
            et_ok = bool(get().cmd)
            et_err = ""
            if et_ok:
                warm_async()
        except Exception as e:
            et_ok, et_err = False, str(e)
        return {
            "version": __version__, "platform": sys.platform, "mode": APP_MODE["mode"],
            "settings": s, "ocrEngines": ocr.engines(), "exiftool": et_ok, "exiftoolError": et_err,
            "incompleteBatches": batchmod.incomplete_batches(),
            "notice": APP_MODE.get("notice", ""),
        }

    @app.post("/api/settings")
    def settings_patch(patch: Dict[str, Any] = Body(...)):
        s = save_settings(_sanitize_settings_patch(patch))
        security.allow_settings_folders(s)
        return s

    @app.post("/api/exiftool/use-bundled")
    def exiftool_use_bundled():
        """Forget a custom ExifTool (the path itself can only be set with /api/dialog kind=exiftool)."""
        s = save_settings({"advanced": {"exiftoolPath": ""}})
        _restart_exiftool()
        return s

    @app.post("/api/settings/reset")
    def settings_reset():
        return reset_settings()

    # ------------------------------------------------------------------ templates & tokens
    @app.get("/api/templates")
    def tpl_list():
        return templates.list_templates()

    @app.post("/api/templates")
    def tpl_save(body: Dict[str, Any] = Body(...)):
        return templates.save_template(body["template"], new=bool(body.get("new")))

    @app.delete("/api/templates/{tid}")
    def tpl_delete(tid: str):
        templates.delete_template(tid)
        return {"ok": True}

    @app.post("/api/templates/import")
    def tpl_import(body: Dict[str, Any] = Body(...)):
        return templates.import_template(body)

    @app.get("/api/templates/{tid}/export")
    def tpl_export(tid: str):
        try:
            t = templates.export_template(tid)
        except KeyError:
            raise HTTPException(404, "No such template")
        return Response(json.dumps(t, indent=2, ensure_ascii=False), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{tid}.photoband-template.json"'})

    @app.get("/api/tokens")
    def tokens():
        return [t.__dict__ for t in TOKENS]

    @app.post("/api/resolve")
    def api_resolve(body: Dict[str, Any] = Body(...)):
        fields = body.get("fields")
        if fields is None and body.get("path"):
            fields = photos.meta(security.check(body["path"]))["fields"]
        fields = dict(fields or {})
        fields.setdefault("template", body.get("templateName", ""))
        out = {}
        for key, fmt in (body.get("formats") or {}).items():
            out[key] = resolve(fmt or "", fields).to_dict()
        return out

    @app.post("/api/validate")
    def api_validate(body: Dict[str, Any] = Body(...)):
        return [i.__dict__ for i in validate(body.get("format", ""))]

    # ------------------------------------------------------------------ fonts
    @app.get("/api/fonts")
    def api_fonts(refresh: bool = False):
        idx = fonts.build_index() if refresh else fonts.index()
        return {"families": idx["families"], "fallback": fonts.FALLBACK_IDS, "default": fonts.DEFAULT_FAMILY}

    @app.get("/fonts/file/{fid}")
    def font_file(fid: str):
        p = fonts.file_path(fid)
        if not p or not os.path.isfile(p):
            raise HTTPException(404)
        mt = "font/otf" if p.lower().endswith(".otf") else "font/ttf"
        return FileResponse(p, media_type=mt, headers={"Cache-Control": "private, max-age=86400"})

    @app.post("/api/fonts/add")
    def font_add(body: Dict[str, Any] = Body(...)):
        p = security.check(body["path"])
        info = fonts.add_font_file(p)
        return {"ok": True, "family": info["family"]}

    @app.get("/api/fonts/google")
    def google_catalog():
        try:
            return fonts.google_catalog()
        except Exception as e:
            raise HTTPException(502, str(e))

    @app.post("/api/fonts/google/download")
    def google_download(body: Dict[str, Any] = Body(...)):
        try:
            return fonts.google_download(body["family"])
        except Exception as e:
            raise HTTPException(502, str(e))

    @app.get("/api/licenses")
    def licenses():
        out = []
        for m in json.load(open(os.path.join(paths.fonts_dir(), "manifest.json"), encoding="utf-8")):
            d = os.path.join(paths.fonts_dir(), m["id"])
            text = ""
            for fn in ("OFL.txt", "LICENSE.txt"):
                if os.path.exists(os.path.join(d, fn)):
                    text = open(os.path.join(d, fn), encoding="utf-8", errors="replace").read()
                    break
            out.append({"family": m["family"], "license": m["license"], "text": text})
        return out

    # ------------------------------------------------------------------ dialogs & files
    @app.post("/api/dialog")
    def dialog(body: Dict[str, Any] = Body(...)):
        kind = body.get("kind", "open-files")
        if kind not in dialogs.KINDS:
            raise HTTPException(400, "Unknown dialog kind")
        title = body.get("title", "")
        if kind == "exiftool":
            title = "Choose ExifTool"
        try:
            res = dialogs.ask(kind, title=title if isinstance(title, str) else "",
                              initial=body.get("initial", "") if isinstance(body.get("initial"), str) else "",
                              save_name=body.get("saveName", "") if isinstance(body.get("saveName"), str) else "")
        except dialogs.Unavailable:
            return {"unavailable": True}
        if kind == "exiftool":
            # the chosen program is stored here, server-side; settings PATCHes can't set it
            if not res:
                return {"paths": []}
            from .exiftool import ExifToolError, validate_exiftool
            p = os.path.abspath(res[0])
            try:
                validate_exiftool(p)
            except ExifToolError as e:
                raise HTTPException(400, str(e))
            s = save_settings({"advanced": {"exiftoolPath": p}})
            _restart_exiftool()
            return {"paths": [p], "settings": s}
        if res:
            if kind == "save-file":
                security.allow([res[0]], from_dialog=True)
                security.allow_root(os.path.dirname(res[0]), from_dialog=True)
            else:
                security.allow(res, from_dialog=True)
        return {"paths": res or []}

    @app.get("/api/fs/list")
    def fs_list(dir: str = ""):
        """The in-app browser, the fallback for browser mode when no system file dialog works.
        Each entry (and the folder itself) carries a one-time ``pick`` id; /api/fs/allow grants
        access only for those ids. It lists only the home folder, the temp folder, mounted drives
        and folders already opened (never system folders or hidden ones), and is off in the
        desktop window, which always has the system dialogs."""
        if APP_MODE["mode"] == "desktop" and dialogs._provider is not None:
            raise HTTPException(403, "Use the system file dialog to choose files.")
        d = dir or os.path.expanduser("~")
        if "\x00" in d:
            raise HTTPException(400, "Invalid path")
        last = (load_settings().get("session") or {}).get("lastFolder") or None
        if security.is_unc(d) and not security.unc_browsable(d, last):
            raise HTTPException(403, "Network locations can only be opened with the system file dialog.")
        shown = os.path.normpath(os.path.abspath(d))
        refuse = HTTPException(403, "Photoband can only browse your home folder and drives. Use Open with the "
                                    "system dialog for other places.")
        # check the path as given AND where it leads: a mapped drive (Z:\) resolves to its share
        # (\\server\share), a link in the home folder may lead to /etc
        if not security.may_browse(shown, last):
            raise refuse
        real = os.path.realpath(shown)
        if security.is_unc(real) and not security.is_unc(shown):
            # a mapped network drive: list it under its drive letter, as the user knows it
            d = shown
        else:
            d = real
        if not os.path.isdir(d):
            raise HTTPException(404, "Not a folder")
        if not security.may_browse(real, last):
            raise refuse
        entries = []
        try:
            names = os.listdir(d)
        except PermissionError:
            names = []
        for n in sorted(names, key=str.lower):
            if n.startswith("."):
                continue
            p = os.path.join(d, n)
            try:
                if os.path.isdir(p):
                    entries.append({"name": n, "path": p, "dir": True, "pick": security.new_pick(p, True)})
                elif os.path.splitext(n)[1].lower() in SUPPORTED_EXT | {".ttf", ".otf", ".json"}:
                    entries.append({"name": n, "path": p, "dir": False, "size": os.path.getsize(p),
                                    "pick": security.new_pick(p, False)})
            except OSError:
                continue
        roots = _browse_roots_offered(last)
        parent = os.path.dirname(d)
        parent = parent if parent != d and security.may_browse(parent, last) and security.may_browse(
            os.path.realpath(parent), last) else None
        return {"dir": d, "parent": parent,
                "entries": entries, "roots": roots, "home": os.path.expanduser("~"),
                "pick": security.new_pick(d, True)}

    @app.post("/api/fs/allow")
    def fs_allow(body: Dict[str, Any] = Body(...)):
        """Grant what the user picked in the in-app browser. Accepts only pick ids from
        /api/fs/list: ``picks`` (files or folders) and, for "save as", ``save`` =
        {folder: <pick id of a folder>, name: <plain file name>}. Raw ``paths`` are accepted
        only for the last folder the app opened (Reopen). Returns the granted paths."""
        granted: List[str] = []
        picks = body.get("picks") or []
        if not isinstance(picks, list):
            raise UserError("picks must be a list")
        for pid in picks:
            p, is_dir = security.redeem_pick(pid)
            # pick ids come only from listings /api/fs/list checked (a mapped network drive's
            # entries resolve to \\server\share paths, which need the dialog-level grant)
            if is_dir:
                security.allow_root(p, from_dialog=True)
            else:
                security.allow([p], from_dialog=True)
            granted.append(p)
        sv = body.get("save")
        if sv:
            if not isinstance(sv, dict):
                raise UserError("Invalid save choice")
            folder, is_dir = security.redeem_pick(sv.get("folder"))
            if not is_dir:
                raise PermissionError("Choose a folder to save into.")
            out = os.path.join(folder, security.plain_file_name(sv.get("name")))
            security.allow([out], from_dialog=True)
            security.allow_root(folder, from_dialog=True)
            granted.append(out)
        raw = body.get("paths") or []
        if raw:
            last = (load_settings().get("session") or {}).get("lastFolder") or ""
            for p in raw:
                if (isinstance(p, str) and last and security.is_unc(p) and security.is_unc(last)
                        and not security.is_device_path(p)
                        and os.path.normcase(os.path.normpath(p)) == os.path.normcase(os.path.normpath(last))
                        and os.path.isdir(p)):
                    # the NAS folder opened last time (only ever set from a path the user chose)
                    security.allow_root(p, from_dialog=True)
                    granted.append(os.path.normpath(p))
                elif (isinstance(p, str) and last and not security.is_unc(p) and os.path.isdir(p)
                        and os.path.normcase(os.path.realpath(p)) == os.path.normcase(os.path.realpath(last))):
                    security.allow_root(p, from_dialog=security.is_unc(os.path.realpath(p)))
                    granted.append(os.path.realpath(p))
                else:
                    raise PermissionError("Choose the file or folder in Photoband first.")
        return {"ok": True, "paths": granted}

    @app.post("/api/open")
    def open_photos(body: Dict[str, Any] = Body(...)):
        ps = [security.check(p) for p in body.get("paths", [])]
        items = photos.list_photos(ps, bool(body.get("includeSubfolders")))
        _sweep_async(sorted({p if os.path.isdir(p) else os.path.dirname(p) for p in ps}))
        if ps:
            save_settings({"session": {"lastFolder": ps[0] if os.path.isdir(ps[0]) else os.path.dirname(ps[0])}})
        return items

    @app.get("/api/photo/meta")
    def photo_meta(path: str):
        p = security.check(path)
        photos.set_current(p)
        m = photos.meta(p)
        m["draft"] = drafts.load_draft(p)
        return m

    @app.get("/api/photo/proxy")
    async def photo_proxy(path: str, thumb: bool = False):
        import anyio
        p = await anyio.to_thread.run_sync(security.check, path)
        if thumb:
            # filmstrip thumbnails decode (lowest priority) in their own small pool, so a
            # folder of big scans can't tie up the server's request threads
            import asyncio
            pp, tp, _ = await asyncio.wrap_future(_thumb_pool.submit(photos.proxy_paths, p, True))
        else:
            pp, tp, _ = await anyio.to_thread.run_sync(photos.proxy_paths, p)
        return FileResponse(tp if thumb else pp, media_type="image/webp",
                            headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/api/photo/prefetch")
    def photo_prefetch(body: Dict[str, Any] = Body(...)):
        ps = [p for p in body.get("paths", []) if security.is_allowed(p)]
        photos.prefetch(ps)
        return {"ok": True}

    @app.get("/api/photo/crop")
    def photo_crop(path: str, x: int, y: int, w: int, h: int, out: int = 1024):
        p = security.check(path)
        return Response(photos.crop_webp(p, x, y, w, h, out), media_type="image/webp",
                        headers={"Cache-Control": "private, max-age=600"})

    @app.post("/api/photo/erase-preview")
    async def photo_erase_preview(body: Dict[str, Any] = Body(...)):
        p = security.check(body["path"])
        import anyio
        try:
            data = await anyio.to_thread.run_sync(photos.erase_preview_webp, p, body.get("erase") or {})
        except ValueError as e:   # e.g. an oversized brush mask
            raise HTTPException(400, str(e))
        return Response(data, media_type="image/webp")

    @app.post("/api/photo/existing")
    def photo_existing(body: Dict[str, Any] = Body(...)):
        p = security.check(body["path"])
        return _validated_existing(photos.existing(p, run_ocr=bool(body.get("ocr", True))))

    # ------------------------------------------------------------------ drafts
    @app.post("/api/drafts")
    def draft_save(body: Dict[str, Any] = Body(...)):
        p = security.check(body["path"])
        if body.get("state") is None:
            drafts.delete_draft(p)
        else:
            state = dict(body["state"])
            state.pop("_hash", None)
            if body.get("hash"):
                # identifies the editor state, so a save only removes the draft it actually saved
                state["_hash"] = str(body["hash"])
            drafts.save_draft(p, state)
        return {"ok": True}

    @app.get("/api/drafts")
    def draft_list():
        """All drafts: {path, updated, exists, valid}. valid=False means the file changed since the
        draft was made (or is gone); the draft is kept so the UI can still offer it."""
        return drafts.list_drafts()

    @app.get("/api/drafts/get")
    def draft_get(path: str):
        """A draft even when it no longer matches the file ("use this draft anyway")."""
        p = security.check(path)
        valid = drafts.load_draft(p) is not None
        return {"path": p, "valid": valid, "state": drafts.load_draft_any(p)}

    # ------------------------------------------------------------------ saving
    def _tiles_from_form(form, job) -> List[Tile]:
        from .composite import TILE_MAX, upload_size
        tiles = []
        for t in job.get("tiles", []):
            f = form.get(t["name"])
            if f is None:
                raise HTTPException(400, f"Missing tile {t['name']}")
            # the UI renders text tiles of at most TILE_MAX px a side: refuse anything bigger
            # before it is decoded (a tiny PNG can declare a huge image)
            try:
                w, h = upload_size(f)
            except ValueError as e:
                raise HTTPException(400, f"Tile {t['name']}: {e}")
            if w > TILE_MAX or h > TILE_MAX:
                raise HTTPException(400, f"Tile {t['name']} is {w}×{h} px; at most {TILE_MAX} px a side")
            tiles.append(Tile(int(t["x"]), int(t["y"]), f))
        return tiles

    async def _read_form(request: Request):
        form = await request.form()
        job = json.loads(form["job"])
        files = {}
        for k, v in form.multi_items():
            if k != "job" and hasattr(v, "read"):
                files[k] = await v.read()
        return job, files

    def _allowed_for_write(job, settings):
        src = security.check(job["path"])
        if job.get("mode") == "copyAs":
            security.check(job.get("dest_path") or "")
        return src

    @app.post("/api/save/preview")
    def api_save_preview(body: Dict[str, Any] = Body(...)):
        from .save import save_preview
        src = security.check(body.get("path") or "")
        try:
            return save_preview(src, load_settings().get("saving", {}), body.get("fields"), body.get("templateName") or "")
        except (OSError, ImageError) as e:
            raise HTTPException(404, f"Cannot read {src}: {e}")

    @app.post("/api/save")
    async def api_save(request: Request):
        job, files = await _read_form(request)
        settings = load_settings()
        src = _allowed_for_write(job, settings)
        req = SaveRequest(path=job["path"], mode=job.get("mode", "copy"), layout=job["layout"],
                          tiles=_tiles_from_form(files, job), state=job.get("state") or {}, settings=settings,
                          dest_path=job.get("dest_path"), erase=job.get("erase"),
                          original_text=job.get("original_text"), case=job.get("case"),
                          embed_marker=job.get("embed_marker"), fields=job.get("fields"),
                          template_name=job.get("template_name", ""), on_exists=job.get("on_exists"),
                          expected_stat=tuple(job["expected_stat"]) if job.get("expected_stat") else None,
                          expected_hash=job.get("expected_hash"))
        import anyio
        res = await anyio.to_thread.run_sync(save, req)
        if res.ok:
            # Edits typed while the save ran are autosaved as a newer draft: keep that one. The
            # draft is compared whatever the file's state (an overwrite changed its identity).
            want = job.get("draft_hash")
            if want:
                drafts.delete_draft_if(src, str(want), unhashed=True)
            else:
                drafts.delete_draft(src)
            security.allow([res.out_path])
        return res.to_json()

    @app.get("/api/log")
    def api_log():
        return read_log()

    @app.post("/api/open-folder")
    def open_folder(body: Dict[str, Any] = Body(...)):
        """Show a file or folder in the file manager. Never opens or launches it."""
        which = body.get("which", "logs")
        p = paths.sub("logs") if which == "logs" else security.check(body.get("path", ""))
        if not os.path.exists(p):
            raise HTTPException(404, "Not found")
        _reveal(p)
        return {"ok": True}

    # ------------------------------------------------------------------ batch
    _pre: Dict[str, Dict[str, Any]] = {}
    _pre_pool = ThreadPoolExecutor(max_workers=max(2, min(4, os.cpu_count() or 2)), thread_name_prefix="preflight")

    @app.post("/api/batch/scan")
    def batch_scan(body: Dict[str, Any] = Body(...)):
        folder = security.check(body["folder"])
        return batchmod.scan_folder(folder, bool(body.get("includeSubfolders")))

    @app.post("/api/batch/preflight")
    def batch_preflight(body: Dict[str, Any] = Body(...)):
        files = [security.check(p) for p in body.get("paths", [])]
        # results are big (metadata + analysis per photo): free finished ones when a new
        # pre-flight starts or after 30 min, and stop the one this replaces
        now = time.time()
        for k, old in list(_pre.items()):
            if k == body.get("replaces"):
                old["cancel"] = True
            # finished ones stay a minute so another window can still collect its last results
            # (a stopped one stays until the photos it was checking are done: its last results)
            if ((old["cancel"] and not old["active"]) or now - old.get("finished", now) > 60
                    or now - old.get("started", now) > 1800):
                old["cancel"] = True
                _pre.pop(k, None)
        pid = f"pf{int(now * 1000)}"
        # active: photos being checked right now. Stop means: start no new ones, let these finish.
        st = {"id": pid, "total": len(files), "results": {}, "cancel": False, "started": now, "active": set()}
        _pre[pid] = st
        want_ocr = bool(body.get("ocr", False))

        def one(p):
            # joined before the stop check: once stopped, "active" only shrinks
            st["active"].add(p)
            try:
                if not st["cancel"]:
                    check(p)
            finally:
                st["active"].discard(p)

        def check(p):
            try:
                m = photos.meta(p)
                r: Dict[str, Any] = {"path": p, "meta": m, "ok": True}
                info = m["info"]
                if info.get("save_blocked"):
                    r["blocked"] = info["save_blocked"]
                elif info.get("pages", 1) > 1 and not load_settings()["saving"].get("allowMultipageSave"):
                    r["blocked"] = "Multi-page TIFF"
                else:
                    # one decode (at prefetch priority) serves the analysis and the proxy
                    r["existing"] = _validated_existing(photos.existing(p, run_ocr=want_ocr, prio=decodegate.PREFETCH))
                    photos.proxy_paths(p, prio=decodegate.PREFETCH)
            except Exception as e:
                r = {"path": p, "ok": False, "error": str(e)}
            st["results"][p] = r

        for p in files:
            _pre_pool.submit(one, p)
        return {"id": pid, "total": len(files)}

    @app.get("/api/batch/preflight/{pid}")
    def batch_preflight_poll(pid: str, since: int = 0):
        st = _pre.get(pid)
        if not st:
            raise HTTPException(404)
        res = list(st["results"].values())
        # stopped: no photo is being checked any more and none will start; the rest stay unchecked
        stopped = bool(st["cancel"]) and not st["active"]
        if len(res) >= st["total"] or stopped:
            st.setdefault("finished", time.time())
        return {"id": pid, "total": st["total"], "done": len(res), "results": res[since:],
                "stopped": stopped, "active": len(st["active"])}

    @app.post("/api/batch/preflight/{pid}/cancel")
    def batch_preflight_cancel(pid: str):
        """Stop after the photos being checked now: they finish (and are reported), no new ones
        start. Poll until "stopped" to collect the last results."""
        if pid in _pre:
            _pre[pid]["cancel"] = True
        return {"ok": True}

    @app.post("/api/batch/create")
    def batch_create(body: Dict[str, Any] = Body(...)):
        """files: the photos to save (indices 0..n-1). plan: the whole pre-flight plan
        [{index?, path, name, status, action, reasons, usesDraft, edited}], kept in the batch so a
        resume can prepare unstaged photos again; photos not saved are journaled with reasons."""
        files = body.get("files", [])
        if not isinstance(files, list) or not all(isinstance(p, str) for p in files):
            raise UserError("files must be a list of paths")
        for p in files:
            security.check(p)
        plan = [x for x in (body.get("plan") or []) if isinstance(x, dict) and x.get("path")]
        for x in plan:
            # restore and resume act on these paths: only photos the user opened
            security.check(x["path"])
        want = set(files)
        unsaved = [x for x in plan if x.get("index") is None and x["path"] not in want]
        b = batchmod.new_batch({"count": len(files), "files": files, "plan": plan,
                                "settings": body.get("batchSettings", {}), "templateId": body.get("templateId"),
                                "folder": body.get("folder", "")}, unsaved=unsaved)
        return {"id": b.id}

    @app.post("/api/batch/{bid}/stage")
    async def batch_stage(bid: str, request: Request):
        job, files = await _read_form(request)
        settings = load_settings()
        src = security.check(job["path"])
        b = batchmod.get_batch(bid)
        # a batch writes only where the server decides: a copy at the destination derived from
        # Settings › Saving, or the photo itself. Never a client-chosen path or conflict answer.
        if job.get("mode") not in ("copy", "overwrite"):
            raise HTTPException(400, "A batch saves copies or overwrites the originals.")
        job.pop("dest_path", None)
        job.pop("on_exists", None)
        idx = job.get("index")
        if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < int(b.data.get("meta", {}).get("count", 0)):
            raise HTTPException(400, "Invalid batch index")
        from .util import canonical_path
        files_ = [canonical_path(p) for p in b.data.get("meta", {}).get("files") or [] if isinstance(p, str)]
        if canonical_path(src) not in files_:
            raise HTTPException(403, "That photo is not part of this batch.")
        notes: List[str] = []
        if job.get("mode") == "overwrite" and (job.get("layout") or {}).get("mode") == "erase":
            from .save import cached_case
            if job.get("case") == "C" or cached_case(src) == "C":
                # a physical caption is erased on copies only, whatever Settings › Saving allows
                job["mode"] = "copy"
                job["case"] = "C"
                notes.append("Physical caption: erased on a copy; the original was not changed")
        # the source as staged: the save refuses it if the content changed since (size+mtime+hash)
        from .util import quick_hash
        try:
            job["expected_hash"] = quick_hash(src)
        except OSError as e:
            raise HTTPException(404, f"Cannot read {src}: {e}")
        # resolve the copy destination now so a resumed run writes the same file
        if job.get("mode") == "copy":
            from .imageio import probe
            from .save import destination_for, SaveError, source_ids_for_file
            try:
                out, _fmt = destination_for(src, probe(src), settings["saving"], job.get("fields"),
                                            job.get("template_name", ""), "increment"
                                            if settings["saving"].get("onExists") == "ask" else None, notes=notes,
                                            src_ids=source_ids_for_file(src, job.get("layout")))
            except SaveError as e:
                raise HTTPException(409, str(e))
            # reserved names compare case-insensitively (Windows and macOS file systems)
            reserved = {(e.get("dest") or "").lower() for e in b.data.get("entries", {}).values()
                        if int(e.get("index", -1)) != int(job["index"])}
            if out.lower() in reserved:
                # count up from the template's own name (never "name-2-2")
                try:
                    plain, _f = destination_for(src, probe(src), settings["saving"], job.get("fields"),
                                                job.get("template_name", ""), "overwrite")
                except SaveError:
                    plain = out
                base, ext = os.path.splitext(plain)
                i = 2
                while f"{base}-{i}{ext}".lower() in reserved or os.path.exists(f"{base}-{i}{ext}"):
                    i += 1
                out = f"{base}-{i}{ext}"
            elif os.path.exists(out):
                # destination_for chose to replace an earlier copy of this same photo (onExists
                # "overwrite"); save() checks again and keeps anything else aside
                job["on_exists"] = "overwrite"
            job["mode"] = "copyAs"
            job["dest_path"] = out
            security.allow([out])
        job["settings"] = settings
        tiles = [{"x": t["x"], "y": t["y"], "png": files[t["name"]]} for t in job.get("tiles", [])]
        b.stage(int(job["index"]), {k: v for k, v in job.items() if k != "tiles"}, tiles)
        with b._lock:
            ent = b.data["entries"][str(int(job["index"]))]
            ent["dest"] = job.get("dest_path") or src
            if notes:
                ent["notes"] = notes
            b._write()
        return {"ok": True, "dest": job.get("dest_path") or src}

    @app.post("/api/batch/{bid}/exclude")
    def batch_exclude(bid: str, body: Dict[str, Any] = Body(...)):
        """One photo ({index, path, state, reason}) or several ({items: [...]}) that won't be
        saved: excluded, failed to prepare, cancelled before staging, not prepared."""
        b = batchmod.get_batch(bid)
        items = body.get("items") if isinstance(body.get("items"), list) else [body]
        meta = b.data.get("meta", {}) or {}
        files_ = meta.get("files") or []
        planned = b.planned_paths()
        from .util import canonical_path
        skipped = []
        for it in items:
            if not isinstance(it, dict):
                raise HTTPException(400, "Invalid item")
            st = it.get("state", "excluded")
            if st not in ("excluded", "failed", "cancelled", "notPrepared", "skipped", "held", "blocked"):
                raise HTTPException(400, "Invalid state")
            try:
                idx = int(it["index"])
            except (KeyError, TypeError, ValueError):
                raise HTTPException(400, "Invalid index")
            if not 0 <= idx < int(b.data.get("expected", 0) or 0):
                raise HTTPException(400, "Invalid index")
            with b._lock:
                cur = dict(b.data.get("entries", {}).get(str(idx)) or {})
            if cur and (cur.get("state") in ("done", "running", "restored") or cur.get("backup") or cur.get("out")):
                skipped.append(idx)   # a saved photo is never re-labelled (nor its path changed)
                continue
            # the path is the batch's own record of that photo, never the client's
            path = cur.get("path") or (files_[idx] if idx < len(files_) else "")
            if not path:
                p = it.get("path")
                path = p if isinstance(p, str) and p and canonical_path(p) in planned else ""
            reason = it.get("reason", "")
            b.mark(idx, st, path=path, error=reason if isinstance(reason, str) else "")
        return {"ok": True, "skipped": skipped}

    @app.post("/api/batch/{bid}/staging-complete")
    def batch_staging_complete(bid: str):
        batchmod.get_batch(bid).staging_complete()
        return {"ok": True}

    @app.post("/api/batch/{bid}/run")
    def batch_run(bid: str):
        try:
            batchmod.get_batch(bid).start()
        except batchmod.BatchBusy as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @app.post("/api/batch/{bid}/cancel")
    def batch_cancel(bid: str):
        batchmod.get_batch(bid).cancel()
        return {"ok": True}

    @app.post("/api/batch/{bid}/retry")
    def batch_retry(bid: str):
        try:
            batchmod.get_batch(bid).retry_failed()
        except batchmod.BatchBusy as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @app.post("/api/batch/{bid}/restore")
    def batch_restore(bid: str, body: Optional[Dict[str, Any]] = Body(None)):
        """body: {force?: bool, indices?: [int]}. Files edited since the batch are skipped unless
        force (then the edited file is kept as <name>-before-restore). Per-file results."""
        body = body or {}
        idx = body.get("indices")
        return batchmod.get_batch(bid).restore_originals(force=bool(body.get("force")),
                                                         indices=[int(i) for i in idx] if idx else None)

    @app.post("/api/batch/{bid}/discard")
    def batch_discard(bid: str):
        batchmod.discard_batch(bid)
        return {"ok": True}

    @app.get("/api/batch/{bid}")
    def batch_status(bid: str):
        return batchmod.get_batch(bid).summary()

    @app.get("/api/batch/{bid}/report.csv")
    def batch_report(bid: str):
        csv_text = batchmod.get_batch(bid).report_csv()
        return PlainTextResponse(csv_text, media_type="text/csv",
                                 headers={"Content-Disposition": f'attachment; filename="photoband-batch-{bid}.csv"'})

    @app.get("/api/batch-incomplete")
    def batch_incomplete():
        return batchmod.incomplete_batches()

    @app.post("/api/batch/{bid}/resume")
    def batch_resume(bid: str):
        try:
            b = batchmod.get_batch(bid)
        except (KeyError, ValueError):
            raise HTTPException(404, "This batch no longer exists.")
        # exactly the batch's photos and the files it wrote (not their whole folders)
        meta = b.data.get("meta", {}) or {}
        grant = [p for p in meta.get("files") or [] if isinstance(p, str)]
        grant += [x.get("path") for x in meta.get("plan") or [] if isinstance(x, dict) and isinstance(x.get("path"), str)]
        for e in (b.data.get("entries") or {}).values():
            for k in ("path", "dest", "out"):
                if isinstance(e.get(k), str) and e.get(k):
                    grant.append(e[k])
        security.allow([p for p in grant if p and os.path.isfile(p)])
        _sweep_async(b.folders())
        return b.summary()

    return app


_BUNDLE_EXT = (".app", ".appex", ".bundle", ".framework", ".plugin", ".kext", ".pkg", ".mpkg",
               ".prefpane", ".saver", ".qlgenerator", ".workflow", ".action", ".xpc")


def reveal_argv(p: str, system: Optional[str] = None) -> Optional[List[str]]:
    """The command that shows ``p`` in the file manager without opening it:
    macOS ``open -R``, Windows ``explorer /select,``, elsewhere ``xdg-open`` on a plain folder."""
    plat = system or sys.platform
    if plat == "darwin":
        return ["open", "-R", p]
    if plat == "win32":
        if '"' in p:
            return None
        return ["explorer", f"/select,{p}"]
    d = p if os.path.isdir(p) else os.path.dirname(p)
    while d and d.lower().rstrip("/").endswith(_BUNDLE_EXT):
        d = os.path.dirname(d.rstrip("/"))
    if not d or not os.path.isdir(d):
        return None
    return ["xdg-open", d]


def _reveal(p: str) -> None:
    argv = reveal_argv(p)
    if not argv:
        return
    try:
        if sys.platform == "win32":
            # explorer wants /select,"path" as one token; paths can't contain quotes (checked above)
            subprocess.Popen(f'explorer /select,"{p}"', shell=False)
        else:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        log.debug("could not reveal %s", p, exc_info=True)


app = create_app()
