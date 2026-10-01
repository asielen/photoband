"""File-format edge cases for imageio / colors / proxy (review #5): small synthetic files,
each test well under a second."""
import io
import struct
import zlib

import numpy as np
import pytest
import tifffile
from PIL import Image, ImageCms

from conftest import make_band_layout
from photoband import imageio as pio
from photoband.colors import _is_srgb, icc_matches, srgb_to_file, to_display_srgb8
from photoband.composite import composite
from photoband.imageio import (ImageError, expected_output_pixels, is_tifffile_shape_description, probe,
                               read_pixels, write_image)

RNG = np.random.default_rng(42)


# --------------------------------------------------------------------------
# helpers: tiny ICC profiles and PNG files
# --------------------------------------------------------------------------

def _s15(v):
    return struct.pack(">i", int(round(v * 65536)))


def _xyz(x, y, z):
    return b"XYZ " + b"\0" * 4 + _s15(x) + _s15(y) + _s15(z)


def _curv(g):
    return b"curv" + b"\0" * 4 + struct.pack(">I", 1) + struct.pack(">H", int(round(g * 256)))


def _icc(space, tags, desc_text):
    a = desc_text.encode("ascii") + b"\0"
    desc = b"desc" + b"\0" * 4 + struct.pack(">I", len(a)) + a + b"\0" * 83
    tags = [(b"desc", desc)] + tags
    n = len(tags)
    offset = 128 + 4 + 12 * n
    table = data = b""
    for sig, body in tags:
        while (offset + len(data)) % 4:
            data += b"\0"
        table += sig + struct.pack(">II", offset + len(data), len(body))
        data += body
    hdr = struct.pack(">I", offset + len(data)) + b"lcms" + struct.pack(">I", 0x02100000) + b"mntr" + space
    hdr += b"XYZ " + b"\0" * 12 + b"acsp" + b"APPL" + b"\0" * 24 + _s15(0.9642) + _s15(1.0) + _s15(0.8249)
    hdr += b"lcms" + b"\0" * 44
    return hdr + struct.pack(">I", n) + table + data


D50 = _xyz(0.9642, 1.0, 0.8249)


def gray_icc(g=1.8):
    return _icc(b"GRAY", [(b"wtpt", D50), (b"kTRC", _curv(g))], "Gray Gamma %.1f" % g)


def prophoto_icc():
    return _icc(b"RGB ", [(b"wtpt", D50), (b"rXYZ", _xyz(0.7977, 0.2880, 0.0)), (b"gXYZ", _xyz(0.1352, 0.7119, 0.0)),
                          (b"bXYZ", _xyz(0.0313, 0.0001, 0.8249)),
                          (b"rTRC", _curv(1.8)), (b"gTRC", _curv(1.8)), (b"bTRC", _curv(1.8))], "ProPhoto test")


def linear_srgb_icc():
    return _icc(b"RGB ", [(b"wtpt", D50), (b"rXYZ", _xyz(0.4361, 0.2225, 0.0139)),
                          (b"gXYZ", _xyz(0.3851, 0.7169, 0.0971)), (b"bXYZ", _xyz(0.1431, 0.0606, 0.7141)),
                          (b"rTRC", _curv(1.0)), (b"gTRC", _curv(1.0)), (b"bTRC", _curv(1.0))], "sRGB linear")


SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _chunk(t, b):
    return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xffffffff)


def write_png_raw(path, rows_bytes, w, h, depth, ctype, extra=b""):
    raw = b"".join(b"\0" + r for r in rows_bytes)
    ihdr = struct.pack(">IIBBBBB", w, h, depth, ctype, 0, 0, 0)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + extra + _chunk(b"IDAT", zlib.compress(raw))
                 + _chunk(b"IEND", b""))


def png16(path, a, extra=b""):
    ctype = {1: 0, 2: 4, 3: 2, 4: 6}[a.shape[2]]
    write_png_raw(path, [a[y].astype(">u2").tobytes() for y in range(a.shape[0])], a.shape[1], a.shape[0],
                  16, ctype, extra)


def png_ihdr(path):
    with open(path, "rb") as fh:
        d = fh.read(33)
    return d[24], d[25]  # bit depth, color type


def png_chunk_types(path):
    d = open(path, "rb").read()
    i, out = 8, []
    while i < len(d):
        n = struct.unpack(">I", d[i:i + 4])[0]
        out.append(d[i + 4:i + 8])
        i += 12 + n
    return out


def tiff_tags(path):
    with tifffile.TiffFile(path) as tf:
        return {t.code: t.value for t in tf.pages[0].tags.values()}


def rt(tmp_path, src, fmt, name="out"):
    """write_image the source's own pixels, return (notes, out_info, out_pixels)."""
    info = probe(str(src))
    arr = read_pixels(info)
    out = tmp_path / (name + pio.EXT_FOR[fmt])
    notes = write_image(str(out), arr, info, fmt)
    oi = probe(str(out))
    return notes, oi, read_pixels(oi), arr, info


# --------------------------------------------------------------------------
# 1 MINISWHITE gray + alpha
# --------------------------------------------------------------------------

def test_miniswhite_alpha_not_inverted(tmp_path):
    p = tmp_path / "mw.tif"
    raw = np.dstack([np.full((50, 40), 50, np.uint8), np.full((50, 40), 200, np.uint8)])
    tifffile.imwrite(p, raw, photometric="miniswhite", extrasamples=["unassalpha"], metadata=None)
    info = probe(str(p))
    a = read_pixels(info)
    assert tuple(a[0, 0]) == (205, 200)  # gray inverted to black-is-zero, alpha untouched
    # band pixels written through composite + write_image: opaque alpha on disk
    lay, tiles = make_band_layout(40, 50)
    res = composite(a, info.icc, lay, tiles)
    out = tmp_path / "o.tif"
    write_image(str(out), res.canvas, info, "TIFF")
    with tifffile.TiffFile(out) as tf:
        assert int(tf.pages[0].photometric) == 0
        disk = tf.pages[0].asarray()
    x, y, w, h = lay["photoRect"]
    assert np.array_equal(disk[y:y + h, x:x + w], raw)
    assert tuple(disk[0, 0]) == (0, 255)  # white band: miniswhite 0, alpha opaque
    # PNG output is black-is-zero with the same alpha
    notes, oi, o, arr, _ = rt(tmp_path, p, "PNG")
    assert oi.mode == "LA" and np.array_equal(o, arr)


# --------------------------------------------------------------------------
# 2 YCbCr TIFF without JPEG compression
# --------------------------------------------------------------------------

def test_uncompressed_ycbcr_blocked_and_previewed_as_rgb(tmp_path):
    p = tmp_path / "ycc.tif"
    rgb = np.zeros((4, 4, 3), np.uint8)
    rgb[:] = (200, 40, 40)
    y = 0.299 * 200 + 0.587 * 40 + 0.114 * 40
    ycc = np.zeros_like(rgb)
    ycc[..., 0] = round(y)
    ycc[..., 1] = round((40 - y) / 1.772 + 128)
    ycc[..., 2] = round((200 - y) / 1.402 + 128)
    tifffile.imwrite(p, ycc, photometric="ycbcr", subsampling=(1, 1), metadata=None)
    info = probe(str(p))
    assert info.save_blocked and "YCbCr" in info.save_blocked
    a = read_pixels(info)
    assert np.abs(a[0, 0].astype(int) - (200, 40, 40)).max() <= 2


# --------------------------------------------------------------------------
# 3 / 4 16-bit gray + alpha PNG
# --------------------------------------------------------------------------

def test_png16_gray_alpha_roundtrip(tmp_path):
    a = RNG.integers(0, 65536, (7, 9, 2), dtype=np.int64).astype(np.uint16)
    p = tmp_path / "la16.png"
    png16(p, a, _chunk(b"iCCP", b"g\0\0" + zlib.compress(gray_icc())))
    info = probe(str(p))
    assert (info.channels, info.dtype, info.mode) == (2, "uint16", "LA")
    assert np.array_equal(read_pixels(info), a)
    notes, oi, o, arr, _ = rt(tmp_path, p, "PNG")
    assert png_ihdr(tmp_path / "out.png") == (16, 4)
    assert np.array_equal(o, a)
    assert oi.icc == gray_icc()  # gray profile on gray+alpha output
    assert not any("RGBA" in n for n in notes)


def test_icc_space_mismatch_dropped(tmp_path):
    # gray profile on RGB pixels, RGB profile on gray pixels
    for icc, arr in ((gray_icc(), RNG.integers(0, 256, (4, 4, 3), dtype=np.uint8)),
                     (prophoto_icc(), RNG.integers(0, 256, (4, 4, 1), dtype=np.uint8))):
        info = pio.ImageInfo(path="x", format="PNG", width=4, height=4, channels=arr.shape[2], dtype="uint8",
                             mode="RGB", icc=icc)
        for fmt in ("PNG", "TIFF", "JPEG"):
            out = tmp_path / ("m" + pio.EXT_FOR[fmt])
            notes = write_image(str(out), arr, info, fmt)
            assert probe(str(out)).icc is None, fmt
            assert any("does not match" in n for n in notes)
    assert icc_matches(gray_icc(), 2) and not icc_matches(gray_icc(), 3)
    assert icc_matches(prophoto_icc(), 4) and not icc_matches(prophoto_icc(), 1)


# --------------------------------------------------------------------------
# 5 sRGB detection by content
# --------------------------------------------------------------------------

def test_srgb_detection_by_content():
    assert _is_srgb(None)
    assert _is_srgb(SRGB_ICC)
    lin = linear_srgb_icc()  # description says "sRGB" but the curve is linear
    assert not _is_srgb(lin)
    assert not _is_srgb(prophoto_icc())
    ref = ImageCms.profileToProfile(Image.new("RGB", (1, 1), (128, 128, 128)), ImageCms.createProfile("sRGB"),
                                    ImageCms.ImageCmsProfile(io.BytesIO(lin)), outputMode="RGB").getpixel((0, 0))
    got = tuple(int(v) for v in srgb_to_file("#808080", 3, np.uint8, lin))
    assert got == ref and got[0] < 100


# --------------------------------------------------------------------------
# 6 preview of gray files with a gray profile
# --------------------------------------------------------------------------

def test_display_gray_icc_color_managed():
    icc = gray_icc(1.8)
    a = np.full((2, 2, 1), 123, np.uint8)
    ref = ImageCms.profileToProfile(Image.new("L", (1, 1), 123), ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                                    ImageCms.createProfile("sRGB"), outputMode="RGB").getpixel((0, 0))
    d = to_display_srgb8(a, icc, "L")
    assert tuple(int(v) for v in d[0, 0]) == ref and ref[0] != 123
    # gray+alpha too; no profile stays as is
    assert tuple(to_display_srgb8(np.dstack([a, a]), icc, "LA")[0, 0]) == ref
    assert tuple(to_display_srgb8(a, None, "L")[0, 0]) == (123, 123, 123)


# --------------------------------------------------------------------------
# 7 probe notes reach the save log
# --------------------------------------------------------------------------

def test_conversion_notes_returned_by_write_image(tmp_path):
    a = RNG.integers(0, 256, (16, 16, 3), dtype=np.uint8)
    p = tmp_path / "mp.tif"
    with tifffile.TiffWriter(p, bigtiff=True) as tw:
        tw.write(a, photometric="rgb", tile=(16, 16), metadata=None)
        tw.write(a, photometric="rgb", metadata=None)
    notes, *_ = rt(tmp_path, p, "TIFF")
    j = " | ".join(notes)
    assert "other page was dropped" in j and "only the first page is shown" not in j
    assert "striped" in j and "BigTIFF" in j
    p = tmp_path / "planar.tif"
    b = RNG.integers(0, 256, (5, 6, 6, 5), dtype=np.uint8)[0]  # 6 samples, separate planes
    tifffile.imwrite(p, np.moveaxis(b, -1, 0)[:5].copy(), photometric="rgb", planarconfig="separate",
                     extrasamples=["unassalpha", "unspecified"], metadata=None)
    notes, oi, o, arr, info = rt(tmp_path, p, "TIFF")
    assert info.channels == 4 and info.extrasamples == (2,)
    assert any("interleaved" in n for n in notes) and any("dropped" in n for n in notes)
    assert oi.samples == 4 and np.array_equal(o, arr)


def test_palette_and_low_bit_notes(tmp_path):
    p = tmp_path / "pal.png"
    Image.fromarray(RNG.integers(0, 256, (8, 8, 3), dtype=np.uint8)).quantize(8).save(p)
    notes, oi, o, arr, info = rt(tmp_path, p, "PNG")
    assert info.palette and any("Palette" in n for n in notes) and np.array_equal(o, arr)
    # 4-bit gray
    p = tmp_path / "g4.png"
    vals = RNG.integers(0, 16, (3, 8), dtype=np.uint8)
    rows = [bytes((int(r[i]) << 4) | int(r[i + 1]) for i in range(0, 8, 2)) for r in vals]
    write_png_raw(p, rows, 8, 3, 4, 0)
    notes, oi, o, arr, info = rt(tmp_path, p, "PNG")
    assert np.array_equal(arr[:, :, 0], vals * 17)
    assert any("4-bit" in n for n in notes) and oi.dtype == "uint8" and np.array_equal(o, arr)


# --------------------------------------------------------------------------
# 8 / 12 no tifffile JSON description, Software or default resolution
# --------------------------------------------------------------------------

def test_shape_description_helper():
    assert is_tifffile_shape_description('{"shape": [120, 90, 3]}')
    assert is_tifffile_shape_description(b'{"shape": [10, 10]}\0')
    assert not is_tifffile_shape_description("Scanned by Epson")
    assert not is_tifffile_shape_description('{"title": "x"}')
    assert not is_tifffile_shape_description(None)


def test_tiff_write_adds_no_description_software_or_resolution(tmp_path):
    a = RNG.integers(0, 256, (6, 5, 3), dtype=np.uint8)
    p = tmp_path / "src.png"
    Image.fromarray(a).save(p)  # no pHYs
    notes, oi, o, arr, info = rt(tmp_path, p, "TIFF")
    t = tiff_tags(tmp_path / "out.tif")
    for code in (270, 305, 282, 283, 296):
        assert code not in t, code
    assert np.array_equal(o, arr)
    # a source resolution is kept (TIFF: as stored, unit included)
    p = tmp_path / "cm.tif"
    tifffile.imwrite(p, a, photometric="rgb", resolution=(118.11, 118.11), resolutionunit="centimeter",
                     metadata=None)
    rt(tmp_path, p, "TIFF", "cm_out")
    t = tiff_tags(tmp_path / "cm_out.tif")
    assert int(t[296]) == 3 and abs(t[282][0] / t[282][1] - 118.11) < 1e-3 and 305 not in t


# --------------------------------------------------------------------------
# 9 / 10 PNG tRNS and gAMA
# --------------------------------------------------------------------------

def test_png_trns_becomes_alpha(tmp_path):
    g = RNG.integers(0, 256, (6, 6), dtype=np.uint8)
    g[0, 0] = 17
    p = tmp_path / "gt.png"
    Image.fromarray(g, "L").save(p, transparency=17)
    info = probe(str(p))
    a = read_pixels(info)
    assert info.mode == "LA" and np.array_equal(a[:, :, 0], g)
    assert np.array_equal(a[:, :, 1], np.where(g == 17, 0, 255))
    notes, oi, o, arr, _ = rt(tmp_path, p, "PNG")
    assert oi.mode == "LA" and np.array_equal(o, arr) and any("tRNS" in n for n in notes)
    # RGB 8-bit
    c = RNG.integers(0, 256, (5, 5, 3), dtype=np.uint8)
    c[1, 1] = (1, 2, 3)
    p = tmp_path / "rt.png"
    Image.fromarray(c).save(p, transparency=(1, 2, 3))
    a = read_pixels(probe(str(p)))
    assert a.shape[2] == 4 and np.array_equal(a[:, :, :3], c) and a[1, 1, 3] == 0 and a[0, 0, 3] == 255
    # gray 16-bit
    g16 = RNG.integers(0, 65536, (4, 4, 1), dtype=np.int64).astype(np.uint16)
    g16[2, 2] = 1234
    p = tmp_path / "g16t.png"
    png16(p, g16, _chunk(b"tRNS", struct.pack(">H", 1234)))
    a = read_pixels(probe(str(p)))
    assert a.shape[2] == 2 and np.array_equal(a[:, :, 0], g16[:, :, 0]) and a[2, 2, 1] == 0


def test_png_gama_kept(tmp_path):
    a = RNG.integers(0, 256, (5, 5, 3), dtype=np.uint8)
    p = tmp_path / "g.png"
    Image.fromarray(a).save(p)
    d = open(p, "rb").read()
    open(p, "wb").write(d[:33] + _chunk(b"gAMA", struct.pack(">I", 55556)) + d[33:])
    rt(tmp_path, p, "PNG")
    with Image.open(tmp_path / "out.png") as im:
        assert abs(im.info.get("gamma", 0) - 0.55556) < 1e-5
    # 16-bit writer keeps it too, and iCCP excludes sRGB
    a16 = RNG.integers(0, 65536, (3, 3, 3), dtype=np.int64).astype(np.uint16)
    p = tmp_path / "g16.png"
    png16(p, a16, _chunk(b"gAMA", struct.pack(">I", 45455)) + _chunk(b"sRGB", b"\0"))
    rt(tmp_path, p, "PNG", "o16")
    types = png_chunk_types(tmp_path / "o16.png")
    assert b"gAMA" in types and b"sRGB" in types


# --------------------------------------------------------------------------
# 13 TIFF codecs kept
# --------------------------------------------------------------------------

@pytest.mark.parametrize("comp,code", [("zstd", 50000), ("lzma", 34925), ("packbits", 32773)])
def test_tiff_codecs_kept(tmp_path, comp, code):
    a = RNG.integers(0, 65536, (8, 7, 3), dtype=np.int64).astype(np.uint16)
    p = tmp_path / "c.tif"
    tifffile.imwrite(p, a, photometric="rgb", compression=comp, metadata=None)
    notes, oi, o, arr, _ = rt(tmp_path, p, "TIFF")
    assert oi.compression_code == code and np.array_equal(o, a) and not notes


# --------------------------------------------------------------------------
# 14 16-bit band/text colors
# --------------------------------------------------------------------------

def test_16bit_colors_precise():
    icc = prophoto_icc()
    v16 = srgb_to_file("#8b1a1a", 3, np.uint16, icc).astype(int)
    v8 = srgb_to_file("#8b1a1a", 3, np.uint8, icc).astype(int)
    assert np.abs(v16 / 257 - v8).max() <= 1.0
    assert (v16 % 257 != 0).any()  # more than 8-bit precision
    assert tuple(srgb_to_file("#ffffff", 3, np.uint16, icc)) == (65535, 65535, 65535)
    assert tuple(srgb_to_file("#000000", 3, np.uint16, icc)) == (0, 0, 0)
    g16 = int(srgb_to_file("#808080", 1, np.uint16, gray_icc())[0])
    g8 = int(srgb_to_file("#808080", 1, np.uint8, gray_icc())[0])
    assert abs(g16 / 257 - g8) <= 1.0 and g16 % 257 != 0
    # no profile: luma computed in 16-bit
    assert int(srgb_to_file("#8b1a1a", 1, np.uint16, None)[0]) == round((0.299 * 139 + 0.587 * 26 + 0.114 * 26) * 257)
    # alpha channel stays opaque
    assert srgb_to_file("#8b1a1a", 4, np.uint16, icc)[3] == 65535


# --------------------------------------------------------------------------
# 15 / 16 JPEG output: rounding and alpha
# --------------------------------------------------------------------------

def test_jpeg_16bit_rounding(tmp_path):
    a = np.full((16, 16, 1), 1000, np.uint16)  # 1000/257 = 3.89 -> 4 (not 1000 >> 8 = 3)
    info = pio.ImageInfo(path="x", format="TIFF", width=16, height=16, channels=1, dtype="uint16", mode="L")
    assert int(expected_output_pixels(a, info, "JPEG")[0, 0, 0]) == 4
    out = tmp_path / "r.jpg"
    write_image(str(out), a, info, "JPEG")
    assert int(read_pixels(probe(str(out)))[5, 5, 0]) == 4


def test_jpeg_alpha_composited_over_white(tmp_path):
    a = np.zeros((16, 16, 4), np.uint8)
    a[:, :8, 3] = 0      # transparent black -> white
    a[:, 8:, 3] = 255    # opaque black stays black
    info = pio.ImageInfo(path="x", format="PNG", width=16, height=16, channels=4, dtype="uint8", mode="RGBA")
    exp = expected_output_pixels(a, info, "JPEG")
    assert exp.shape[2] == 3 and tuple(exp[0, 0]) == (255, 255, 255) and tuple(exp[0, 15]) == (0, 0, 0)
    out = tmp_path / "a.jpg"
    notes = write_image(str(out), a, info, "JPEG")
    o = read_pixels(probe(str(out)))
    assert o[4, 2].min() > 245 and o[4, 13].max() < 10 and any("white" in n for n in notes)
    # associated alpha: color is premultiplied, so 50% gray-over-white stays correct
    b = np.zeros((4, 4, 4), np.uint16)
    b[..., 3] = 32768
    b[..., :3] = 16384  # premultiplied 0.25 -> unpremultiplied 0.5
    info16 = pio.ImageInfo(path="x", format="TIFF", width=4, height=4, channels=4, dtype="uint16", mode="RGBA",
                           extrasamples=(1,))
    e = expected_output_pixels(b, info16, "JPEG")
    assert abs(int(e[0, 0, 0]) - round((0.25 + (1 - 32768 / 65535)) * 255)) <= 1
    # fully opaque alpha is just dropped
    c = np.full((4, 4, 2), 255, np.uint8)
    c[..., 0] = 77
    infoc = pio.ImageInfo(path="x", format="PNG", width=4, height=4, channels=2, dtype="uint8", mode="LA")
    assert np.array_equal(expected_output_pixels(c, infoc, "JPEG"), c[:, :, :1])


# --------------------------------------------------------------------------
# 17 damaged files
# --------------------------------------------------------------------------

def test_truncated_files_raise_image_error(tmp_path):
    a = RNG.integers(0, 256, (64, 48, 3), dtype=np.uint8)
    srcs = {}
    srcs["t.tif"] = lambda p: tifffile.imwrite(p, a, photometric="rgb", compression="zlib", metadata=None)
    srcs["p.png"] = lambda p: Image.fromarray(a).save(p)
    srcs["j.jpg"] = lambda p: Image.fromarray(a).save(p, quality=90)
    srcs["p16.png"] = lambda p: png16(p, a.astype(np.uint16) * 257)
    for name, make in srcs.items():
        p = tmp_path / name
        make(p)
        data = open(p, "rb").read()
        for cut in (len(data) // 2, 30):
            q = tmp_path / f"cut{cut}_{name}"
            q.write_bytes(data[:cut])
            with pytest.raises(ImageError):
                read_pixels(probe(str(q)))


# --------------------------------------------------------------------------
# end to end through save(): conversion notes land in the save log
# --------------------------------------------------------------------------

def test_save_log_gets_conversion_notes(tmp_path):
    from photoband.imageio import load_upright
    from photoband.save import SaveRequest, save
    from photoband.settings import load_settings
    a = np.dstack([RNG.integers(0, 256, (50, 40), dtype=np.uint8), np.full((50, 40), 255, np.uint8)])
    p = tmp_path / "mwpages.tif"
    with tifffile.TiffWriter(p) as tw:
        tw.write(a, photometric="miniswhite", extrasamples=["unassalpha"], tile=(16, 16), metadata=None)
        tw.write(a[:, :, 0], photometric="minisblack", metadata=None)
    s = load_settings()
    s["saving"]["allowMultipageSave"] = True
    s["saving"]["onExists"] = "overwrite"
    arr, info = load_upright(str(p))
    lay, tiles = make_band_layout(arr.shape[1], arr.shape[0])
    r = save(SaveRequest(path=str(p), mode="copy", layout=lay, tiles=tiles, settings=s, embed_marker=False,
                         state={"templateId": "t", "template": {"id": "t"}, "blocks": [], "overrides": {}}))
    assert r.ok, r.error
    log = " | ".join(r.notes)
    assert "other page was dropped" in log and "striped" in log
    with tifffile.TiffFile(r.out_path) as tf:
        disk = tf.pages[0].asarray()
        assert len(tf.pages) == 1 and int(tf.pages[0].photometric) == 0
    x, y, w, h = lay["photoRect"]
    assert np.array_equal(disk[y:y + h, x:x + w], a)
    assert disk[-1, 0, 1] == 255  # band alpha opaque
