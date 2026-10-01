"""Regression tests for the metadata / image I/O review: % in source names, JPEG XMP
over 64 KB, DPI on quarter-turned images, the TIFF decode budget, huge IFD chains,
the probe cache, ExifTool deadlines, the memory probe and the bounded prefetch."""
import os
import shutil
import stat
import sys
import textwrap
import threading
import time

import numpy as np
import pytest
import tifffile
from PIL import Image

from photoband import decodegate, exiftool as E, imageio, metawrite as MW, photos
from photoband.exiftool import ExifToolError
from photoband.imageio import ImageError, probe

from test_metawrite import cli, do_save, md_of, mkimg

needs_et = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")


# --------------------------------------------------------------------------
# 1. % in the source file name
# --------------------------------------------------------------------------

@needs_et
@pytest.mark.parametrize("name", ["Grandma%20from%201950.jpg", "100%.jpg", "a%d%f%e%c%2f.jpg"])
def test_percent_in_source_name_keeps_metadata(tmp_path, name):
    p = mkimg(str(tmp_path / name))
    cli("-overwrite_original", "-Artist=Robert", "-Copyright=Family archive", "-XMP-dc:Title=Lake", p)
    # decoys a %-expansion could pick instead
    for decoy in ("Grandma0from1950.jpg", "100.jpg", "a.jpg"):
        mkimg(str(tmp_path / decoy))
        cli("-overwrite_original", "-Artist=WRONG", str(tmp_path / decoy))
    before = set(os.listdir(os.path.join(os.environ["PHOTOBAND_HOME"], "tmp"))) \
        if os.path.isdir(os.path.join(os.environ["PHOTOBAND_HOME"], "tmp")) else set()
    res, _ = do_save(p)
    md = md_of(res.out_path)
    assert md.get("IFD0:Artist") == "Robert"
    assert md.get("IFD0:Copyright") == "Family archive"
    assert md.get("XMP-dc:Title") == "Lake"
    after = set(os.listdir(os.path.join(os.environ["PHOTOBAND_HOME"], "tmp")))
    assert not [f for f in after - before if f.startswith("src-")], "temp link left behind"


def test_src_arg_links_and_plain_names(tmp_path):
    plain = str(tmp_path / "plain.jpg")
    open(plain, "wb").write(b"x")
    assert MW._src_arg(plain) == (plain, None)
    pct = str(tmp_path / "50%.jpg")
    open(pct, "wb").write(b"abc")
    arg, tmp = MW._src_arg(pct)
    try:
        assert "%" not in arg and arg == tmp and arg.endswith(".jpg")
        assert open(arg, "rb").read() == b"abc"
    finally:
        os.unlink(tmp)


def test_source_open_failure_is_fatal():
    with pytest.raises(MW.MetadataError):
        MW._check_open("Warning: Error opening file - /x/a.jpg\n")
    with pytest.raises(MW.MetadataError):
        MW._check_open("File '/x/a.jpg' does not exist for -tagsFromFile option\n")
    MW._check_open("Warning: No writable tags set from /x/a.jpg\n")


# --------------------------------------------------------------------------
# 2. JPEG output of a source with XMP over 64 KB
# --------------------------------------------------------------------------

@needs_et
def test_big_xmp_tiff_to_jpeg_keeps_xmp(tmp_path):
    src = str(tmp_path / "scan.tif")
    a = np.random.default_rng(1).integers(0, 255, (300, 400, 3)).astype(np.uint8)
    tifffile.imwrite(src, a)
    anc = [f"-XMP-photoshop:DocumentAncestors+=xmp.did:{i:08d}-aaaa-bbbb-cccc-dddddddddddd" for i in range(2500)]
    argf = tmp_path / "args.txt"
    argf.write_text("\n".join(["-overwrite_original", "-XMP-dc:Title=My Title", "-XMP-dc:Description=My cap",
                               "-XMP-dc:Subject=Beach", *anc, src]) + "\n")
    cli("-@", str(argf))
    assert len(cli("-xmp", "-b", src)) > 120_000
    res, _ = do_save(src, "JPEG")
    md = md_of(res.out_path)
    assert md.get("XMP-dc:Title") == "My Title"
    assert md.get("XMP-dc:Description") == "My cap"
    assert md.get("XMP-dc:Subject") in ("Beach", ["Beach"])
    # (ExifTool reads at most 1000 list items unless minor errors are ignored)
    full = E.get().read_json(res.out_path, ["-m"])
    assert len(full.get("XMP-photoshop:DocumentAncestors") or []) == 2500
    assert any("extended XMP" in n for n in res.notes)
    assert not any("too large" in n for n in res.notes)


def test_too_large_warning_is_fatal_and_dc_check():
    with pytest.raises(MW.MetadataError):
        MW._check_too_large("Warning: [minor] XMP block too large for JPEG segment! (169214 bytes)\n")
    MW._check_too_large("Warning: something else\n")
    src = {"XMP-dc:Title": "T", "XMP-dc:Subject": ["a"], "XMP-dc:Rights": "", "IFD0:Artist": "x"}
    assert MW._dc_missing(src, {"XMP-dc:Title": "T"}) == ["XMP-dc:Subject"]
    assert MW._dc_missing(src, {"XMP-dc:Title": "T", "XMP-dc:Subject": ["a"]}) == []


# --------------------------------------------------------------------------
# 3. DPI on orientations 5-8
# --------------------------------------------------------------------------

def _aniso_tiff(path, orientation=6):
    img = (np.random.default_rng(1).random((300, 400, 3)) * 255).astype(np.uint8)
    tifffile.imwrite(path, img, photometric="rgb", resolution=(600, 300), resolutionunit="inch",
                     extratags=[(274, "H", 1, orientation, True)])
    return path


@needs_et
@pytest.mark.parametrize("fmt", ["TIFF", "JPEG", "PNG"])
def test_dpi_swapped_for_quarter_turn(tmp_path, fmt):
    p = _aniso_tiff(str(tmp_path / "aniso.tif"))
    i = probe(p)
    assert i.dpi == (600.0, 300.0) and i.upright_dpi == (300.0, 600.0)
    assert i.to_json()["dpi"] == (300.0, 600.0)       # the UI reads dpi[0] as horizontal
    res, _ = do_save(p, fmt)
    o = probe(res.out_path)
    assert o.orientation == 1
    assert o.dpi == pytest.approx((300.0, 600.0), abs=0.6)
    md = md_of(res.out_path)
    if "IFD0:XResolution" in md:
        assert (md["IFD0:XResolution"], md["IFD0:YResolution"]) == (300, 600)


@needs_et
def test_dpi_jpeg_exif_resolution_swapped(tmp_path):
    p = mkimg(str(tmp_path / "o6.jpg"))
    cli("-overwrite_original", "-IFD0:Orientation#=6", "-IFD0:XResolution=600", "-IFD0:YResolution=300",
        "-IFD0:ResolutionUnit=inches", "-JFIF:XResolution=600", "-JFIF:YResolution=300",
        "-JFIF:ResolutionUnit=inches", "-XMP-tiff:XResolution=600", "-XMP-tiff:YResolution=300", p)
    res, _ = do_save(p, "JPEG")
    md = md_of(res.out_path)
    assert (md["IFD0:XResolution"], md["IFD0:YResolution"]) == (300, 600)
    assert (md["JFIF:XResolution"], md["JFIF:YResolution"]) == (300, 600)
    assert (md["XMP-tiff:XResolution"], md["XMP-tiff:YResolution"]) == (300, 600)


@needs_et
def test_dpi_unturned_kept(tmp_path):
    p = _aniso_tiff(str(tmp_path / "o1.tif"), orientation=1)
    res, _ = do_save(p, "TIFF")
    assert probe(res.out_path).dpi == (600.0, 300.0)


# --------------------------------------------------------------------------
# 4. TIFF decode budget
# --------------------------------------------------------------------------

def test_many_samples_rejected(tmp_path):
    p = str(tmp_path / "bomb.tif")
    tifffile.imwrite(p, np.zeros((64, 64, 64), np.uint8), photometric="minisblack", planarconfig="contig",
                     extrasamples=[0] * 63, compression="zlib")
    with pytest.raises(ImageError, match="64 samples per pixel"):
        probe(p)


def test_float_samples_rejected(tmp_path):
    p = str(tmp_path / "f64.tif")
    tifffile.imwrite(p, np.zeros((20, 30, 3), np.float64), photometric="rgb")
    with pytest.raises(ImageError, match="64-bit floating-point"):
        probe(p)


def test_budget_counts_every_decoded_sample(tmp_path):
    p = str(tmp_path / "x.tif")
    tifffile.imwrite(p, np.zeros((20, 30, 7), np.uint16), photometric="rgb", extrasamples=[2, 0, 0, 0])
    i = probe(p)
    assert i.channels == 4 and i.dropped_samples == 3
    assert decodegate.decoded_bytes(i) == 20 * 30 * 7 * 2 == i.decode_bytes


def test_one_bit_tiff_still_opens(tmp_path):
    p = str(tmp_path / "bw.tif")
    tifffile.imwrite(p, np.random.default_rng(0).random((40, 50)) > 0.5, photometric="minisblack")
    i = probe(p)
    assert i.save_blocked
    assert imageio.read_pixels(i).shape == (40, 50, 1)


def test_system_memory_with_and_without_psutil(monkeypatch):
    total, avail = decodegate.system_memory()
    assert total > 0 and 0 <= avail <= total * 1.01
    monkeypatch.setitem(sys.modules, "psutil", None)      # import psutil -> ImportError
    decodegate._fallback_cache.clear()
    t2, a2 = decodegate.system_memory()
    assert t2 > 0 and a2 >= 0
    monkeypatch.setattr(decodegate, "_system_memory_fallback", lambda: (_ for _ in ()).throw(AssertionError))
    assert decodegate.system_memory() == (t2, a2)        # cached for a moment


# --------------------------------------------------------------------------
# 5. huge IFD chains, probe cache
# --------------------------------------------------------------------------

def test_probe_many_ifds_is_fast(tmp_path):
    p = str(tmp_path / "many.tif")
    with tifffile.TiffWriter(p) as tw:
        tw.write(np.zeros((8, 8), np.uint8))
        for _ in range(5000):
            tw.write(np.zeros((1, 1), np.uint8), metadata=None, software=False)
    imageio.clear_probe_cache()
    t = time.perf_counter()
    i = probe(p)
    assert time.perf_counter() - t < 1.0
    assert i.pages > 1 and not i.pages_exact
    assert any("+ pages" in n for n in i.notes)
    assert any("+ pages were dropped" in n for n in imageio._source_notes(i))


def test_probe_counts_small_multipage_exactly(tmp_path):
    p = str(tmp_path / "three.tif")
    with tifffile.TiffWriter(p) as tw:
        for _ in range(3):
            tw.write(np.zeros((8, 8), np.uint8))
    i = probe(p)
    assert i.pages == 3 and i.pages_exact


def test_probe_cache_returns_copies_and_sees_changes(tmp_path):
    p = str(tmp_path / "c.png")
    Image.new("RGB", (30, 20)).save(p)
    a = probe(p)
    a.notes.append("mutated")
    b = probe(p)
    assert "mutated" not in b.notes and b.width == 30
    time.sleep(0.01)
    tmp = p + ".new"
    Image.new("RGB", (31, 20)).save(tmp, "PNG")
    os.replace(tmp, p)
    assert probe(p).width == 31


# --------------------------------------------------------------------------
# 6. ExifTool deadlines
# --------------------------------------------------------------------------

FAKE = textwrap.dedent("""\
    #!{py}
    import sys, time, json
    if "-ver" in sys.argv:
        print("12.76"); sys.exit(0)
    args = []
    for line in sys.stdin:
        a = line.rstrip("\\n")
        if a == "False" and args and args[-1] == "-stay_open":
            sys.exit(0)
        if a.startswith("-execute"):
            n = a[len("-execute"):]
            if any("hang" in x for x in args):
                time.sleep(10 ** 6)
            files = [x for x in args if x.startswith("/")]
            print(json.dumps([{{"SourceFile": f}} for f in files])); print("{{ready%s}}" % n); sys.stdout.flush()
            if any("slowerr" in x for x in args):
                time.sleep(3); sys.stderr.write("Warning: stale\\n")
            sys.stderr.write("{{ready_err%s}}\\n" % n); sys.stderr.flush(); args = []
        else:
            args.append(a)
""")


@pytest.fixture
def fake_et(tmp_path, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("shebang script")
    p = tmp_path / "exiftool"
    p.write_text(FAKE.format(py=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    et = E.ExifTool(str(p))
    yield et
    et._kill()


def test_stuck_command_times_out_and_instance_recovers(fake_et, monkeypatch):
    monkeypatch.setattr(E, "READ_TIMEOUT", 1.5)
    t = time.monotonic()
    with pytest.raises(ExifToolError, match="did not finish"):
        fake_et.read_json("/x/hang.jpg")
    assert time.monotonic() - t < 10
    assert fake_et.read_json("/x/ok.jpg")["SourceFile"] == "/x/ok.jpg"


def test_late_stderr_never_reaches_the_next_command(fake_et, monkeypatch):
    monkeypatch.setattr(E, "ERR_GRACE", 0.5)
    fake_et.execute("-j", files=["/x/slowerr.jpg"])
    out, err = fake_et.execute("-j", files=["/x/ok.jpg"])
    assert "stale" not in err


def test_close_does_not_hang_on_a_stuck_command(fake_et, monkeypatch):
    monkeypatch.setattr(E, "READ_TIMEOUT", 600.0)
    monkeypatch.setattr(E, "CLOSE_LOCK_TIMEOUT", 0.5)
    th = threading.Thread(target=lambda: pytest.raises(ExifToolError, fake_et.read_json, "/x/hang.jpg"),
                          daemon=True)
    th.start()
    time.sleep(1.0)
    t = time.monotonic()
    fake_et.close()
    assert time.monotonic() - t < 8
    th.join(10)
    assert not th.is_alive()


# --------------------------------------------------------------------------
# 8. tifffile shape description (the tested check is the one used)
# --------------------------------------------------------------------------

@needs_et
def test_compact_shape_description_removed(tmp_path):
    p = mkimg(str(tmp_path / "tf.tif"))
    cli("-overwrite_original", '-IFD0:ImageDescription={"shape":[300,400,3]}', p)
    res, _ = do_save(p, "TIFF")
    assert "IFD0:ImageDescription" not in md_of(res.out_path)


# --------------------------------------------------------------------------
# 12. bounded prefetch
# --------------------------------------------------------------------------

def test_prefetch_queue_is_bounded(monkeypatch):
    gate = threading.Event()
    done = []

    def slow(p):
        gate.wait(10)
        done.append(p)

    monkeypatch.setattr(photos, "_safe_proxy", slow)
    photos.prefetch([f"/n/{i}.jpg" for i in range(50)])
    time.sleep(0.2)
    pend = photos.prefetch_pending()
    assert len(pend) <= photos.PREFETCH_MAX
    assert pend[-1] == "/n/49.jpg"
    assert "/n/10.jpg" not in pend
    gate.set()
    deadline = time.time() + 10
    while photos.prefetch_pending() and time.time() < deadline:
        time.sleep(0.05)
    assert not photos.prefetch_pending()
    assert len(done) <= photos.PREFETCH_MAX + photos.PREFETCH_WORKERS
