"""Existing caption text: decide which case a photo is and what the editor starts from.

Read order on open (spec): photoband metadata record, then hidden marker plus
payload, then marker only (exact edge, OCR for text), then plain detection.

Every decision the result feeds (the editor's starting state, the batch plan) must
mean the same thing whichever of these sources it came from: a file whose
metadata was stripped is still the file Photoband wrote. See photoband/record.py
for which record fields the marker payload carries and what each path does when
a field is missing (provenance: :func:`provenance`).
"""
from __future__ import annotations

import base64
import io
from typing import Any, Dict, List, Optional

import numpy as np
from PIL import Image

from . import record as _record
from .detect import (BandResult, TextBlock, TextLine, band_from_rect, decide_case, detect_band, detect_text_over_photo,
                     edge_candidates, estimate_style, estimate_styles, filter_ocr_lines, find_text,
                     line_ocr_crop, scan_evidence)
from .imageio import ImageInfo, pixel_hash


def band_from_json(d: Dict[str, Any]) -> BandResult:
    return BandResult(
        found=bool(d.get("found", True)), photo_rect=tuple(int(v) for v in d["photo_rect"]),
        bands=[{"side": b["side"], "rect": tuple(int(v) for v in b["rect"]),
                **({"color": tuple(b["color"])} if b.get("color") is not None else {})} for b in d.get("bands", [])],
        band_color=tuple(d.get("band_color") or ()), band_color_hex=d.get("band_color_hex", ""),
        textured=bool(d.get("textured")), noise=float(d.get("noise", 0.0)),
        confidence=float(d.get("confidence", 0.0)), angle=float(d.get("angle", 0.0) or 0.0),
        photo_quad=tuple(tuple(float(v) for v in p) for p in (d.get("photo_quad") or [])),
        edge_blur=float(d.get("edge_blur", 0.0) or 0.0), color_drift=float(d.get("color_drift", 0.0) or 0.0),
        nested=bool(d.get("nested", False)))


def blocks_from_json(items: List[Dict[str, Any]]) -> List[TextBlock]:
    out = []
    for b in items or []:
        lines = [TextLine(box=tuple(int(v) for v in ln["box"]), text=ln.get("text", ""),
                          confidence=float(ln.get("confidence", 0.0)), words=list(ln.get("words", [])))
                 for ln in b.get("lines", [])]
        out.append(TextBlock(box=tuple(int(v) for v in b["box"]), lines=lines, role=b.get("role") or "caption"))
    return out


MASK_MAX = 8192          # px a side for brush masks (the UI draws them at preview size)


def decode_mask_png(data: Optional[str]) -> Optional[np.ndarray]:
    """A brush mask sent by the UI as a data URL / base64 PNG (any size; alpha or luminance > 0 = on)."""
    if not data:
        return None
    if "," in data and data.startswith("data:"):
        data = data.split(",", 1)[1]
    try:
        im = Image.open(io.BytesIO(base64.b64decode(data)))
    except Exception:
        return None
    # masks come at preview size: refuse a huge one before decoding it (a tiny PNG can declare one)
    if im.size[0] > MASK_MAX or im.size[1] > MASK_MAX:
        raise ValueError(f"The brush mask is {im.size[0]}×{im.size[1]} px; at most {MASK_MAX} px a side")
    try:
        a = np.asarray(im.convert("RGBA"))
        return (a[:, :, 3] > 0) & (a[:, :, :3].max(axis=2) > 0)
    except Exception:
        return None


def with_photo_rect(band: BandResult, rect, W: int, H: int) -> BandResult:
    """Band result with a user-confirmed photo rectangle; band rectangles recomputed."""
    x, y, w, h = (int(v) for v in rect)
    x, y = max(0, x), max(0, y)
    w, h = min(w, W - x), min(h, H - y)
    color = band.band_color
    bands = []
    if y > 0:
        bands.append({"side": "top", "rect": (0, 0, W, y), "color": color})
    if y + h < H:
        bands.append({"side": "bottom", "rect": (0, y + h, W, H - y - h), "color": color})
    if x > 0:
        bands.append({"side": "left", "rect": (0, y, x, h), "color": color})
    if x + w < W:
        bands.append({"side": "right", "rect": (x + w, y, W - x - w, h), "color": color})
    return BandResult(found=bool(bands), photo_rect=(x, y, w, h), bands=bands, band_color=band.band_color,
                      band_color_hex=band.band_color_hex, textured=band.textured, noise=band.noise,
                      confidence=band.confidence, angle=band.angle, photo_quad=band.photo_quad,
                      edge_blur=band.edge_blur, color_drift=band.color_drift, nested=band.nested)


def _crop_hash(arr: np.ndarray, off, size) -> Optional[str]:
    try:
        x, y = (int(v) for v in off)
        w, h = (int(v) for v in size)
    except Exception:
        return None
    if x < 0 or y < 0 or x + w > arr.shape[1] or y + h > arr.shape[0]:
        return None
    return pixel_hash(arr[y:y + h, x:x + w])


_INVERSE_ORIENT = {6: 8, 8: 6}


def _reoriented_record_match(arr: np.ndarray, rec: Dict[str, Any]) -> Optional[List[int]]:
    """Case A after a lossless rotate/flip elsewhere (an Orientation flag
    change): the record's canvas, offset and hash describe the pixels as they
    were saved. Try the 7 other orientations of the upright array; on a hash
    match return the photo rectangle in the current upright frame.

    Only the photo crop is transformed and hashed, never the whole image, and
    only orientations whose frame has the record's canvas size are tried."""
    from .imageio import orient_box, upright
    try:
        cw, ch = (int(v) for v in rec.get("canvas") or [])
        ox, oy = (int(v) for v in rec.get("photoOffset") or [])
        ow, oh = (int(v) for v in rec.get("originalSize") or [])
    except (TypeError, ValueError):
        return None
    if cw <= 0 or ch <= 0 or ow <= 0 or oh <= 0 or ox < 0 or oy < 0 or ox + ow > cw or oy + oh > ch:
        return None
    H, W = arr.shape[:2]
    want = {rec.get("outputPhotoHash"), rec.get("photoHash")} - {None}
    for o in (2, 3, 4, 5, 6, 7, 8):
        # arr == upright(saved, o): the saved frame has the canvas size
        if (W, H) != ((ch, cw) if o in (5, 6, 7, 8) else (cw, ch)):
            continue
        x, y, w, h = orient_box((ox / cw, oy / ch, ow / cw, oh / ch), o)
        X, Y = int(round(x * W)), int(round(y * H))
        PW, PH = int(round(w * W)), int(round(h * H))
        crop = arr[Y:Y + PH, X:X + PW]
        back = upright(crop, _INVERSE_ORIENT.get(o, o))
        if back.shape[1] == ow and back.shape[0] == oh and pixel_hash(back) in want:
            return [X, Y, PW, PH]
    return None


def _proxy8(arr: np.ndarray, long_edge: int = 1600):
    import cv2
    H, W = arr.shape[:2]
    s = min(1.0, long_edge / max(H, W))
    small = cv2.resize(arr, (max(1, round(W * s)), max(1, round(H * s))), interpolation=cv2.INTER_AREA) if s < 1 else arr
    if small.ndim == 2:
        small = small[:, :, None]
    c = small.shape[2]
    col = small[:, :, :3] if c >= 3 else np.repeat(small[:, :, :1], 3, axis=2)
    if col.dtype == np.uint16:
        col = (col // 257).astype(np.uint8)
    return np.ascontiguousarray(col.astype(np.uint8)), s


def _erase_inputs_for_record(arr: np.ndarray, rect) -> Dict[str, Any]:
    H, W = arr.shape[:2]
    band = detect_band(arr)
    if not band.found or not band.band_color:
        x, y, w, h = (int(v) for v in rect)
        mask = np.ones((H, W), bool)
        mask[y:y + h, x:x + w] = False
        c = arr[..., : (1 if arr.shape[2] in (1, 2) else 3)][mask]
        med = tuple(float(v) for v in np.median(c, axis=0)) if c.size else (255.0,)
        unit = 257.0 if arr.dtype == np.uint16 else 1.0
        hexc = "#%02x%02x%02x" % tuple(int(round(v / unit)) for v in (med * 3)[:3]) if len(med) == 1 else \
            "#%02x%02x%02x" % tuple(int(round(v / unit)) for v in med[:3])
        band = BandResult(found=True, photo_rect=tuple(rect), bands=[], band_color=med, band_color_hex=hexc,
                          textured=False, noise=0.0, confidence=1.0)
    band = with_photo_rect(band, rect, W, H)
    blocks = find_text(arr, band)
    return {"band": band.to_json(), "blocks": [b.to_json() for b in blocks]}


SAVE_MODES = ("copy", "overwrite")


def provenance(rec: Optional[Dict[str, Any]], payload: Optional[Dict[str, Any]], marker: bool) -> Dict[str, Any]:
    """How a Photoband output was saved, from whatever survived: the record's ``saveMode``, else
    the marker payload's (the same value, written since payloads carry it), else, for records
    from before ``saveMode``, ``originalFile`` (only an in-place save records the original it
    replaced). A file that is Photoband's output (record or hidden marker) but says neither, e.g. a
    JPEG whose metadata was stripped (its marker has no payload), is of UNKNOWN provenance.

    Returns ``{"isCopy": True}`` for a known copy, ``{"isCopy": False}`` for a known in-place save,
    ``{"isCopy": False, "copyUnknown": True}`` when it can't be told (callers must not treat that
    file as an ordinary photo: it may be a copy), and ``{}`` for a file that isn't Photoband's.
    Payload values come from the file and are untrusted: only the exact strings count."""
    for src in (rec, payload):
        m = (src or {}).get("saveMode")
        if m in SAVE_MODES:
            return {"isCopy": m == "copy"}
    if rec and rec.get("originalFile"):
        return {"isCopy": False}
    if rec or payload or marker:
        return {"isCopy": False, "copyUnknown": True}
    return {}


def _record_from_payload(p: Dict[str, Any]) -> Dict[str, Any]:
    """The ``record`` summary the editor reads (``mode`` decides erase-in-place vs rebuild), from a
    marker payload, with the fields a payload doesn't carry (appVersion, saved, originalText) None."""
    lay = p.get("layout") if isinstance(p.get("layout"), dict) else {}
    tpl = p.get("template") if isinstance(p.get("template"), dict) else {}
    mode = lay.get("mode")
    return {"appVersion": None, "saved": None, "templateId": tpl.get("id"),
            "originalSize": p.get("originalSize"), "photoOffset": p.get("photoOffset"), "canvas": p.get("canvas"),
            "originalText": None, "mode": mode if mode in ("band", "rebuild", "erase") else "band",
            "fromPayload": True}


def analyze_existing(arr: np.ndarray, info: ImageInfo, md: Dict[str, Any], run_ocr: bool = True) -> Dict[str, Any]:
    """Decide the case for an opened photo. ``arr`` is upright full resolution."""
    H, W = arr.shape[:2]
    out: Dict[str, Any] = {"case": None, "source": None, "warnings": []}

    # 1. metadata record
    rec = _record.from_metadata(md)
    # how the file was saved is a fact about the FILE, not about its pixels: it holds even when
    # the photo was changed in another app since (the detection fallback below)
    out.update(provenance(rec, None, False))
    if rec:
        out["record"] = {k: rec.get(k) for k in ("appVersion", "saved", "templateId", "originalSize",
                                                   "photoOffset", "canvas", "originalText", "mode")}
        h = _crop_hash(arr, rec.get("photoOffset", [0, 0]), rec.get("originalSize", [0, 0]))
        rect = None
        if h and h in (rec.get("outputPhotoHash"), rec.get("photoHash")) and list(rec.get("canvas") or []) == [W, H]:
            ox, oy = rec["photoOffset"]
            ow, oh = rec["originalSize"]
            rect = [ox, oy, ow, oh]
        else:
            # rotated or flipped losslessly in another app (Orientation flag changed)
            rect = _reoriented_record_match(arr, rec)
        if rect is not None:
            out.update(case="A", source="record", confidence=1.0,
                       sourceRect=rect,
                       state={"template": rec.get("template"), "templateId": rec.get("templateId"),
                              "overrides": rec.get("overrides"), "blocks": rec.get("blocks")},
                       originalText=rec.get("originalText"),
                       lossyRecaption=info.format == "JPEG")
            # isCopy (set above): a captioned copy Photoband saved (copies sit next to their
            # originals), which a batch over the folder must not caption again
            if rec.get("mode") == "erase":
                # re-editing an erase-in-place save keeps the original paper band: provide the band
                # and the text this app drew there, so the editor can erase it again
                try:
                    out.update(_erase_inputs_for_record(arr, rect))
                except Exception as e:  # pragma: no cover - editor falls back to rebuild
                    out["warnings"].append(f"Couldn't prepare the band for editing in place: {e}")
            return out
        out["warnings"].append("This file has a Photoband record, but the photo was changed in another app, so "
                               "the caption is being detected instead.")

    # 2/3. hidden marker, using band detection for the approximate edge
    band = detect_band(arr)
    mk = _read_marker(arr, band, W, H)
    if mk is not None:
        rect_ok = bool(getattr(mk, "rect_ok", True))
        out["marker"] = {"photoRect": list(mk.photo_rect), "payload": mk.payload is not None, "rectVerified": rect_ok}
        p = None
        hash_ok = False
        if mk.payload is not None:
            from .save import decode_marker_payload
            p = decode_marker_payload(mk.payload)
            p = p if isinstance(p, dict) else None
            x, y, w, h = mk.photo_rect
            if p and x >= 0 and y >= 0 and x + w <= W and y + h <= H:
                crop_h = pixel_hash(arr[y:y + h, x:x + w])
                hash_ok = crop_h[:16] == mk.short_hash and p.get("photoHash", "")[:16] == mk.short_hash
        # the payload is bound to this marker (salt and CRC over its hash), so its saveMode holds even
        # when the photo itself no longer matches; with no record and no payload it is unknown
        for k in ("isCopy", "copyUnknown"):
            out.pop(k, None)
        out.update(provenance(rec, p, True))
        if rect_ok or hash_ok:
            # exact rect from the marker; bands re-measured around it (detection may
            # have failed entirely on a high-key photo)
            band = band_from_rect(arr, mk.photo_rect, like=band)
        if p and hash_ok:
            out.update(case="A", source="marker+payload", confidence=1.0, sourceRect=list(mk.photo_rect),
                       state={"template": p.get("template"), "templateId": (p.get("template") or {}).get("id"),
                              "overrides": p.get("overrides"), "blocks": p.get("blocks")},
                       band=band.to_json(), lossyRecaption=info.format == "JPEG")
            if not rec:
                # what the record path gives the editor (mode: erase in place keeps the paper band)
                out["record"] = _record_from_payload(p)
            if (p.get("layout") or {}).get("mode") == "erase":
                # same as the record path: an erase-in-place save keeps the original paper band, so
                # provide the band and the text this app drew there for the editor to erase again
                try:
                    out.update(_erase_inputs_for_record(arr, mk.photo_rect))
                except Exception as e:  # pragma: no cover - editor falls back to rebuild
                    out["warnings"].append(f"Couldn't prepare the band for editing in place: {e}")
            return out
        if not rect_ok and not hash_ok:
            # the file was cropped or stretched since it was saved: the stored edge is no longer exact
            out["warnings"].append("This file has a Photoband hidden marker, but it was cropped or resized unevenly "
                                   "since, so the photo edge is detected instead.")
            mk = None

    # 4. detection (+ OCR)
    blocks = find_text(arr, band) if band.found else []
    engine = None
    ocr_ran = False
    if run_ocr and blocks:
        from . import ocr
        try:
            used = ocr.recognize_blocks(arr, blocks,
                                        crop_fn=lambda ln: line_ocr_crop(arr, band, ln))
            # the engine that actually read the text (a broken first choice falls through)
            engine = (used or ocr.engines() or [None])[0]
            ocr_ran = engine is not None
            if ocr_ran:
                # only plausible text on writing-sized marks counts as read (an engine's "|" or
                # "az" on dust and hairs is not a caption; see detect.filter_ocr_lines)
                blocks = filter_ocr_lines(blocks, shape=arr.shape)
        except Exception as e:
            out["warnings"].append(f"Text recognition failed: {e}")
    over = []
    status: Dict[str, Any] = {}
    if run_ocr:
        try:
            p8, s = _proxy8(arr)
            px, py, pw, ph = band.photo_rect
            pr = (int(px * s), int(py * s), max(1, int(pw * s)), max(1, int(ph * s)))
            for bx, by, bw, bh in detect_text_over_photo(p8, pr, status):
                over.append([int(bx / s), int(by / s), int(np.ceil(bw / s)), int(np.ceil(bh / s))])
        except Exception:
            pass
        if status.get("general_text") == "timeout":
            out["warnings"].append("The check for text printed over the photo ran out of time; only date stamps "
                                   "were checked.")
    score, cues = scan_evidence(band, md)
    # ocr_ran: only when letters could actually be read (no OCR asked for, or no engine, is not
    # "read and found nothing"); then the evidence of writing is the shape of the marks
    case, hint = decide_case(band, blocks, score, ocr_ran, marker=mk is not None, shape=arr.shape)
    if mk is not None and case in ("B", "C"):
        case = "B"   # the hidden marker proves a Photoband (digital) band; its pattern is not paper grain
    if hint:
        out["hints"] = [hint]
        out["warnings"].append(hint)   # shown as a quiet row by the existing banner UI
        blocks = []
    if case is None and band.found and hint:
        # not a caption band: the whole image is the photo
        H_, W_ = arr.shape[:2]
        band_json = dict(band.to_json(), found=False, bands=[], photo_rect=[0, 0, W_, H_])
    else:
        band_json = band.to_json()
    if case is None and over:
        case = "D"
    style = estimate_style(arr, band, blocks) if blocks else None
    captions = [b for b in blocks if b.role != "other"]
    out.update(
        case=case,
        source="marker" if mk is not None else ("detection" if case in ("B", "C") else None),
        # how sure the detector is WHERE the photo ends (the band's edge), not whether anything is
        # written on the border: that is the case itself (B/C need writing, see decide_case)
        confidence=1.0 if mk is not None else (float(band.confidence) if case in ("B", "C") else 0.0),
        edgeConfidence=1.0 if mk is not None else (float(band.confidence) if case in ("B", "C") else 0.0),
        band=band_json,
        blocks=[b.to_json() for b in blocks],
        text="\n".join(ln.text for b in captions for ln in b.lines if ln.text),
        style=style.to_json() if style else None,
        styles=estimate_styles(arr, band, blocks) if blocks else [],
        textOverPhoto=over,
        engine=engine,
        # words were read (an unread mark, e.g. with no OCR engine, gives nothing to "use")
        hasText=any(ln.text.strip() for b in captions for ln in b.lines),
        scanCues=cues,
        scanScore=round(float(score), 2),
    )
    if over:
        out["warnings"].append("Text printed over the photo was found. It is flagged only and never erased.")
    return out


def _read_marker(arr: np.ndarray, band: BandResult, W: int, H: int):
    """Hidden marker: first at the detected edge (+-4 px), then by sweeping that
    edge across the largest band (a confidently wrong detection, e.g. a
    high-key photo whose edge vanishes into the band; well under 1 s extra at
    3000 px), and when detection failed or looks implausible also at the
    strongest edge candidates of each side (at most 8 extra reads)."""
    try:
        from . import marker
    except Exception:
        return None
    if band.found:
        plausible = len(band.bands) >= 2 and band.confidence >= 0.6
        try:
            mk = marker.read(arr, band.photo_rect, sweep=True)
        except Exception:
            mk = None
        if mk is not None:
            return mk
        if plausible:
            return None
    tried = set()
    if band.found:
        x, y, w, h = band.photo_rect
        tried = {("top", y), ("bottom", H - y - h), ("left", x), ("right", W - x - w)}
    try:
        cands = edge_candidates(arr)
    except Exception:
        cands = []
    n = 0
    for side, depth in cands:
        d = int(round(depth))
        if d < 4 or any(s == side and abs(d - v) <= 3 for s, v in tried):
            continue
        if side in ("top", "bottom") and not (0.02 * H <= d <= 0.45 * H):
            continue
        if side in ("left", "right") and not (0.02 * W <= d <= 0.45 * W):
            continue
        rect = {"top": (0, d, W, H - d), "bottom": (0, 0, W, H - d),
                "left": (d, 0, W - d, H), "right": (0, 0, W - d, H)}[side]
        tried.add((side, d))
        n += 1
        try:
            mk = marker.read(arr, rect)
        except Exception:
            mk = None
        if mk is not None:
            return mk
        if n >= 8:
            break
    return None
