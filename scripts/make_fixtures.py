#!/usr/bin/env python3
"""Generate the spec's test fixtures into tests/fixtures (or a given folder).

    python scripts/make_fixtures.py [out_dir] [--big]

--big also writes the 600 MB 16-bit scan used for the performance targets.
"""
from __future__ import annotations

import json
import math
import os
import struct
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def natural(w: int, h: int, seed: int = 0, sky: bool = False) -> np.ndarray:
    """Smooth colored blobs + grain: a stand-in for a real photo (float 0..1)."""
    rng = np.random.default_rng(seed)
    small = rng.random((max(2, h // 64), max(2, w // 64), 3))
    img = np.array(Image.fromarray((small * 255).astype(np.uint8)).resize((w, h), Image.BICUBIC)) / 255.0
    yy, xx = np.mgrid[0:h, 0:w]
    img = img * 0.7 + 0.15
    img[..., 1] *= 0.9 + 0.1 * np.sin(xx / 37.0)
    if sky:
        t = np.clip(yy / (0.35 * h), 0, 1)[..., None]
        skycol = np.array([0.985, 0.975, 0.96]) * (1 - t) + np.array([0.80, 0.84, 0.90]) * t
        img = np.where((yy < 0.35 * h)[..., None], skycol, img)
    img += rng.normal(0, 0.008, img.shape)
    return np.clip(img, 0, 1)


def draw_faces(img: np.ndarray, boxes) -> None:
    h, w = img.shape[:2]
    pil = Image.fromarray((img * 255).astype(np.uint8))
    d = ImageDraw.Draw(pil)
    for (x, y, bw, bh) in boxes:
        d.ellipse([x * w, y * h, (x + bw) * w, (y + bh) * h], fill=(224, 182, 150), outline=(120, 80, 60), width=3)
    img[...] = np.asarray(pil) / 255.0


def to_dtype(img: np.ndarray, bits: int) -> np.ndarray:
    if bits == 16:
        return np.round(img * 65535).astype(np.uint16)
    return np.round(img * 255).astype(np.uint8)


def exiftool(args, path):
    subprocess.run(["exiftool", "-overwrite_original", "-m", *args, path], check=True, capture_output=True)


def exif_json(path, data):
    jp = path + ".json"
    with open(jp, "w", encoding="utf-8") as fh:
        json.dump([{"SourceFile": path, **data}], fh, ensure_ascii=False)
    subprocess.run(["exiftool", "-overwrite_original", "-m", "-struct", f"-json={jp}", path], check=True,
                   capture_output=True)
    os.unlink(jp)


def mwg(boxes, names, W, H):
    """MWG RegionInfo struct from top-left normalized boxes."""
    lst = []
    for (x, y, w, h), n in zip(boxes, names):
        r = {"Area": {"X": x + w / 2, "Y": y + h / 2, "W": w, "H": h, "Unit": "normalized"}, "Type": "Face"}
        if n:
            r["Name"] = n
        lst.append(r)
    return {"AppliedToDimensions": {"W": W, "H": H, "Unit": "pixel"}, "RegionList": lst}


def prophoto_icc() -> bytes:
    """Minimal ICC v2 matrix/TRC profile with ROMM RGB (ProPhoto) primaries, D50, gamma 1.8."""
    def s15(v):
        return struct.pack(">i", int(round(v * 65536)))

    def xyz(x, y, z):
        return b"XYZ " + b"\0" * 4 + s15(x) + s15(y) + s15(z)

    def curv(g):
        return b"curv" + b"\0" * 4 + struct.pack(">I", 1) + struct.pack(">H", int(round(g * 256)))

    def desc(t):
        a = t.encode("ascii") + b"\0"
        return b"desc" + b"\0" * 4 + struct.pack(">I", len(a)) + a + b"\0" * 4 + b"\0" * 4 + b"\0" * 2 + b"\0" + b"\0" * 67

    def text(t):
        return b"text" + b"\0" * 4 + t.encode("ascii") + b"\0"

    tags = [
        (b"desc", desc("ProPhoto RGB (Photoband test)")),
        (b"cprt", text("No copyright, test fixture")),
        (b"wtpt", xyz(0.9642, 1.0, 0.8249)),
        (b"rXYZ", xyz(0.7977, 0.2880, 0.0)),
        (b"gXYZ", xyz(0.1352, 0.7119, 0.0)),
        (b"bXYZ", xyz(0.0313, 0.0001, 0.8249)),
        (b"rTRC", curv(1.8)), (b"gTRC", curv(1.8)), (b"bTRC", curv(1.8)),
    ]
    n = len(tags)
    offset = 128 + 4 + 12 * n
    table = b""
    data = b""
    for sig, body in tags:
        while (offset + len(data)) % 4:
            data += b"\0"
        table += sig + struct.pack(">II", offset + len(data), len(body))
        data += body
    size = offset + len(data)
    hdr = struct.pack(">I", size) + b"lcms" + struct.pack(">I", 0x02100000) + b"mntr" + b"RGB " + b"XYZ "
    hdr += b"\0" * 12 + b"acsp" + b"APPL" + b"\0" * 4 + b"\0" * 4 + b"\0" * 4 + b"\0" * 8 + b"\0" * 4
    hdr += s15(0.9642) + s15(1.0) + s15(0.8249) + b"lcms" + b"\0" * 44
    assert len(hdr) == 128, len(hdr)
    return hdr + struct.pack(">I", n) + table + data


def font(size, prefer=("DejaVuSerif.ttf", "DejaVuSans.ttf", "LiberationSerif-Regular.ttf")):
    for root in ("/usr/share/fonts", "/Library/Fonts", "C:/Windows/Fonts"):
        for dp, _, fs in os.walk(root):
            for p in prefer:
                if p in fs:
                    return ImageFont.truetype(os.path.join(dp, p), size)
    # No DejaVu (Windows, stock macOS): Pillow's built-in scalable font at the asked
    # size.  The size-less bitmap default is ~10 px whatever ``size`` says, which
    # left e.g. the 70 px date stamp of fixture 13 too small to be a stamp.
    return ImageFont.load_default(size)


# ----------------------------------------------------------------------------

def main(out: str, big: bool = False) -> None:
    import tifffile
    os.makedirs(out, exist_ok=True)
    made = []

    # 1. 16-bit ProPhoto LZW TIFF with MWG regions (Lightroom-style)
    W, H = 3000, 2000
    img = natural(W, H, 1)
    boxes = [(0.12, 0.30, 0.10, 0.16), (0.44, 0.28, 0.10, 0.16), (0.74, 0.32, 0.10, 0.16)]
    draw_faces(img, boxes)
    p = os.path.join(out, "01_prophoto16_lzw.tif")
    tifffile.imwrite(p, to_dtype(img, 16), photometric="rgb", compression="lzw", predictor=2,
                     iccprofile=prophoto_icc(), resolution=(600, 600), resolutionunit="inch", metadata=None)
    exiftool(["-XMP-dc:Title=Picnic at Lake Merced", "-XMP-dc:Description=Sunday outing with the neighbors",
              "-XMP-photoshop:DateCreated=1952:06:14", "-XMP-photoshop:City=San Francisco",
              "-XMP-photoshop:State=CA", "-XMP-photoshop:Country=USA"], p)
    exif_json(p, {"XMP-mwg-rs:RegionInfo": mwg(boxes, ["Ann Church", "Bea Ortiz", "Carl Church"], W, H)})
    made.append(p)

    # 2. 8-bit grayscale uncompressed
    g = natural(2400, 1800, 2).mean(axis=2)
    p = os.path.join(out, "02_gray8_uncompressed.tif")
    tifffile.imwrite(p, to_dtype(g, 8), photometric="minisblack", compression=None, resolution=(300, 300),
                     resolutionunit="inch", metadata=None)
    exiftool(["-XMP-dc:Title=Grandpa's workshop", "-IPTC:ObjectName=Grandpa's workshop"], p)
    made.append(p)

    # 3. JPEG Orientation=6 with MWG regions (regions against the stored image)
    Ws, Hs = 2400, 1600  # stored (landscape); displayed portrait
    img = natural(Ws, Hs, 3)
    # faces in *upright* coordinates, drawn upright, then store rotated so Orientation 6 displays them
    up = np.rot90(img, 1).copy()  # upright = stored rotated 90 CCW? build upright first:
    up = natural(Hs, Ws, 3)  # upright W=1600, H=2400
    ub = [(0.15, 0.20, 0.2, 0.12), (0.60, 0.22, 0.2, 0.12)]
    draw_faces(up, ub)
    stored = np.rot90(up, 1)  # rotate CCW by 90 so displaying with Orientation 6 (rotate CW) restores it
    p = os.path.join(out, "03_rotated6_mwg.jpg")
    Image.fromarray(to_dtype(np.ascontiguousarray(stored), 8)).save(p, quality=92)
    # stored-coordinate boxes: upright (x', y') = (1 - y - h, x) => stored x = y', stored y = 1 - x' - w'
    sb = [(b[1], 1 - b[0] - b[2], b[3], b[2]) for b in ub]
    exiftool(["-IFD0:Orientation#=6", "-XMP-dc:Title=Portrait by the window", "-EXIF:DateTimeOriginal=1987:08:02 15:30:00"], p)
    exif_json(p, {"XMP-mwg-rs:RegionInfo": mwg(sb, ["Left Person", "Right Person"], Ws, Hs)})
    made.append(p)

    # 4. MP regions (Windows Photo Gallery)
    img = natural(2000, 1500, 4)
    mb = [(0.2, 0.3, 0.12, 0.16), (0.6, 0.3, 0.12, 0.16)]
    draw_faces(img, mb)
    p = os.path.join(out, "04_mp_regions.tif")
    tifffile.imwrite(p, to_dtype(img, 8), photometric="rgb", compression="zlib", metadata=None,
                     resolution=(300, 300), resolutionunit="inch")
    exif_json(p, {"XMP-MP:RegionInfoMP": {"Regions": [
        {"PersonDisplayName": "Dora", "Rectangle": ", ".join(f"{v:.4f}" for v in mb[0])},
        {"PersonDisplayName": "Emil", "Rectangle": ", ".join(f"{v:.4f}" for v in mb[1])}]},
        "XMP-dc:Title": "Wedding 1961"})
    made.append(p)

    # 5. PersonInImage only
    p = os.path.join(out, "05_person_in_image.png")
    Image.fromarray(to_dtype(natural(1800, 1200, 5), 8)).save(p, dpi=(300, 300))
    exiftool(["-XMP-iptcExt:PersonInImage=Frank Miller", "-XMP-iptcExt:PersonInImage=Grace Lee",
              "-XMP-dc:Title=Beach day"], p)
    made.append(p)

    # 6. Group photo, three rows
    W, H = 3000, 2000
    img = natural(W, H, 6)
    rows = [(0.66, ["Hana", "Ivo", "Jun", "Kai"]), (0.43, ["Lia", "Max", "Noa"]), (0.20, ["Oli", "Pia", "Quin", "Ray"])]
    boxes, names = [], []
    for y, ns in rows:
        for i, n in enumerate(ns):
            x = 0.1 + i * (0.8 / len(ns)) + 0.02 * (y > 0.5)
            boxes.append((x, y + 0.01 * (i % 2), 0.08, 0.12))
            names.append(n)
    boxes.append((0.85, 0.44, 0.08, 0.12)); names.append("")  # one unnamed
    draw_faces(img, boxes)
    p = os.path.join(out, "06_group_three_rows.tif")
    tifffile.imwrite(p, to_dtype(img, 8), photometric="rgb", compression="lzw", metadata=None,
                     resolution=(400, 400), resolutionunit="inch")
    exiftool(["-XMP-dc:Title=Class of 1958", "-XMP-photoshop:DateCreated=1958"], p)
    exif_json(p, {"XMP-mwg-rs:RegionInfo": mwg(boxes, names, W, H)})
    made.append(p)

    # 7a. multi-page TIFF
    p = os.path.join(out, "07a_multipage.tif")
    with tifffile.TiffWriter(p) as tw:
        tw.write(to_dtype(natural(800, 600, 7), 8), photometric="rgb", metadata=None)
        tw.write(to_dtype(natural(800, 600, 8), 8), photometric="rgb", metadata=None)
    made.append(p)
    # 7b. CMYK TIFF
    p = os.path.join(out, "07b_cmyk.tif")
    rgb = to_dtype(natural(900, 600, 9), 8)
    cmyk = np.asarray(Image.fromarray(rgb).convert("CMYK"))
    tifffile.imwrite(p, cmyk, photometric="separated", metadata=None)
    made.append(p)

    # 8. diacritics + Cyrillic names
    W, H = 2000, 1400
    img = natural(W, H, 10)
    b8 = [(0.1, 0.3, 0.12, 0.18), (0.45, 0.3, 0.12, 0.18), (0.75, 0.3, 0.12, 0.18)]
    draw_faces(img, b8)
    p = os.path.join(out, "08_unicode_names.tif")
    tifffile.imwrite(p, to_dtype(img, 8), photometric="rgb", compression="lzw", metadata=None)
    exif_json(p, {"XMP-mwg-rs:RegionInfo": mwg(b8, ["Zoë Ångström", "Łukasz Dvořák", "Наталья Ивановна"], W, H),
                  "XMP-dc:Title": "Réunion à Kraków"})
    made.append(p)

    # 9. partial date
    p = os.path.join(out, "09_partial_date.jpg")
    Image.fromarray(to_dtype(natural(1600, 1200, 11), 8)).save(p, quality=90)
    exiftool(["-XMP-photoshop:DateCreated=1952", "-XMP-dc:Title=Summer"], p)
    made.append(p)

    # 11. captioned in another tool: colored band, font the app doesn't have
    W, H = 1800, 1200
    ph = to_dtype(natural(W, H, 12), 8)
    bh = 260
    canvas = np.zeros((H + bh, W, 3), np.uint8)
    canvas[:H] = ph
    canvas[H:] = (42, 77, 143)
    pil = Image.fromarray(canvas)
    d = ImageDraw.Draw(pil)
    f = font(64, ("DejaVuSans-Bold.ttf", "DejaVuSans.ttf"))
    d.text((60, H + 50), "Aunt Rosa and Uncle Tom", font=f, fill=(255, 255, 255))
    d.text((60, H + 140), "Lake Tahoe 1974", font=font(48, ("DejaVuSans.ttf",)), fill=(230, 230, 230))
    p = os.path.join(out, "11_other_tool_colored_band.png")
    pil.save(p)
    made.append(p)

    # 12. scanned real Polaroid with handwriting on textured paper (case C)
    W, H = 1600, 1560
    rng = np.random.default_rng(13)
    paper = np.ones((1900, 1760, 3)) * np.array([0.95, 0.93, 0.88])
    paper += rng.normal(0, 0.016, paper.shape)
    paper += np.linspace(0, 0.02, 1760)[None, :, None]
    x0, y0 = 80, 110
    paper[y0:y0 + H, x0:x0 + W] = natural(W, H, 14)
    pil = Image.fromarray(to_dtype(np.clip(paper, 0, 1), 8))
    d = ImageDraw.Draw(pil)
    hf = font(72, ("DejaVuSerif-Italic.ttf", "DejaVuSans-Oblique.ttf", "DejaVuSerif.ttf"))
    d.text((260, y0 + H + 60), "Mom & Dad, Yosemite '71", font=hf, fill=(40, 45, 110))
    p = os.path.join(out, "12_scanned_polaroid_handwriting.tif")
    tifffile.imwrite(p, np.asarray(pil), photometric="rgb", compression="lzw", metadata=None,
                     resolution=(600, 600), resolutionunit="inch")
    made.append(p)

    # 13. date stamp over the photo (case D)
    img = to_dtype(natural(2000, 1500, 15), 8)
    pil = Image.fromarray(img)
    d = ImageDraw.Draw(pil)
    sf = font(70, ("DejaVuSansMono-Bold.ttf", "DejaVuSans-Bold.ttf"))
    d.text((1480, 1360), "'98 6 14", font=sf, fill=(255, 140, 20))
    p = os.path.join(out, "13_date_stamp.jpg")
    pil.save(p, quality=92)
    made.append(p)

    # 14. near-white sky at the top edge
    p = os.path.join(out, "14_near_white_sky.tif")
    tifffile.imwrite(p, to_dtype(natural(2400, 1600, 16, sky=True), 8), photometric="rgb", compression="lzw",
                     metadata=None)
    exiftool(["-XMP-dc:Title=Ocean Beach"], p)
    made.append(p)

    if big:
        # 600 MB 16-bit scan (12,000 px long edge)
        W, H = 12000, 8400
        p = os.path.join(out, "07c_big_16bit_scan.tif")
        base = natural(W // 4, H // 4, 17)
        arr = np.asarray(Image.fromarray(to_dtype(base, 8)).resize((W, H), Image.BILINEAR)).astype(np.uint16) * 257
        arr += np.random.default_rng(1).integers(0, 200, arr.shape, dtype=np.uint16)
        tifffile.imwrite(p, arr, photometric="rgb", compression=None, metadata=None, bigtiff=False,
                         resolution=(1200, 1200), resolutionunit="inch")
        exiftool(["-XMP-dc:Title=Big scan"], p)
        made.append(p)
    print("\n".join(made))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(args[0] if args else os.path.join(ROOT, "tests", "fixtures"), big="--big" in sys.argv)
