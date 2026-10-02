"""Face regions written by Lightroom Classic on a rotated scan.

Lightroom normalizes MWG region boxes against the STORED pixels (the EXIF
orientation still has to be applied, as MWG says) but writes
AppliedToDimensions with the UPRIGHT size. Photoband used to read an upright
AppliedToDimensions as "these boxes are already upright", so on an orientation
6/8 scan the faces of a group photo were drawn as a small column down the left
side, x and y swapped and scaled by the wrong axis; captions ordered the names
by that wrong x, MP duplicates were not recognised and saved copies got
regions remapped from the wrong frame.

The fixtures copy the structure of the real file (210412-bag-woodbury-156.tif,
Lightroom Classic 14.5.1): IFD0 and XMP-tiff Orientation 6, stored 2898x4226,
AppliedToDimensions 4226x2898, per-region mwg-rs:Rotation, no stArea:unit,
crs crop fields at the full frame, xmp:CreatorTool naming Lightroom. Each face
is painted in its own colour in the upright image, so every check asks "does
the box land on that face's colour?" rather than comparing numbers with
numbers.
"""
import shutil
import subprocess

import numpy as np
import pytest
from PIL import Image

from captiontokens import resolve
from conftest import make_band_layout
from photoband import exiftool as E
from photoband.imageio import ImageInfo, load_upright, orient_box, probe, upright
from photoband.metadata import normalize, parse_regions, region_frame_orientation
from photoband.save import SaveRequest, save
from photoband.settings import load_settings

pytestmark = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")

# (name, X, Y, W, H) exactly as Lightroom wrote them in the real file: centre
# x/y and size, normalized to the stored 2898x4226 frame (orientation 6)
REAL_156 = [
    ("Oliver Newman", 0.28561, 0.36670, 0.05840, 0.04004),
    ("Eleanor \"Elly\" Drenth", 0.30271, 0.45459, 0.04701, 0.03223),
    ("Florence Woodbury", 0.32764, 0.24316, 0.05983, 0.04102),
    ("Robert Church", 0.24929, 0.68164, 0.05413, 0.03711),
    ("Roy Dodson", 0.60185, 0.40869, 0.06410, 0.04395),
    ("Roy Dodson Jr", 0.27279, 0.51123, 0.05556, 0.03809),
    ("elanor bowen", 0.30698, 0.30127, 0.05840, 0.04004),
    ("Francis Woodbury", 0.29637, 0.75289, 0.07523, 0.04137),
    ("Frances Woodbury", 0.28923, 0.61788, 0.08042, 0.03826),
    ("Harriett Woodbury", 0.33722, 0.42060, 0.06615, 0.02358),
    ("Robert Bowen", 0.62938, 0.55494, 0.06161, 0.03425),
]
# the real file: stored 2898x4226, shown 4226x2898
REAL_STORED = (2898, 4226)
# the fixtures: the same shape at a tenth of the size
STORED = (290, 423)
UP = (STORED[1], STORED[0])


def _truth():
    """Upright top-left boxes (the faces as a person sees them on the photo)."""
    out = []
    for n, x, y, w, h in REAL_156:
        out.append((n, orient_box((x - w / 2, y - h / 2, w, h), 6)))
    return out


TRUTH = _truth()
LEFT_TO_RIGHT = [n for n, b in sorted(TRUTH, key=lambda t: (t[1][0] + t[1][2] / 2, t[1][1] + t[1][3] / 2))]


def _color(i):
    # distinct, saturated, far from the grey background and from each other
    return ((37 * i + 60) % 256, (91 * i + 200) % 256, (53 * i + 20) % 256)


def _stored_areas(orientation):
    """MWG areas (centre x/y, w, h) in the stored frame for the truth faces."""
    inv = {6: 8, 8: 6}[orientation]
    out = []
    for n, b in TRUTH:
        x, y, w, h = orient_box(b, inv)
        out.append((n, x + w / 2, y + h / 2, w, h))
    return out


def _xmp(orientation, areas, atd, *, rotation=True, creator=True, mp=None):
    rot = {6: "3.14159", 8: "-3.14159"}[orientation]
    regs = []
    for n, x, y, w, h in areas:
        regs.append(f"""
      <rdf:li><rdf:Description {f'mwg-rs:Rotation="{rot}"' if rotation else ''}
        mwg-rs:Name="{n.replace('"', '&quot;')}" mwg-rs:Type="Face">
       <mwg-rs:Area stArea:h="{h:.5f}" stArea:w="{w:.5f}" stArea:x="{x:.5f}" stArea:y="{y:.5f}"/>
      </rdf:Description></rdf:li>""")
    mp_xml = ""
    if mp:
        lis = "".join(f"""<rdf:li rdf:parseType='Resource'><MPReg:PersonDisplayName>{n}</MPReg:PersonDisplayName>
           <MPReg:Rectangle>{x:.6f}, {y:.6f}, {w:.6f}, {h:.6f}</MPReg:Rectangle></rdf:li>""" for n, (x, y, w, h) in mp)
        mp_xml = f"<MP:RegionInfo rdf:parseType='Resource'><MPRI:Regions><rdf:Bag>{lis}</rdf:Bag></MPRI:Regions></MP:RegionInfo>"
    return f"""<?xpacket begin='' id='W5M0MpCehiHzreSzNTczkc9d'?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 7.0-c000 1.000000, 0000/00/00-00:00:00        ">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmlns:tiff="http://ns.adobe.com/tiff/1.0/"
    xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
    xmlns:mwg-rs="http://www.metadataworkinggroup.com/schemas/regions/"
    xmlns:stDim="http://ns.adobe.com/xap/1.0/sType/Dimensions#" xmlns:stArea="http://ns.adobe.com/xmp/sType/Area#"
    xmlns:MP='http://ns.microsoft.com/photo/1.2/' xmlns:MPRI='http://ns.microsoft.com/photo/1.2/t/RegionInfo#'
    xmlns:MPReg='http://ns.microsoft.com/photo/1.2/t/Region#'
   {'xmp:CreatorTool="Adobe Photoshop Lightroom 14.5.1 Classic (Windows)"' if creator else ''}
   tiff:Orientation="{orientation}"
   crs:CropTop="0" crs:CropLeft="0" crs:CropBottom="1" crs:CropRight="1" crs:CropAngle="0"
   crs:CropConstrainToWarp="0" crs:CropConstrainToUnitSquare="1" crs:RawFileName="scan.tif">
   <mwg-rs:Regions rdf:parseType="Resource">
    <mwg-rs:AppliedToDimensions stDim:w="{atd[0]}" stDim:h="{atd[1]}" stDim:unit="pixel"/>
    <mwg-rs:RegionList><rdf:Bag>{''.join(regs)}
    </rdf:Bag></mwg-rs:RegionList>
   </mwg-rs:Regions>
   {mp_xml}
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end='w'?>"""


def _cli(*args):
    r = subprocess.run(["exiftool", "-config", E.config_path(), *args], capture_output=True, text=True)
    return r.stdout + r.stderr


def _md(path):
    return E.get().read_json(path, ["-a"])


def make_scan(tmp_path, orientation=6, *, ext="tif", rotation=True, creator=True, mp=None, name="scan"):
    """A Lightroom-tagged scan: the upright picture has each face painted in its
    colour; the file stores it turned so that ``orientation`` makes it upright."""
    a = np.full((UP[1], UP[0], 3), 128, np.uint8)
    for i, (_, (x, y, w, h)) in enumerate(TRUTH):
        a[round(y * UP[1]):round((y + h) * UP[1]), round(x * UP[0]):round((x + w) * UP[0])] = _color(i)
    stored = upright(a, {6: 8, 8: 6}[orientation])
    assert stored.shape[:2] == (STORED[1], STORED[0])
    assert np.array_equal(upright(stored, orientation), a)
    p = str(tmp_path / f"{name}.{ext}")
    Image.fromarray(stored).save(p, **({"quality": 98} if ext == "jpg" else {}))
    x = tmp_path / f"{name}.xmp"
    # AppliedToDimensions: the UPRIGHT size, as Lightroom writes it
    x.write_text(_xmp(orientation, _stored_areas(orientation), UP, rotation=rotation, creator=creator, mp=mp),
                 encoding="utf-8")
    out = _cli("-overwrite_original", f"-xmp<={x}", p)
    assert "1 image files updated" in out, out
    out = _cli("-overwrite_original", f"-IFD0:Orientation#={orientation}",
               "-IFD0:Software=Adobe Photoshop Lightroom 14.5.1 Classic (Windows)", p)
    assert "1 image files updated" in out, out
    return p, a


def _on_face(img, box, i, tol=0):
    """The centre of ``box`` (normalized, upright) is face i's colour."""
    H, W = img.shape[:2]
    x, y, w, h = box
    px = img[int((y + h / 2) * H), int((x + w / 2) * W)][:3].astype(int)
    return np.abs(px - np.array(_color(i))).max() <= tol


# --------------------------------------------------------------------------
# the real numbers
# --------------------------------------------------------------------------

def test_real_lightroom_numbers_land_on_the_faces():
    """The values from the real file, read as a dict: the face boxes are square
    in the stored frame (Lightroom draws square faces) and stretched 2:1 when
    read as upright boxes, which is the scaled-wrong look of the bug."""
    md = {"XMP-xmp:CreatorTool": "Adobe Photoshop Lightroom 14.5.1 Classic (Windows)",
          "XMP-mwg-rs:RegionInfo": {
              "AppliedToDimensions": {"W": 4226, "H": 2898, "Unit": "pixel"},
              "RegionList": [{"Area": {"X": x, "Y": y, "W": w, "H": h}, "Name": n, "Rotation": 3.14159, "Type": "Face"}
                             for n, x, y, w, h in REAL_156]}}
    info = ImageInfo(path="x.tif", format="TIFF", width=REAL_STORED[0], height=REAL_STORED[1], channels=3,
                     dtype="uint8", mode="RGB", orientation=6)
    assert region_frame_orientation(md, info) == 6
    r = parse_regions(md, info)
    assert r["warnings"] == []
    Wu, Hu = info.upright_size
    for f, (n, b) in zip(r["named"], TRUTH):
        assert f["name"] == n
        assert f["box"] == pytest.approx(list(b), abs=1e-9)
        # square in pixels, like the faces Lightroom draws
        assert f["box"][2] * Wu == pytest.approx(f["box"][3] * Hu, rel=0.02) or n in (
            "Francis Woodbury", "Frances Woodbury", "Harriett Woodbury", "Robert Bowen")
    # the group stands across the upper half; two crouch in front
    ys = {f["name"]: f["box"][1] + f["box"][3] / 2 for f in r["named"]}
    xs = [f["box"][0] + f["box"][2] / 2 for f in r["named"]]
    assert max(xs) - min(xs) > 0.45
    assert all(v < 0.4 for k, v in ys.items() if k not in ("Roy Dodson", "Robert Bowen"))
    assert ys["Roy Dodson"] > 0.55 and ys["Robert Bowen"] > 0.55


def test_upright_atd_from_other_tools_still_means_upright_boxes():
    """Without a Lightroom signature an upright AppliedToDimensions still says
    the boxes are upright (the existing rule)."""
    md = {"XMP-mwg-rs:RegionInfo": {"AppliedToDimensions": {"W": 4226, "H": 2898, "Unit": "pixel"},
                                    "RegionList": [{"Area": {"X": .5, "Y": .3, "W": .05, "H": .07},
                                                    "Name": "A", "Type": "Face"}]}}
    info = ImageInfo(path="x.tif", format="TIFF", width=2898, height=4226, channels=3, dtype="uint8",
                     mode="RGB", orientation=6)
    assert region_frame_orientation(md, info) == 1
    assert parse_regions(md, info)["named"][0]["box"] == pytest.approx([0.475, 0.265, 0.05, 0.07])
    # Lightroom's per-region Rotation marks its stored-frame boxes
    md["XMP-mwg-rs:RegionInfo"]["RegionList"][0]["Rotation"] = 0
    assert region_frame_orientation(md, info) == 6
    del md["XMP-mwg-rs:RegionInfo"]["RegionList"][0]["Rotation"]
    # a Lightroom CreatorTool alone is no evidence about the regions: another editor may have
    # rewritten them (upright, as AppliedToDimensions says) and kept the file-level tag
    md["XMP-xmp:CreatorTool"] = "Adobe Photoshop Lightroom Classic 14.5.1 (Windows)"
    assert region_frame_orientation(md, info) == 1


# --------------------------------------------------------------------------
# real files: overlay, captions, MP, saved copies
# --------------------------------------------------------------------------

@pytest.mark.parametrize("orientation", [6, 8])
@pytest.mark.parametrize("sig", ["both", "rotation"])
def test_overlay_boxes_land_on_faces(tmp_path, orientation, sig):
    p, img = make_scan(tmp_path, orientation, rotation=sig != "creator", creator=sig != "rotation")
    info = probe(p)
    assert (info.width, info.height, info.orientation) == (STORED[0], STORED[1], orientation)
    norm = normalize(_md(p), info)
    assert norm["warnings"] == []  # the upright AppliedToDimensions is no "mismatch"
    faces = norm["faces"]["named"]
    assert [f["name"] for f in faces] == [n for n, _ in TRUTH]
    up, _ = load_upright(p, info)
    assert up.shape[:2] == (UP[1], UP[0])
    for i, f in enumerate(faces):
        assert f["box"] == pytest.approx(list(TRUTH[i][1]), abs=2e-5), f["name"]  # 5-decimal XMP
        assert _on_face(up, f["box"], i), f["name"]


def test_caption_order_and_rows_follow_the_faces(tmp_path):
    p, _ = make_scan(tmp_path, 6)
    fields = normalize(_md(p), probe(p))["fields"]
    from captiontokens.tokens import join_names
    assert resolve("{names}", fields).text == join_names(LEFT_TO_RIGHT)
    # the Preview overlay numbers faces with the same left-to-right rule
    assert LEFT_TO_RIGHT[0] == "Francis Woodbury" and LEFT_TO_RIGHT[-1] == "Florence Woodbury"
    # two crouch in front, everybody else stands in one row behind them
    rows = resolve("{names:rows}", fields).text
    front, back = rows.split("; ")
    assert front.endswith(": Robert Bowen and Roy Dodson")
    standing = [n for n in LEFT_TO_RIGHT if n not in ("Robert Bowen", "Roy Dodson")]
    assert back.endswith(": " + join_names(standing))


def test_mp_regions_on_the_same_faces_are_merged(tmp_path):
    """A Microsoft (MP) copy of some of the faces, in the stored frame like MP
    always is, is the same people as Lightroom's MWG faces: no duplicates."""
    inv = 8
    mp = [(n, orient_box(b, inv)) for n, b in TRUTH[:4]]
    mp.append(("Only In MP", orient_box((0.05, 0.05, 0.05, 0.07), inv)))
    p, img = make_scan(tmp_path, 6, mp=mp)
    r = parse_regions(_md(p), probe(p))
    names = [f["name"] for f in r["named"]]
    assert names == [n for n, _ in TRUTH] + ["Only In MP"]
    assert r["named"][-1]["source"] == "MP"
    assert r["named"][-1]["box"] == pytest.approx([0.05, 0.05, 0.05, 0.07], abs=1e-5)


@pytest.mark.parametrize("orientation", [6, 8])
def test_saved_copy_regions_land_on_the_same_faces(tmp_path, orientation):
    p, _ = make_scan(tmp_path, orientation)
    s = load_settings()
    s["saving"]["onExists"] = "overwrite"
    s["saving"]["outputFormat"] = "TIFF"
    arr, info = load_upright(p)
    layout, tiles = make_band_layout(arr.shape[1], arr.shape[0])
    state = {"templateId": "classic-polaroid", "template": {"id": "classic-polaroid"},
             "blocks": [{"id": "people", "text": "x", "custom": False}], "overrides": {}}
    res = save(SaveRequest(path=p, mode="copy", layout=layout, tiles=tiles, state=state, settings=s))
    assert res.ok, res.error
    out_info = probe(res.out_path)
    assert int(out_info.orientation or 1) == 1
    md = _md(res.out_path)
    atd = md["XMP-mwg-rs:RegionInfo"]["AppliedToDimensions"]
    assert (float(atd["W"]), float(atd["H"])) == tuple(float(v) for v in layout["canvas"])
    r = parse_regions(md, out_info)
    assert r["warnings"] == []
    out, _ = load_upright(res.out_path, out_info)
    assert [f["name"] for f in r["named"]] == [n for n, _ in TRUTH]
    px, py, pw, ph = layout["photoRect"]
    Wc, Hc = layout["canvas"]
    for i, f in enumerate(r["named"]):
        assert _on_face(out, f["box"], i), f["name"]
        # and exactly where the photo moved to on the canvas
        x, y, w, h = TRUTH[i][1]
        assert f["box"] == pytest.approx([(px + x * pw) / Wc, (py + y * ph) / Hc, w * pw / Wc, h * ph / Hc], abs=1e-5)
    # read back, the copy orders the caption the same way as the source
    assert resolve("{names}", normalize(md, out_info)["fields"]).text == resolve(
        "{names}", normalize(_md(p), probe(p))["fields"]).text


def test_transposed_dimensions_are_only_accepted_for_rotated_files():
    # an unrotated file has one frame: regions claiming the transposed size were made for another
    # image geometry and are flagged; a rotated file may name either frame (Lightroom: upright)
    md = {"XMP-mwg-rs:RegionInfo": {"AppliedToDimensions": {"W": 600, "H": 800, "Unit": "pixel"},
                                    "RegionList": [{"Area": {"X": .5, "Y": .3, "W": .05, "H": .07},
                                                    "Name": "A", "Type": "Face"}]}}
    flat = ImageInfo(path="x.jpg", format="JPEG", width=800, height=600, channels=3, dtype="uint8",
                     mode="RGB", orientation=1)
    assert "region dimensions mismatch" in parse_regions(md, flat)["warnings"]
    turned = ImageInfo(path="x.jpg", format="JPEG", width=800, height=600, channels=3, dtype="uint8",
                       mode="RGB", orientation=6)
    assert "region dimensions mismatch" not in parse_regions(md, turned)["warnings"]
    md["XMP-mwg-rs:RegionInfo"]["AppliedToDimensions"] = {"W": 800, "H": 600, "Unit": "pixel"}
    assert "region dimensions mismatch" not in parse_regions(md, flat)["warnings"]
