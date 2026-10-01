#!/usr/bin/env python3
"""Fetch the ExifTool copy bundled with the app into vendor/exiftool.

    python scripts/fetch_vendor.py              # pinned version, checksum-verified
    python scripts/fetch_vendor.py --archive F  # use an already-downloaded archive F instead
    python scripts/fetch_vendor.py --no-verify  # skip the checksum (prints it, for vendor.lock.json)

* The version, file names and SHA-256 are pinned in scripts/vendor.lock.json. exiftool.org
  deletes old versions, so the SourceForge mirror is tried next.
* Windows gets the stand-alone package (exiftool(-k).exe renamed to exiftool.exe, plus its
  exiftool_files folder); macOS/Linux get the Perl distribution (needs the system Perl).
* vendor/exiftool is replaced only after the new copy is downloaded, verified and unpacked.
* Tesseract is not downloaded. On Windows copy a UB-Mannheim build (tesseract.exe + DLLs +
  tessdata/eng.traineddata) into vendor/tesseract; on macOS Apple Vision is used instead.

Only the stdlib is used, so this runs before any dependency is installed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR = os.path.join(ROOT, "vendor")
LOCK = os.path.join(ROOT, "scripts", "vendor.lock.json")
UNVERIFIED = "TBD-verify"

MANUAL_HELP = """\
You can install ExifTool yourself instead; Photoband finds it on PATH:
  macOS:   brew install exiftool
  Windows: winget install OliverBetz.ExifTool
  Linux:   sudo apt install libimage-exiftool-perl   (or your distro's exiftool package)
or download it from https://exiftool.org and set PHOTOBAND_EXIFTOOL=/path/to/exiftool.
(App installers need the bundled copy: build them on a machine that can reach exiftool.org.)"""


class FetchError(Exception):
    pass


def load_lock(path: str = LOCK) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)["exiftool"]


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(urls: list[str], dst: str) -> str:
    """Try each URL in turn; returns the one that worked."""
    errors = []
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "photoband-fetch-vendor"})
            with urllib.request.urlopen(req, timeout=120) as r, open(dst, "wb") as fh:
                shutil.copyfileobj(r, fh)
            if os.path.getsize(dst) < 1024:
                raise FetchError("the download is too small to be ExifTool")
            return url
        except (urllib.error.URLError, OSError, FetchError) as e:  # HTTPError is a URLError
            errors.append(f"  {url}: {getattr(e, 'reason', None) or e}")
    raise FetchError("Could not download ExifTool:\n" + "\n".join(errors))


def _safe_extract_tar(archive: str, dst: str) -> None:
    with tarfile.open(archive) as t:
        if hasattr(tarfile, "data_filter"):
            t.extractall(dst, filter="data")
            return
        root = os.path.realpath(dst)
        for m in t.getmembers():
            p = os.path.realpath(os.path.join(dst, m.name))
            if not (p == root or p.startswith(root + os.sep)) or m.issym() or m.islnk() or m.isdev():
                raise FetchError(f"Unsafe entry in archive: {m.name}")
        t.extractall(dst)


def _safe_extract_zip(archive: str, dst: str) -> None:
    root = os.path.realpath(dst)
    with zipfile.ZipFile(archive) as z:
        for n in z.namelist():
            p = os.path.realpath(os.path.join(dst, n))
            if not (p == root or p.startswith(root + os.sep)):
                raise FetchError(f"Unsafe entry in archive: {n}")
        z.extractall(dst)


def _find(top: str, pred) -> str | None:
    """First path under top (files and dirs, shallowest first) whose basename matches."""
    for root, dirs, files in os.walk(top):
        dirs.sort()
        for n in sorted(files) + dirs:
            if pred(n):
                return os.path.join(root, n)
    return None


def unpack(archive: str, kind: str, out: str) -> None:
    """Unpack into ``out`` (which must not exist) as the flat layout the app expects:
    unix: out/exiftool + out/lib;  win64: out/exiftool.exe + out/exiftool_files."""
    work = tempfile.mkdtemp(prefix="exiftool-", dir=os.path.dirname(out))
    try:
        os.makedirs(out)
        if kind == "win64":
            _safe_extract_zip(archive, work)
            exe = _find(work, lambda n: n.lower().startswith("exiftool") and n.lower().endswith(".exe"))
            if not exe:
                raise FetchError("exiftool(-k).exe was not found in the archive")
            files_dir = os.path.join(os.path.dirname(exe), "exiftool_files")
            shutil.move(exe, os.path.join(out, "exiftool.exe"))
            if os.path.isdir(files_dir):
                shutil.move(files_dir, os.path.join(out, "exiftool_files"))
        else:
            _safe_extract_tar(archive, work)
            script = _find(work, lambda n: n == "exiftool")
            if not script or not os.path.isfile(script):
                raise FetchError("the exiftool script was not found in the archive")
            base = os.path.dirname(script)
            if not os.path.isdir(os.path.join(base, "lib")):
                raise FetchError("the archive has no lib/ folder next to exiftool")
            shutil.move(script, os.path.join(out, "exiftool"))
            shutil.move(os.path.join(base, "lib"), os.path.join(out, "lib"))
            os.chmod(os.path.join(out, "exiftool"), 0o755)
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def install(new: str, dst: str) -> None:
    """Swap the freshly unpacked copy into place."""
    old = dst + ".old"
    shutil.rmtree(old, ignore_errors=True)
    if os.path.exists(dst):
        os.replace(dst, old)
    os.replace(new, dst)
    shutil.rmtree(old, ignore_errors=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--archive", help="use this already-downloaded archive instead of downloading")
    ap.add_argument("--no-verify", action="store_true", help="skip the SHA-256 check (prints the hash)")
    ap.add_argument("--platform", choices=["win64", "unix"],
                    default="win64" if sys.platform == "win32" else "unix", help=argparse.SUPPRESS)
    ap.add_argument("--dest", default=os.path.join(VENDOR, "exiftool"), help=argparse.SUPPRESS)
    ap.add_argument("--lock", default=LOCK, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    lock = load_lock(args.lock)
    entry = lock[args.platform]
    name, want = entry["file"], (entry.get("sha256") or "").lower()
    if not args.no_verify and (not want or want == UNVERIFIED.lower()):
        print("\n" + "!" * 78 + f"\nREFUSING: scripts/vendor.lock.json has no verified SHA-256 for {name}.\n"
              "Fill it in (see the note in that file), or re-run with --no-verify if you have\n"
              "checked the download yourself.\n" + "!" * 78, file=sys.stderr)
        return 2

    dst = os.path.abspath(args.dest)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="fetch-vendor-", dir=os.path.dirname(dst))
    try:
        if args.archive:
            archive, source = os.path.abspath(args.archive), args.archive
        else:
            archive = os.path.join(tmpdir, name)
            urls = [m.format(file=name) for m in lock["mirrors"]]
            print(f"Downloading ExifTool {lock['version']} ({name}) ...", flush=True)
            source = download(urls, archive)
        got = sha256_file(archive)
        if args.no_verify:
            print(f"WARNING: checksum NOT verified. sha256 of {os.path.basename(archive)}: {got}")
        elif got != want:
            raise FetchError(f"Checksum mismatch for {name} from {source}:\n  expected {want}\n  got      {got}\n"
                             "The file is corrupt or not the pinned release. Nothing was changed.")
        new = os.path.join(tmpdir, "unpacked")
        unpack(archive, args.platform, new)
        install(new, dst)
    except FetchError as e:
        print(f"\nERROR: {e}\n\n{MANUAL_HELP}", file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    label = f"ExifTool from {args.archive}" if args.archive else f"ExifTool {lock['version']}"
    print(f"{label} -> {dst}")
    tess = os.path.join(VENDOR, "tesseract")
    if not os.path.isdir(tess):
        print("Note: vendor/tesseract is empty; OCR uses the OS engine or a system Tesseract.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
