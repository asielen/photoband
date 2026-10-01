"""Metadata on output (review #6): ExifTool failure detection, the spawner
thread, XMP copied as a block, dimensions, regions (MWG/MP/IPTC, points,
circles, text that looks like numbers), subject area, Lightroom settings,
xmpMM, cross-format text fields and case A after a lossless rotation.

Sources are crafted with the exiftool CLI; saves go through save.save()."""
import math
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time

import numpy as np
import pytest
from PIL import Image, PngImagePlugin

from conftest import make_band_layout
from photoband import exiftool as E
from photoband import metawrite as MW
from photoband import record
from photoband.exiftool import ExifData, ExifToolError, check_write
from photoband.imageio import ImageInfo, load_upright
from photoband.save import SaveRequest, save
from photoband.settings import load_settings

pytestmark = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")

XMP = """<?xpacket begin='' id='W5M0MpCehiHzreSzNTczkc9d'?>
<x:xmpmeta xmlns:x='adobe:ns:meta/'><rdf:RDF xmlns:rdf='http://www.w3.org/1999/02/22-rdf-syntax-ns#'>
 <rdf:Description rdf:about=''
  xmlns:dc='http://purl.org/dc/elements/1.1/' xmlns:xmp='http://ns.adobe.com/xap/1.0/'
  xmlns:xmpMM='http://ns.adobe.com/xap/1.0/mm/' xmlns:stEvt='http://ns.adobe.com/xap/1.0/sType/ResourceEvent#'
  xmlns:crs='http://ns.adobe.com/camera-raw-settings/1.0/' xmlns:tiff='http://ns.adobe.com/tiff/1.0/'
  xmlns:exif='http://ns.adobe.com/exif/1.0/' xmlns:xmpGImg='http://ns.adobe.com/xap/1.0/g/img/'
  xmlns:mwg-rs='http://www.metadataworkinggroup.com/schemas/regions/'
  xmlns:stDim='http://ns.adobe.com/xap/1.0/sType/Dimensions#' xmlns:stArea='http://ns.adobe.com/xmp/sType/Area#'
  xmlns:MP='http://ns.microsoft.com/photo/1.2/' xmlns:MPRI='http://ns.microsoft.com/photo/1.2/t/RegionInfo#'
  xmlns:MPReg='http://ns.microsoft.com/photo/1.2/t/Region#'
  xmlns:Iptc4xmpExt='http://iptc.org/std/Iptc4xmpExt/2008-02-29/'
  xmlns:acme='http://acme.example/ns/1.0/'
  acme:SecretSauce='custom-ns-value'
  xmpMM:DocumentID='xmp.did:SRC-DOC' xmpMM:InstanceID='xmp.iid:SRC-INST'
  crs:Exposure2012='+0.35' crs:HasSettings='True' crs:HasCrop='True'
  crs:CropTop='0.1' crs:CropLeft='0.1' crs:CropBottom='0.9' crs:CropRight='0.9' crs:CropAngle='0'
  tiff:Orientation='1' tiff:BitsPerSample='8' tiff:ImageWidth='400' tiff:ImageLength='300'>
  <dc:title><rdf:Alt><rdf:li xml:lang='x-default'>1.50</rdf:li></rdf:Alt></dc:title>
  <xmpMM:History><rdf:Seq><rdf:li rdf:parseType='Resource'><stEvt:action>saved</stEvt:action>
   <stEvt:instanceID>xmp.iid:SRC-INST</stEvt:instanceID></rdf:li></rdf:Seq></xmpMM:History>
  <xmp:Thumbnails><rdf:Alt><rdf:li rdf:parseType='Resource'><xmpGImg:format>JPEG</xmpGImg:format>
   <xmpGImg:image>/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//</xmpGImg:image></rdf:li></rdf:Alt></xmp:Thumbnails>
  <crs:MaskGroupBasedCorrections><rdf:Seq><rdf:li rdf:parseType='Resource'><crs:What>Correction</crs:What>
   <crs:CorrectionAmount>1</crs:CorrectionAmount></rdf:li></rdf:Seq></crs:MaskGroupBasedCorrections>
  <crs:PaintBasedCorrections><rdf:Seq><rdf:li rdf:parseType='Resource'><crs:What>Correction</crs:What>
   </rdf:li></rdf:Seq></crs:PaintBasedCorrections>
  <mwg-rs:Regions rdf:parseType='Resource'>
   <mwg-rs:AppliedToDimensions stDim:w='400' stDim:h='300' stDim:unit='pixel'/>
   <mwg-rs:RegionList><rdf:Bag>
    <rdf:li rdf:parseType='Resource'><mwg-rs:Name>1.50</mwg-rs:Name><mwg-rs:Description>007</mwg-rs:Description>
     <mwg-rs:Type>Face</mwg-rs:Type>
     <mwg-rs:Area stArea:x='0.175' stArea:y='0.333333' stArea:w='0.15' stArea:h='0.266667' stArea:unit='normalized'/></rdf:li>
    <rdf:li rdf:parseType='Resource'><mwg-rs:Type>Focus</mwg-rs:Type>
     <mwg-rs:Area stArea:x='0.5' stArea:y='0.5' stArea:unit='normalized'/></rdf:li>
    <rdf:li rdf:parseType='Resource'><mwg-rs:Type>Focus</mwg-rs:Type><mwg-rs:Name>zero</mwg-rs:Name>
     <mwg-rs:Area stArea:x='0.25' stArea:y='0.75' stArea:w='0' stArea:h='0' stArea:unit='normalized'/></rdf:li>
    <rdf:li rdf:parseType='Resource'><mwg-rs:Type>Face</mwg-rs:Type><mwg-rs:Name>Circle</mwg-rs:Name>
     <mwg-rs:Area stArea:x='0.5' stArea:y='0.25' stArea:d='0.1' stArea:unit='normalized'/></rdf:li>
   </rdf:Bag></mwg-rs:RegionList>
  </mwg-rs:Regions>
  <MP:RegionInfo rdf:parseType='Resource'><MPRI:Regions><rdf:Bag>
    <rdf:li rdf:parseType='Resource'><MPReg:PersonDisplayName>1e5</MPReg:PersonDisplayName>
     <MPReg:Rectangle>0.1, 0.2, 0.15, 0.266667</MPReg:Rectangle></rdf:li>
  </rdf:Bag></MPRI:Regions></MP:RegionInfo>
  <Iptc4xmpExt:ImageRegion><rdf:Bag>
   <rdf:li rdf:parseType='Resource'>
    <Iptc4xmpExt:RegionBoundary rdf:parseType='Resource'><Iptc4xmpExt:rbShape>rectangle</Iptc4xmpExt:rbShape>
     <Iptc4xmpExt:rbUnit>pixel</Iptc4xmpExt:rbUnit><Iptc4xmpExt:rbX>40</Iptc4xmpExt:rbX><Iptc4xmpExt:rbY>60</Iptc4xmpExt:rbY>
     <Iptc4xmpExt:rbW>60</Iptc4xmpExt:rbW><Iptc4xmpExt:rbH>80</Iptc4xmpExt:rbH></Iptc4xmpExt:RegionBoundary>
    <Iptc4xmpExt:Name><rdf:Alt><rdf:li xml:lang='x-default'>Ann</rdf:li></rdf:Alt></Iptc4xmpExt:Name>
   </rdf:li>
   <rdf:li rdf:parseType='Resource'>
    <Iptc4xmpExt:RegionBoundary rdf:parseType='Resource'><Iptc4xmpExt:rbShape>circle</Iptc4xmpExt:rbShape>
     <Iptc4xmpExt:rbUnit>relative</Iptc4xmpExt:rbUnit><Iptc4xmpExt:rbX>0.5</Iptc4xmpExt:rbX>
     <Iptc4xmpExt:rbY>0.5</Iptc4xmpExt:rbY><Iptc4xmpExt:rbRx>0.1</Iptc4xmpExt:rbRx></Iptc4xmpExt:RegionBoundary>
   </rdf:li>
   <rdf:li rdf:parseType='Resource'>
    <Iptc4xmpExt:RegionBoundary rdf:parseType='Resource'><Iptc4xmpExt:rbShape>polygon</Iptc4xmpExt:rbShape>
     <Iptc4xmpExt:rbUnit>relative</Iptc4xmpExt:rbUnit>
     <Iptc4xmpExt:rbVertices><rdf:Seq>
      <rdf:li rdf:parseType='Resource'><Iptc4xmpExt:rbX>0.1</Iptc4xmpExt:rbX><Iptc4xmpExt:rbY>0.1</Iptc4xmpExt:rbY></rdf:li>
      <rdf:li rdf:parseType='Resource'><Iptc4xmpExt:rbX>0.3</Iptc4xmpExt:rbX><Iptc4xmpExt:rbY>0.1</Iptc4xmpExt:rbY></rdf:li>
      <rdf:li rdf:parseType='Resource'><Iptc4xmpExt:rbX>0.2</Iptc4xmpExt:rbX><Iptc4xmpExt:rbY>0.3</Iptc4xmpExt:rbY></rdf:li>
     </rdf:Seq></Iptc4xmpExt:rbVertices></Iptc4xmpExt:RegionBoundary>
   </rdf:li>
  </rdf:Bag></Iptc4xmpExt:ImageRegion>
 </rdf:Description>
</rdf:RDF></x:xmpmeta>
<?xpacket end='w'?>"""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def cli(*args):
    r = subprocess.run(["exiftool", "-config", E.config_path(), *args], capture_output=True, text=True)
    return r.stdout + r.stderr


def md_of(path):
    return E.get().read_json(path, ["-a"])


def mkimg(path, w=400, h=300):
    yy, xx = np.mgrid[0:h, 0:w]
    a = np.zeros((h, w, 3), np.uint8)
    a[..., 0] = xx * 255 // w
    a[..., 1] = yy * 255 // h
    a[..., 2] = 120
    a[60:140, 40:100] = (255, 0, 0)
    Image.fromarray(a).save(path, **({"quality": 95} if path.endswith(".jpg") else {}))
    return path


def rich(tmp_path, ext, exif=True):
    p = mkimg(str(tmp_path / f"src.{ext}"))
    x = tmp_path / "src.xmp"
    x.write_text(XMP)
    cli("-overwrite_original", f"-xmp<={x}", p)
    if exif:
        thumb = str(tmp_path / "thumb.jpg")
        Image.new("RGB", (40, 30), (200, 0, 0)).save(thumb)
        cli("-overwrite_original", "-m", "-IFD0:Make=Canon", "-IFD0:Orientation#=1",
            "-ExifIFD:ExifImageWidth=400", "-ExifIFD:ExifImageHeight=300", "-ExifIFD:SubjectArea=200 150 50 40",
            "-IPTC:Keywords=picnic", f"-ThumbnailImage<={thumb}", p)
    return p


def do_save(path, fmt=None, source_rect=None):
    s = load_settings()
    s["saving"]["onExists"] = "overwrite"
    if fmt:
        s["saving"]["outputFormat"] = fmt
    arr, info = load_upright(path)
    sr = source_rect or [0, 0, arr.shape[1], arr.shape[0]]
    layout, tiles = make_band_layout(sr[2], sr[3], source_rect=sr)
    state = {"templateId": "classic-polaroid", "template": {"id": "classic-polaroid"},
             "blocks": [{"id": "people", "text": "Ann, Bea and Carl", "custom": False}], "overrides": {}}
    res = save(SaveRequest(path=path, mode="copy", layout=layout, tiles=tiles, state=state, settings=s))
    assert res.ok, res.error
    return res, layout


def info_for(w=400, h=300, orientation=1, fmt="JPEG"):
    return ImageInfo(path="x", format=fmt, width=w, height=h, channels=3, dtype="uint8", mode="RGB",
                     orientation=orientation)


# --------------------------------------------------------------------------
# 1. failure detection, BigTIFF, post-write verification
# --------------------------------------------------------------------------

def test_check_write_parses_summary():
    check_write("    1 image files updated\n", "")
    check_write("", "    1 image files updated\n")  # -json import: summary on stderr
    with pytest.raises(ExifToolError):
        check_write("    0 image files updated\n    1 files weren't updated due to errors\n",
                    "Error: File not found - x.jpg\n")
    with pytest.raises(ExifToolError):
        check_write("    0 image files updated\n    1 image files unchanged\n", "")
    check_write("    0 image files updated\n    1 image files unchanged\n", "", require_change=False)
    with pytest.raises(ExifToolError, match="No SourceFile"):
        check_write("", "No SourceFile 'a.jpg' in imported JSON database\n")
    with pytest.raises(ExifToolError, match="Nothing to do"):
        check_write("Nothing to do.\n", "Warning: Tag 'Foo' is not defined\n")
    with pytest.raises(ExifToolError):
        check_write("", "")


def test_write_failures_raise(tmp_path):
    et = E.get()
    with pytest.raises(ExifToolError):
        et.write(str(tmp_path / "missing.jpg"), ["-XMP-dc:Title=x"])
    bad = tmp_path / "corrupt.jpg"
    bad.write_bytes(b"\xff\xd8garbage")
    with pytest.raises(ExifToolError):
        et.write(str(bad), ["-XMP-dc:Title=x"])
    p = mkimg(str(tmp_path / "a.jpg"))
    j = tmp_path / "j.json"
    j.write_text('[{"SourceFile": "/elsewhere.jpg", "XMP-dc:Title": "x"}]')
    with pytest.raises(ExifToolError):
        et.write(p, [f"-json={j}"])
    j.write_text('[{"SourceFile": "*", "XMP-dc:Title": "x"}]')
    et.write(p, [f"-json={j}"])
    assert md_of(p)["XMP-dc:Title"] == "x"


def test_bigtiff_output_fails_cleanly(tmp_path, monkeypatch):
    import tifffile
    orig = tifffile.imwrite

    def imwrite(*a, **k):
        k["bigtiff"] = True
        return orig(*a, **k)

    monkeypatch.setattr(tifffile, "imwrite", imwrite)
    p = mkimg(str(tmp_path / "b.tif"))
    s = load_settings()
    arr, info = load_upright(p)
    layout, tiles = make_band_layout(arr.shape[1], arr.shape[0])
    res = save(SaveRequest(path=p, mode="copy", layout=layout, tiles=tiles, state={}, settings=s))
    assert not res.ok and res.code == "metadata"
    assert "4 GB" in res.error


def test_verification_rejects_missing_record(tmp_path):
    p = mkimg(str(tmp_path / "v.jpg"))
    rec = {"photoHash": "abc", "canvas": [400, 300]}
    with pytest.raises(MW.MetadataError, match="record"):
        MW._verify(E.get(), p, "JPEG", (400, 300), rec)
    E.get().write(p, [f"-XMP-photoband:Record={record.encode(rec)}"])
    MW._verify(E.get(), p, "JPEG", (400, 300), rec)
    with pytest.raises(MW.MetadataError, match="instead of"):
        MW._verify(E.get(), p, "JPEG", (401, 300), rec)


# --------------------------------------------------------------------------
# 2. process lifetime (Linux PR_SET_PDEATHSIG)
# --------------------------------------------------------------------------

@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux parent-death signal")
def test_exiftool_outlives_the_thread_that_started_it(tmp_path):
    p = mkimg(str(tmp_path / "t.jpg"))
    et = E.ExifTool()
    try:
        t = threading.Thread(target=lambda: et.read_json(p))
        t.start()
        t.join()
        time.sleep(0.3)
        assert et._proc.poll() is None
        out = {}
        t2 = threading.Thread(target=lambda: out.update(md=et.read_json(p)))
        t2.start()
        t2.join()
        assert out["md"]["File:ImageWidth"] == 400
    finally:
        et.close()


def _alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux parent-death signal")
def test_hard_killed_app_leaves_no_exiftool(tmp_path):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = mkimg(str(tmp_path / "k.jpg"))
    script = textwrap.dedent(f"""
        import os, sys, threading
        sys.path.insert(0, {root!r})
        from photoband import exiftool as E
        et = E.ExifTool()
        t = threading.Thread(target=lambda: et.read_json({p!r}))  # started from a worker thread
        t.start(); t.join()
        print(et._proc.pid, flush=True)
        os._exit(0)
    """)
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60,
                       env={**os.environ})
    pid = int(r.stdout.strip().splitlines()[-1])
    for _ in range(50):
        if not _alive(pid):
            break
        time.sleep(0.1)
    assert not _alive(pid), "ExifTool stayed running after the app was killed"
    ps = subprocess.run(["ps", "-o", "pid=", "-p", str(pid)], capture_output=True, text=True).stdout
    assert ps.strip() == "" or not _alive(pid)


# --------------------------------------------------------------------------
# 20. wrapper robustness
# --------------------------------------------------------------------------

def test_file_names_with_line_breaks_and_dashes(tmp_path, monkeypatch):
    et = E.get()
    with pytest.raises(ExifToolError, match="line breaks"):
        et.read_json(str(tmp_path / "a\nb.jpg"))
    mkimg(str(tmp_path / "-foo.jpg"))
    monkeypatch.chdir(tmp_path)
    assert et.read_json("-foo.jpg")["File:ImageWidth"] == 400


def test_config_written_atomically(tmp_path, monkeypatch):
    from photoband import paths
    p = E.config_path()
    with open(p, "w") as fh:
        fh.write("# stale\n")
    assert E.config_path() == p
    assert open(p).read() == E.CONFIG
    assert not [f for f in os.listdir(os.path.dirname(p)) if f.endswith(".tmp")]


# --------------------------------------------------------------------------
# full saves: 3, 4, 5-7, 9, 10, 14, 15, 19
# --------------------------------------------------------------------------

@pytest.mark.parametrize("src_ext,fmt", [("jpg", "JPEG"), ("jpg", "TIFF"), ("tif", "PNG"), ("png", "JPEG")])
def test_full_save_metadata(tmp_path, src_ext, fmt):
    src = rich(tmp_path, src_ext, exif=src_ext != "png")
    res, layout = do_save(src, fmt)
    md = md_of(res.out_path)
    px, py, pw, ph = layout["photoRect"]
    Wc, Hc = layout["canvas"]
    # 3: custom namespace survives; stale XMP is gone
    assert md.get("XMP-acme:SecretSauce") == "custom-ns-value"
    for k in ("XMP-tiff:Orientation", "XMP-tiff:BitsPerSample", "XMP-xmp:Thumbnails"):
        assert k not in md, k
    assert not any(k.endswith(("ThumbnailImage", "PhotoshopThumbnail")) or k.startswith("IFD1:") for k in md)
    # record
    rec = record.from_metadata(md)
    assert rec and rec["canvas"] == [Wc, Hc]
    # 4: dimension tags updated where they existed
    assert (md["XMP-tiff:ImageWidth"], md["XMP-tiff:ImageHeight"]) == (Wc, Hc)
    if src_ext != "png":
        assert (md["ExifIFD:ExifImageWidth"], md["ExifIFD:ExifImageHeight"]) == (Wc, Hc)
        # 9: subject area remapped (centre 200,150 → canvas)
        assert md["ExifIFD:SubjectArea"] == f"{200 + px} {150 + py} 50 40"
    # 5: number-looking text kept verbatim
    assert md.text["XMP-dc:Title"] == "1.50"
    regs = md.text["XMP-mwg-rs:RegionInfo"]["RegionList"]
    assert regs[0]["Name"] == "1.50" and regs[0]["Description"] == "007"
    assert md.text["XMP-MP:RegionInfoMP"]["Regions"][0]["PersonDisplayName"] == "1e5"
    # MWG face box
    a = md["XMP-mwg-rs:RegionInfo"]["RegionList"][0]["Area"]
    assert a["X"] == pytest.approx((px + 70) / Wc, abs=1e-5) and a["W"] == pytest.approx(60 / Wc, abs=1e-5)
    # 7: point, zero-size point and circle remapped
    pt = md["XMP-mwg-rs:RegionInfo"]["RegionList"][1]["Area"]
    assert (pt["X"], pt["Y"]) == (pytest.approx((px + 200) / Wc, abs=1e-5), pytest.approx((py + 150) / Hc, abs=1e-5))
    z = md["XMP-mwg-rs:RegionInfo"]["RegionList"][2]["Area"]
    assert z["W"] == 0 and z["X"] == pytest.approx((px + 100) / Wc, abs=1e-5)
    c = md["XMP-mwg-rs:RegionInfo"]["RegionList"][3]["Area"]
    assert c["D"] == pytest.approx(40 / Wc, abs=1e-5)
    # 6: IPTC regions
    ir = md["XMP-iptcExt:ImageRegion"]
    assert (ir[0]["RegionBoundary"]["RbX"], ir[0]["RegionBoundary"]["RbY"]) == (40 + px, 60 + py)
    assert ir[1]["RegionBoundary"]["RbX"] == pytest.approx((px + 200) / Wc, abs=1e-5)
    assert ir[1]["RegionBoundary"]["RbRx"] == pytest.approx(40 / Wc, abs=1e-5)
    v0 = ir[2]["RegionBoundary"]["RbVertices"][0]
    assert (v0["RbX"], v0["RbY"]) == (pytest.approx((px + 40) / Wc, abs=1e-5), pytest.approx((py + 30) / Hc, abs=1e-5))
    # 10: Lightroom crop off, local corrections gone, global settings kept
    assert md["XMP-crs:HasCrop"] in (False, "False")
    assert not any(k.startswith("XMP-crs:Crop") for k in md)
    assert "XMP-crs:MaskGroupBasedCorrections" not in md and "XMP-crs:PaintBasedCorrections" not in md
    assert md.text["XMP-crs:Exposure2012"] == "+0.35"
    assert any("Lightroom crop" in n for n in res.notes)
    # 14: new instance, same document, history appended
    assert md["XMP-xmpMM:DocumentID"] == "xmp.did:SRC-DOC"
    assert md["XMP-xmpMM:InstanceID"].startswith("xmp.iid:") and md["XMP-xmpMM:InstanceID"] != "xmp.iid:SRC-INST"
    hist = md["XMP-xmpMM:History"]
    assert hist[0]["InstanceID"] == "xmp.iid:SRC-INST"
    assert hist[-1]["InstanceID"] == md["XMP-xmpMM:InstanceID"] and hist[-1]["SoftwareAgent"].startswith("Photoband")
    # the caption is never written into a text field
    assert "Ann, Bea" not in str({k: v for k, v in md.items() if k != "XMP-photoband:Record"})


def test_no_exif_ifd_created_for_dimensions(tmp_path):
    p = mkimg(str(tmp_path / "plain.jpg"))
    cli("-overwrite_original", "-XMP-dc:Title=T", p)
    res, _ = do_save(p)
    md = md_of(res.out_path)
    assert not any(k.startswith("ExifIFD:") for k in md)
    assert "XMP-exif:ExifImageWidth" not in md
    assert record.from_metadata(md)


def test_iptc_digest_kept(tmp_path):
    p = mkimg(str(tmp_path / "d.jpg"))
    cli("-overwrite_original", "-IPTC:Keywords=picnic", "-Photoshop:IPTCDigest=new", p)
    res, _ = do_save(p, "TIFF")
    md = md_of(res.out_path)
    assert md.get("Photoshop:IPTCDigest") and md.get("IPTC:Keywords") == "picnic"


def test_dropped_named_regions_are_reported(tmp_path):
    src = rich(tmp_path, "jpg")
    res, _ = do_save(src, "JPEG", source_rect=[150, 0, 250, 300])
    md = md_of(res.out_path)
    names = [r.get("Name") for r in md["XMP-mwg-rs:RegionList"] if isinstance(r, dict)] if "XMP-mwg-rs:RegionList" in md \
        else [r.get("Name") for r in md["XMP-mwg-rs:RegionInfo"]["RegionList"]]
    assert 1.5 not in names and "1.50" not in names and "zero" not in names
    assert any("1.50" in n for n in res.notes) and any("zero" in n for n in res.notes)
    assert "XMP-MP:RegionInfoMP" not in md or not md["XMP-MP:RegionInfoMP"].get("Regions")
    assert any("Ann" in n for n in res.notes)  # IPTC region
    assert "ExifIFD:SubjectArea" in md  # 200,150 is still inside


def test_tifffile_leftovers_removed(tmp_path):
    p = mkimg(str(tmp_path / "tf.tif"))
    cli("-overwrite_original", '-IFD0:ImageDescription={"shape": [300, 400, 3]}', "-IFD0:Software=tifffile.py", p)
    res, _ = do_save(p, "TIFF")
    md = md_of(res.out_path)
    assert "IFD0:ImageDescription" not in md and md.get("IFD0:Software") != "tifffile.py"
    q = mkimg(str(tmp_path / "keep.tif"))
    cli("-overwrite_original", "-IFD0:ImageDescription=Grandma 1952", "-IFD0:Software=Photoshop", q)
    res, _ = do_save(q, "TIFF")
    md = md_of(res.out_path)
    assert md["IFD0:ImageDescription"] == "Grandma 1952" and md["IFD0:Software"] == "Photoshop"


# --------------------------------------------------------------------------
# 8. cross-format text fields, PNG custom text
# --------------------------------------------------------------------------

def _png_with_text(path):
    info = PngImagePlugin.PngInfo()
    for k, v in (("Title", "PNG title"), ("Description", "PNG desc\nline 2"), ("Comment", "PNG comment"),
                 ("MyKey", "custom value")):
        info.add_text(k, v)
    info.add_itxt("Other Key", "itxt val")
    Image.open(mkimg(path)).save(path, pnginfo=info)
    return path


def test_png_text_to_jpeg_and_custom_png_keys(tmp_path):
    p = _png_with_text(str(tmp_path / "t.png"))
    res, _ = do_save(p, "JPEG")
    md = md_of(res.out_path)
    assert md["XMP-dc:Title"] == "PNG title"
    assert md["XMP-dc:Description"] == "PNG desc\nline 2"
    assert md["File:Comment"] == "PNG comment"
    res, _ = do_save(p, "PNG")
    md = md_of(res.out_path)
    assert md["PNG:MyKey"] == "custom value" and md["PNG:OtherKey"] == "itxt val"
    assert md["PNG:Title"] == "PNG title"
    assert "XMP-dc:Title" not in md


def test_xmp_title_wins_over_png_title(tmp_path):
    p = _png_with_text(str(tmp_path / "t.png"))
    cli("-overwrite_original", "-XMP-dc:Title=XMP title", p)
    res, _ = do_save(p, "TIFF")
    md = md_of(res.out_path)
    assert md["XMP-dc:Title"] == "XMP title"
    assert md["XMP-exif:UserComment"] == "PNG comment"


def test_jpeg_comment_to_png(tmp_path):
    p = mkimg(str(tmp_path / "c.jpg"))
    cli("-overwrite_original", "-Comment=JPEG COM text", p)
    res, _ = do_save(p, "PNG")
    assert md_of(res.out_path)["PNG:Comment"] == "JPEG COM text"
    res, _ = do_save(p, "JPEG")
    assert md_of(res.out_path)["File:Comment"] == "JPEG COM text"


# --------------------------------------------------------------------------
# region maths (no ExifTool)
# --------------------------------------------------------------------------

def _md(d):
    return ExifData(d, text=d)


def test_mwg_rotation_for_mirrored_orientations():
    md = _md({"XMP-mwg-rs:RegionInfo": {"RegionList": [
        {"Name": "A", "Rotation": "0.5", "Area": {"X": "0.5", "Y": "0.5", "W": "0.2", "H": "0.1", "Unit": "normalized"}}]}})
    for o, sign in ((1, 1), (3, 1), (6, 1), (8, 1), (2, -1), (4, -1), (5, -1), (7, -1)):
        info = info_for(400, 300, o)
        Wu, Hu = info.upright_size
        upd = MW.build_region_updates(md, info, (0, 0, Wu, Hu), (0, 0, Wu, Hu), (Wu, Hu))
        r = upd["XMP-mwg-rs:RegionInfo"]["RegionList"][0]
        assert float(r["Rotation"]) == pytest.approx(0.5 * sign), o
        if o in (5, 6, 7, 8):  # w and h swap with the frame
            assert r["Area"]["W"] == pytest.approx(0.1 * 300 / 300, abs=1e-6)


def test_mp_uses_file_orientation_even_with_upright_mwg():
    # MWG says "upright frame" (ATD matches upright), MP has no ATD: stored frame
    md = _md({"XMP-mwg-rs:RegionInfo": {"AppliedToDimensions": {"W": "300", "H": "400", "Unit": "pixel"},
                                        "RegionList": [{"Name": "A", "Area": {"X": "0.5", "Y": "0.5", "W": "0.1",
                                                                              "H": "0.1", "Unit": "normalized"}}]},
              "XMP-MP:RegionInfoMP": {"Regions": [{"PersonDisplayName": "A", "Rectangle": "0.1, 0.2, 0.2, 0.1"}]}})
    info = info_for(400, 300, 6)
    upd = MW.build_region_updates(md, info, (0, 0, 300, 400), (0, 0, 300, 400), (300, 400))
    x, y, w, h = (float(v) for v in upd["XMP-MP:RegionInfoMP"]["Regions"][0]["Rectangle"].split(","))
    # orientation 6: (1 - y - h, x, h, w)
    assert (x, y, w, h) == pytest.approx((0.7, 0.1, 0.1, 0.2), abs=1e-6)


def test_iptc_pixel_units_and_drop():
    md = _md({"XMP-iptcExt:ImageRegion": [
        {"Name": "In", "RegionBoundary": {"RbShape": "rectangle", "RbUnit": "pixel", "RbX": "10", "RbY": "10",
                                          "RbW": "20", "RbH": "20"}},
        {"Name": "Out", "RegionBoundary": {"RbShape": "circle", "RbUnit": "pixel", "RbX": "390", "RbY": "10",
                                           "RbRx": "5"}}]})
    notes = []
    upd = MW.build_region_updates(md, info_for(), (0, 0, 200, 300), (5, 7, 200, 300), (210, 400), notes)
    regs = upd["XMP-iptcExt:ImageRegion"]
    assert len(regs) == 1 and regs[0]["RegionBoundary"]["RbX"] == 15 and regs[0]["RegionBoundary"]["RbY"] == 17
    assert any("Out" in n for n in notes)


def test_subject_location_point_and_delete():
    md = _md({"ExifIFD:SubjectLocation": "100 100", "XMP-exif:SubjectArea": ["390", "290", "4", "4"]})
    upd, dels = MW.build_geometry_updates(md, info_for(), (50, 50, 200, 200), (10, 10, 200, 200), (220, 300))
    assert upd["ExifIFD:SubjectLocation"] == "60 60"
    assert "-XMP-exif:SubjectArea=" in dels


# --------------------------------------------------------------------------
# 16. case A after a lossless rotation elsewhere
# --------------------------------------------------------------------------

@pytest.mark.parametrize("ext", ["tif", "jpg"])
def test_case_a_after_orientation_flag_change(tmp_path, ext):
    from photoband.existing import analyze_existing
    p = mkimg(str(tmp_path / f"r.{ext}"))
    res, layout = do_save(p, "TIFF" if ext == "tif" else "JPEG")
    q = str(tmp_path / f"rot.{ext}")
    shutil.copy(res.out_path, q)
    cli("-overwrite_original", "-IFD0:Orientation#=6", q)
    arr, info = load_upright(q)
    ex = analyze_existing(arr, info, md_of(q), run_ocr=False)
    assert ex["case"] == "A", ex["warnings"]
    x, y, w, h = ex["sourceRect"]
    px, py, pw, ph = layout["photoRect"]
    Wc, Hc = layout["canvas"]
    # orientation 6 turns the canvas 90° clockwise: photo rect (Hc - py - ph, px, ph, pw)
    assert [x, y, w, h] == [Hc - py - ph, px, ph, pw]


def test_jpeg_extended_xmp_survives(tmp_path):
    src = rich(tmp_path, "jpg", exif=False)
    big = tmp_path / "big.txt"
    big.write_text("x" * 100000)
    cli("-overwrite_original", f"-XMP-dc:Description<={big}", src)
    assert "XMP-xmpNote:HasExtendedXMP" in md_of(src)
    res, _ = do_save(src, "JPEG")
    md = E.get().read_json(res.out_path)
    assert len(md["XMP-dc:Description"]) == 100000
    assert md.get("XMP-acme:SecretSauce") == "custom-ns-value"
    assert record.from_metadata(md)
