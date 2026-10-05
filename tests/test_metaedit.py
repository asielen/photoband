"""Edited photo details (metaedit): what is written where, what the reader sees afterwards, and
the details-only save's safety (backups, guards, verification), through the real ExifTool."""
import json
import os
import shutil
import subprocess

import numpy as np
import pytest
from PIL import Image

from conftest import make_band_layout
from photoband import metaedit, save as savemod
from photoband.exiftool import get as et_get
from photoband.imageio import probe
from photoband.metadata import normalize
from photoband.save import SaveRequest, image_data_hash, save, save_details
from photoband.settings import load_settings

pytestmark = pytest.mark.skipif(shutil.which("exiftool") is None, reason="exiftool not installed")


def _img(path, size=(300, 200), orientation=None, fmt=None):
    rng = np.random.default_rng(3)
    a = rng.integers(0, 255, (size[1], size[0], 3), dtype=np.uint8)
    im = Image.fromarray(a)
    kw = {}
    if orientation:
        ex = Image.Exif()
        ex[0x0112] = orientation
        kw["exif"] = ex.tobytes()
    im.save(path, **({"quality": 90} if path.endswith(".jpg") else {}), **kw)
    return path


def _et(path, *args):
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-m", *args, path], check=True)


def _et_json(path, data):
    j = path + ".json"
    with open(j, "w", encoding="utf-8") as fh:
        json.dump([{"SourceFile": path, **data}], fh, ensure_ascii=False)
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-m", "-n", "-struct", f"-json={j}", path], check=True)
    os.unlink(j)


def _md(path):
    return et_get().read_json(path)


def L(v):
    """A list tag as a list (ExifTool gives one value as a plain value or a one-item list)."""
    return v if isinstance(v, list) else [v]


def _fields(path):
    return normalize(_md(path), probe(path))["fields"]


def _settings(**saving):
    s = load_settings()
    s["saving"]["location"] = "subfolder"
    s["saving"]["backupOriginals"] = False
    s["saving"].update(saving)
    return s


def _details(path, edits, **saving):
    r = save_details(path, edits, _settings(**saving))
    assert r.ok, (r.code, r.error)
    return r


# -- validation ------------------------------------------------------------------------

@pytest.mark.parametrize("edits", [
    {"title": "base64:SGVsbG8="}, {"title": "two\nlines"}, {"caption": "bell\x07"}, {"title": 5},
    {"keywords": "a, b"}, {"date": {"iso": "1952-06", "level": "day"}}, {"date": {"iso": "1952-13", "level": "month"}},
    {"date": {"iso": "0999", "level": "year"}}, {"faces": {"mwg:0": {"box": [0.9, 0.9, 0.2, 0.2]}}},
    {"faces": {"mwg:0": {"box": [0.1, 0.1, 0, 0.2]}}}, {"nonsense": 1},
])
def test_invalid_edits_are_refused(edits):
    with pytest.raises(metaedit.EditError):
        metaedit.validate(edits)


def test_validation_normalizes():
    e = metaedit.validate({"caption": " Line 1\r\nLine 2 ", "keywords": ["a", "A", " b ", "DATE: Y~", ""],
                           "faces": {"new:1": {"name": "", "box": None}, "new:2": {"name": "X", "box": [0, 0, 0.5, 0.5]}}})
    assert e["caption"] == "Line 1\nLine 2"
    assert e["keywords"] == ["a", "b"]           # duplicates, blanks and date markers out
    assert list(e["faces"]) == ["new:2"]         # an empty new face is nothing


# -- effective fields ------------------------------------------------------------------

def test_apply_to_fields():
    f = {"title": "Old", "keywords": ["family", "DATE: Y~"], "date": "1925:06:15", "date_certainty": "Y~",
         "faces": [{"name": "Ann", "box": [0.1, 0.1, 0.1, 0.1], "ids": ["mwg:0"], "key": "mwg:0"}],
         "faces_unnamed": [{"name": "", "box": [0.5, 0.1, 0.1, 0.1], "ids": ["mwg:1"], "key": "mwg:1"}],
         "faces_unnamed_count": 1}
    g = metaedit.apply_to_fields(f, {"title": "", "keywords": ["picnic"]})
    assert g["title"] is None and g["keywords"] == ["picnic", "DATE: Y~"]   # the date marker stays with the date
    g = metaedit.apply_to_fields(f, {"date": {"iso": "1952-06", "level": "month"}})
    assert (g["date"], g["date_certainty"], g["keywords"]) == ("1952-06", "Y!M!", ["family", "DATE: Y!M!"])
    g = metaedit.apply_to_fields(f, {"date": None})
    assert g["date"] is None and g["keywords"] == ["family"]
    g = metaedit.apply_to_fields(f, {"faces": {"mwg:1": {"name": "Bea"}, "mwg:0": {"deleted": True},
                                               "new:x": {"name": "", "box": [0.7, 0.7, 0.1, 0.1]}}})
    assert [x["name"] for x in g["faces"]] == ["Bea"] and g["faces_unnamed_count"] == 1
    assert f["faces"][0]["name"] == "Ann"        # the input is not changed


# -- text fields -----------------------------------------------------------------------

def test_every_field_round_trips(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    edits = {"title": "Picnic at Lake Merced", "caption": "Sunday picnic\nafter church", "notes": "Scanned 2021",
             "creator": "Robert Church", "sublocation": "Lake Merced", "city": "San Francisco", "state": "California",
             "country": "USA", "keywords": ["family", "picnic"], "date": {"iso": "1952-06-14", "level": "day"}}
    before = image_data_hash(p)
    _details(p, edits)
    f = _fields(p)
    for k in ("title", "caption", "notes", "creator", "sublocation", "city", "state", "country"):
        assert f[k] == edits[k], k
    assert f["keywords"] == ["family", "picnic", "DATE: Y!M!D!"]
    assert f["date"].startswith("1952:06:14")
    assert image_data_hash(p) == before
    md = _md(p)
    assert "IPTC:City" not in md and not any(k.startswith("IPTC:") for k in md)   # IPTC is never created


def test_mirrors_present_in_the_file_follow(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _et(p, "-XMP-dc:Title=Old", "-IPTC:ObjectName=Old", "-EXIF:XPTitle=Old", "-XMP-dc:Description=Old d",
        "-IPTC:Caption-Abstract=Old d", "-EXIF:ImageDescription=Old d", "-IPTC:Keywords=k1", "-XMP-dc:Subject=k1",
        "-IPTC:City=Oldtown", "-EXIF:Artist=Old A")
    _details(p, {"title": "New", "caption": "New d", "keywords": ["k2"], "city": "Newtown", "creator": "New A"})
    md = _md(p)
    assert md["XMP-dc:Title"] == md["IPTC:ObjectName"] == md["IFD0:XPTitle"] == "New"
    assert md["XMP-dc:Description"] == md["IPTC:Caption-Abstract"] == md["IFD0:ImageDescription"] == "New d"
    assert L(md["IPTC:Keywords"]) == L(md["XMP-dc:Subject"]) == ["k2"]
    assert md["IPTC:City"] == md["XMP-photoshop:City"] == "Newtown"
    assert md["IFD0:Artist"] == "New A"


def test_clearing_removes_fallbacks_and_languages(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _et(p, "-XMP-dc:Title=Old", "-XMP-dc:Title-de=Alt", "-XMP-photoshop:Headline=Head", "-IPTC:ObjectName=Old",
        "-XMP-dc:Description=D", "-EXIF:XPComment=XP")
    _details(p, {"title": "", "caption": ""})
    f = _fields(p)
    assert f["title"] is None and f["caption"] is None
    md = _md(p)
    assert not any(k.startswith(("XMP-dc:Title", "IPTC:ObjectName", "XMP-photoshop:Headline", "IFD0:XPComment"))
                   for k in md)


def test_iptc_never_holds_mangled_text(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _et(p, "-IPTC:City=Old", "-IPTC:ObjectName=Old", "-XMP-photoshop:City=Old", "-XMP-dc:Title=Old")
    _details(p, {"city": "東京", "title": "x" * 70})     # not cp1252 / over the 64-byte IIM limit
    md = _md(p)
    assert "IPTC:City" not in md and "IPTC:ObjectName" not in md
    f = _fields(p)
    assert f["city"] == "東京" and f["title"] == "x" * 70
    _details(p, {"city": "Zürich"})                    # cp1252 is fine in an unflagged IPTC
    assert _fields(p)["city"] == "Zürich"


def test_utf8_flagged_iptc_takes_any_text(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-charset", "iptc=UTF8", "-IPTC:CodedCharacterSet=UTF8",
                    "-IPTC:City=Old", p], check=True)
    _details(p, {"city": "東京"})
    assert _md(p)["IPTC:City"] == "東京"


def test_keywords_and_lightroom_hierarchy(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _et(p, "-XMP-dc:Subject=Ann", "-XMP-dc:Subject=picnic", "-XMP-lr:HierarchicalSubject=People|Ann",
        "-XMP-lr:HierarchicalSubject=Events|picnic", "-XMP-dc:Subject=DATE: Y~")
    _details(p, {"keywords": ["picnic", "lake"]})
    md = _md(p)
    assert md["XMP-dc:Subject"] == ["picnic", "lake", "DATE: Y~"]
    assert L(md["XMP-lr:HierarchicalSubject"]) == ["Events|picnic"]


# -- dates -----------------------------------------------------------------------------

def _caption(path, fmt="{date}"):
    from captiontokens import resolve
    return resolve(fmt, _fields(path)).text


@pytest.mark.parametrize("iso, level, shown", [
    ("1952-06-14", "day", "June 14, 1952"), ("1952-06", "month", "June 1952"), ("1952", "year", "1952"),
    ("1925", "circa", "c. 1925"),
])
def test_dates_at_every_level(tmp_path, iso, level, shown):
    p = _img(str(tmp_path / "a.jpg"))
    _details(p, {"date": {"iso": iso, "level": level}})
    assert _caption(p) == shown
    md = _md(p)
    assert md["ExifIFD:DateTimeOriginal"].endswith("00:00:00")      # whole, at midnight (photokin's form)
    assert L(md["XMP-dc:Subject"]) == [f"DATE: {metaedit.LEVELS[level]}"]


def test_a_cameras_own_time_stays_when_the_day_is_kept(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _et(p, "-ExifIFD:DateTimeOriginal=2017:04:05 17:01:07", "-ExifIFD:OffsetTimeOriginal=+02:00")
    _details(p, {"date": {"iso": "2017-04-05", "level": "day"}})
    md = _md(p)
    assert md["ExifIFD:DateTimeOriginal"] == "2017:04:05 17:01:07" and md["ExifIFD:OffsetTimeOriginal"] == "+02:00"
    _details(p, {"date": {"iso": "2017-04", "level": "month"}})      # less detail: the time means nothing now
    md = _md(p)
    assert md["ExifIFD:DateTimeOriginal"] == "2017:04:15 00:00:00" and "ExifIFD:OffsetTimeOriginal" not in md
    assert _caption(p) == "April 2017"


def test_clearing_the_date(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _et(p, "-ExifIFD:DateTimeOriginal=1952:06:15 00:00:00", "-XMP-photoshop:DateCreated=1952",
        "-IPTC:DateCreated=1952:06:15", "-XMP-dc:Subject=DATE: Y!", "-XMP-dc:Subject=family")
    _details(p, {"date": None})
    f = _fields(p)
    assert f["date"] is None and f["keywords"] == ["family"]


def test_png_gets_no_exif_block(tmp_path):
    p = _img(str(tmp_path / "a.png"))
    _details(p, {"date": {"iso": "1952", "level": "year"}, "notes": "From the attic"})
    md = _md(p)
    assert not any(k.startswith(("ExifIFD:", "IFD0:")) for k in md)
    f = _fields(p)
    assert f["notes"] == "From the attic" and _caption(p) == "1952"


# -- faces -----------------------------------------------------------------------------

def _regions(path, regions, w, h, extra=None):
    _et_json(path, {"XMP-mwg-rs:RegionInfo": {"AppliedToDimensions": {"W": w, "H": h, "Unit": "pixel"},
                                              "RegionList": regions}, **(extra or {})})


def _area(cx, cy, w, h):
    return {"X": cx, "Y": cy, "W": w, "H": h, "Unit": "normalized"}


def _named(path):
    return {f["name"]: [round(v, 3) for v in f["box"]] for f in _fields(path)["faces"]}


def test_rename_move_delete_and_add_faces(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _regions(p, [{"Type": "Face", "Name": "Ann", "Area": _area(0.2, 0.3, 0.1, 0.1)},
                 {"Type": "Face", "Name": "Bob", "Area": _area(0.5, 0.3, 0.1, 0.1)},
                 {"Type": "Face", "Area": _area(0.8, 0.3, 0.1, 0.1)},
                 {"Type": "Focus", "Area": _area(0.5, 0.5, 0.2, 0.2)}], 300, 200)
    keys = {f["name"] or "?": f["key"] for f in _fields(p)["faces"] + _fields(p)["faces_unnamed"]}
    _details(p, {"faces": {keys["Ann"]: {"name": "Anne", "box": [0.1, 0.1, 0.12, 0.12]},
                           keys["Bob"]: {"deleted": True},
                           keys["?"]: {"name": "Cy"},
                           "new:1": {"name": "Dee", "box": [0.4, 0.6, 0.1, 0.1]}}})
    assert _named(p) == {"Anne": [0.1, 0.1, 0.12, 0.12], "Cy": [0.75, 0.25, 0.1, 0.1], "Dee": [0.4, 0.6, 0.1, 0.1]}
    regs = _md(p)["XMP-mwg-rs:RegionInfo"]["RegionList"]
    assert any(r.get("Type") == "Focus" for r in regs)        # not a face: untouched


def test_mp_and_person_in_image_follow(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _regions(p, [{"Type": "Face", "Name": "Ann", "Area": _area(0.2, 0.3, 0.1, 0.1)}], 300, 200,
             {"XMP-MP:RegionInfoMP": {"Regions": [{"PersonDisplayName": "Ann", "Rectangle": "0.15, 0.25, 0.1, 0.1"}]},
              "XMP-iptcExt:PersonInImage": ["Ann"]})
    key = _fields(p)["faces"][0]["key"]
    assert "mp:0" in key                                       # the MP copy belongs to the same face
    _details(p, {"faces": {key: {"name": "Anne", "box": [0.5, 0.5, 0.1, 0.1]}}})
    md = _md(p)
    assert md["XMP-MP:RegionInfoMP"]["Regions"][0]["PersonDisplayName"] == "Anne"
    assert L(md["XMP-iptcExt:PersonInImage"]) == ["Anne"]
    assert _named(p) == {"Anne": [0.5, 0.5, 0.1, 0.1]}       # one face, not two


def test_people_named_without_a_region_stay_and_can_be_marked(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _regions(p, [{"Type": "Face", "Name": "Ann", "Area": _area(0.2, 0.3, 0.1, 0.1)}], 300, 200,
             {"XMP-iptcExt:PersonInImage": ["Ann", "Bob"]})
    f = _fields(p)
    assert [(x["name"], x["box"] is None) for x in f["faces"]] == [("Ann", False), ("Bob", True)]
    bob = next(x for x in f["faces"] if x["name"] == "Bob")
    _details(p, {"faces": {bob["key"]: {"box": [0.6, 0.2, 0.1, 0.1]}}})
    assert _named(p) == {"Ann": [0.15, 0.25, 0.1, 0.1], "Bob": [0.6, 0.2, 0.1, 0.1]}
    assert L(_md(p)["XMP-iptcExt:PersonInImage"]) == ["Ann", "Bob"]


def test_faces_on_a_rotated_lightroom_photo(tmp_path):
    # stored 300x200, shown upright 200x300 (orientation 6); Lightroom: boxes in the stored frame,
    # AppliedToDimensions upright, a Rotation on each region
    p = _img(str(tmp_path / "a.jpg"), orientation=6)
    _regions(p, [{"Type": "Face", "Name": "Ann", "Rotation": 0, "Area": _area(0.25, 0.5, 0.1, 0.15)}], 200, 300)
    f = _fields(p)["faces"][0]
    _details(p, {"faces": {f["key"]: {"box": [0.2, 0.6, 0.15, 0.1]},
                           "new:1": {"name": "Bob", "box": [0.6, 0.1, 0.15, 0.1]}}})
    assert _named(p) == {"Ann": [0.2, 0.6, 0.15, 0.1], "Bob": [0.6, 0.1, 0.15, 0.1]}
    regs = _md(p)["XMP-mwg-rs:RegionInfo"]["RegionList"]
    assert all("Rotation" in r for r in regs)                  # Lightroom reads them all the same way


def test_first_face_on_a_rotated_photo_without_regions(tmp_path):
    p = _img(str(tmp_path / "a.jpg"), orientation=6)
    _details(p, {"faces": {"new:1": {"name": "Ann", "box": [0.1, 0.2, 0.3, 0.2]}}})
    assert _named(p) == {"Ann": [0.1, 0.2, 0.3, 0.2]}
    atd = _md(p)["XMP-mwg-rs:RegionInfo"]["AppliedToDimensions"]
    assert (atd["W"], atd["H"]) == (300, 200)                  # the stored frame, as MWG says


def test_removing_every_face(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _regions(p, [{"Type": "Face", "Name": "Ann", "Area": _area(0.2, 0.3, 0.1, 0.1)}], 300, 200)
    _details(p, {"faces": {_fields(p)["faces"][0]["key"]: {"deleted": True}}})
    assert _fields(p)["faces"] == []


# -- details save: safety --------------------------------------------------------------

def test_backup_once_then_the_log_keeps_old_values(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    orig = open(p, "rb").read()
    r1 = _details(p, {"title": "One"}, backupOriginals=True)
    assert r1.backup_path and open(r1.backup_path, "rb").read() == orig
    r2 = _details(p, {"title": "Two"}, backupOriginals=True)
    assert not r2.backup_path and any("Backup already kept" in n for n in r2.notes)
    assert any("'One' -> 'Two'" in n for n in r2.notes)
    assert len(os.listdir(os.path.dirname(r1.backup_path))) == 2   # the backup + the folder note


def test_no_backup_when_off(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    r = _details(p, {"title": "One"})
    assert not r.backup_path and not os.path.exists(tmp_path / "_originals")


def test_changed_on_disk_is_refused(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    st = os.stat(p)
    opened = [st.st_size, str(st.st_mtime_ns), "", str(st.st_ino)]
    _et(p, "-XMP-dc:Title=Someone else")
    r = save_details(p, {"title": "Mine"}, _settings(), opened)
    assert not r.ok and r.code == "changed"
    assert _fields(p)["title"] == "Someone else"


def test_same_size_replacement_with_kept_dates_is_a_change(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _details(p, {"title": "AAAA"})
    st = os.stat(p)
    opened = [st.st_size, str(st.st_mtime_ns), "", str(st.st_ino)]
    _details(p, {"title": "BBBB"}, keepFileDates=True)       # same size, same modified time, new file
    st2 = os.stat(p)
    assert (st2.st_size, st2.st_mtime_ns) == (st.st_size, st.st_mtime_ns)
    r = save_details(p, {"title": "CCCC"}, _settings(), opened)
    assert not r.ok and r.code == "changed"


def test_failed_check_leaves_the_file_untouched(tmp_path, monkeypatch):
    p = _img(str(tmp_path / "a.jpg"))
    orig = open(p, "rb").read()
    monkeypatch.setattr(metaedit, "check_written", lambda *a, **k: ["title"])
    r = save_details(p, {"title": "Nope"}, _settings())
    assert not r.ok and r.code == "metadata"
    assert open(p, "rb").read() == orig
    assert [f for f in os.listdir(tmp_path) if f != "a.jpg"] == []    # no temp file left


def test_read_only_and_backup_folder_are_refused(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    os.chmod(p, 0o444)
    try:
        assert save_details(p, {"title": "x"}, _settings()).code == "readonly"
    finally:
        os.chmod(p, 0o666)
    b = tmp_path / "_originals"
    b.mkdir()
    q = _img(str(b / "a-original.jpg"))
    assert save_details(q, {"title": "x"}, _settings()).code == "backup_folder"


def test_caches_carry_over(tmp_path):
    from photoband import photos, proxy
    p = _img(str(tmp_path / "a.jpg"))
    photos.proxy_paths(p)
    old = probe(p)
    _details(p, {"title": "x"})
    assert probe(p).mtime_ns != old.mtime_ns
    assert proxy.cached_proxy(p, probe(p)) is not None      # not decoded again for a details edit


# -- saving a captioned copy / overwrite with edits ------------------------------------------

def _caption_save(path, mode, edits, **saving):
    with Image.open(path) as im:
        w, h = im.size
    lay, tiles = make_band_layout(w, h, text="x")
    state = {"templateId": "classic-polaroid", "template": {"id": "classic-polaroid"}, "blocks": [], "overrides": {}}
    return save(SaveRequest(path=path, mode=mode, layout=lay, tiles=tiles, state=state, settings=_settings(**saving),
                            meta_edits=edits, fields=_fields(path)))


def test_a_copy_carries_the_edits_and_the_original_does_not_change(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _et(p, "-XMP-dc:Title=Old", "-XMP-dc:Subject=k")
    _regions(p, [{"Type": "Face", "Name": "Ann", "Area": _area(0.2, 0.3, 0.1, 0.1)}], 300, 200)
    orig = open(p, "rb").read()
    key = _fields(p)["faces"][0]["key"]
    r = _caption_save(p, "copy", {"title": "", "keywords": [], "faces": {key: {"name": "Anne"}}})
    assert r.ok, r.error
    assert open(p, "rb").read() == orig
    f = _fields(r.out_path)
    assert f["title"] is None and f["keywords"] == [] and [x["name"] for x in f["faces"]] == ["Anne"]


def test_overwrite_with_a_cleared_title(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _et(p, "-XMP-dc:Title=Old", "-XMP-dc:Creator=Me")
    r = _caption_save(p, "overwrite", {"title": "", "creator": ""})
    assert r.ok, r.error
    f = _fields(p)
    assert f["title"] is None and f["creator"] is None


def test_file_name_pattern_sees_the_edits(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    r = _caption_save(p, "copy", {"title": "Picnic"}, fileName="{title}")
    assert r.ok and os.path.basename(r.out_path).startswith("Picnic")


def test_edited_faces_gone_from_the_file_stop_the_save(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    r = _caption_save(p, "copy", {"faces": {"mwg:7": {"name": "Ghost"}}})
    assert not r.ok and r.code == "changed"
    r = save_details(p, {"faces": {"mwg:7": {"name": "Ghost"}}}, _settings())
    assert not r.ok and r.code == "changed"


def test_save_module_exposes_details():
    assert callable(savemod.save_details)


def test_a_batch_copy_keeps_the_details_draft_for_the_original(tmp_path):
    from photoband import drafts
    p = _img(str(tmp_path / "a.jpg"))
    st = {"templateId": "t", "blocks": {"b": {"id": "b", "custom": True, "text": "x"}}, "overrides": {"o": 1},
          "meta": {"title": "Kept"}, "faceRows": 2, "_hash": "h1"}
    drafts.save_draft(p, st)
    assert not drafts.keep_details_if(p, "other")          # edited since staging: kept whole
    assert drafts.load_draft(p)["blocks"]
    assert drafts.keep_details_if(p, "h1")
    d = drafts.load_draft(p)
    assert d["meta"] == {"title": "Kept"} and d["faceRows"] == 2 and d["blocks"] == {} and d["overrides"] == {}
    drafts.save_draft(p, {"templateId": "t", "blocks": {}, "_hash": "h2"})
    assert drafts.keep_details_if(p, "h2") and drafts.load_draft(p) is None   # nothing to keep


# -- adversarial review regressions ---------------------------------------------------------

def test_a_face_named_only_in_mp_keeps_its_lightroom_region(tmp_path):
    # the reader folds an unnamed MWG region onto the MP-named face: editing the face renames both
    p = _img(str(tmp_path / "a.jpg"))
    _regions(p, [{"Type": "Face", "Area": _area(0.2, 0.3, 0.1, 0.1)}], 300, 200,
             {"XMP-MP:RegionInfoMP": {"Regions": [{"PersonDisplayName": "Ann", "Rectangle": "0.15, 0.25, 0.1, 0.1"}]}})
    key = _fields(p)["faces"][0]["key"]
    _details(p, {"faces": {key: {"name": "Anne"}}})
    md = _md(p)
    assert [r.get("Name") for r in md["XMP-mwg-rs:RegionInfo"]["RegionList"]] == ["Anne"]
    assert md["XMP-MP:RegionInfoMP"]["Regions"][0]["PersonDisplayName"] == "Anne"


def test_a_users_own_date_keyword_is_kept(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _et(p, "-XMP-dc:Subject=family", "-XMP-dc:Subject=Date: probably 1950s, ask Ann",
        "-XMP-lr:HierarchicalSubject=Notes|Date: probably 1950s, ask Ann")
    _details(p, {"date": {"iso": "1952", "level": "year"}})
    md = _md(p)
    assert L(md["XMP-dc:Subject"]) == ["family", "Date: probably 1950s, ask Ann", "DATE: Y!"]
    assert L(md["XMP-lr:HierarchicalSubject"]) == ["Notes|Date: probably 1950s, ask Ann"]


def test_several_photographers_stay_separate(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _et(p, "-XMP-dc:Creator=Ann Smith", "-XMP-dc:Creator=Bob Jones", "-EXIF:Artist=Ann Smith; Bob Jones")
    assert _fields(p)["creators"] == ["Ann Smith", "Bob Jones"]
    _details(p, {"creator": ["Ann Smith", "Bob Jones", "Cy"]})
    md = _md(p)
    assert md["XMP-dc:Creator"] == ["Ann Smith", "Bob Jones", "Cy"]
    assert md["IFD0:Artist"] == "Ann Smith; Bob Jones; Cy"
    assert _fields(p)["creator"] == "Ann Smith, Bob Jones, Cy"
    # a text is one name, never split (a comma in it is part of the name)
    _details(p, {"creator": ["Smith, Ann", "Bob Jones"]})
    assert _md(p)["XMP-dc:Creator"] == ["Smith, Ann", "Bob Jones"]


def test_a_face_a_little_outside_the_frame_can_be_renamed(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _regions(p, [{"Type": "Face", "Name": "Ann", "Area": _area(0.98, 0.3, 0.1, 0.1)}], 300, 200)
    f = _fields(p)["faces"][0]
    _details(p, {"faces": {f["key"]: {"name": "Anne", "was": {"name": "Ann", "box": f["box"]}}}})
    assert [x["name"] for x in _fields(p)["faces"]] == ["Anne"]


def test_a_copy_may_crop_away_an_unnamed_face(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _regions(p, [{"Type": "Face", "Name": "Ann", "Area": _area(0.2, 0.3, 0.1, 0.1)},
                 {"Type": "Face", "Area": _area(0.8, 0.3, 0.1, 0.1)}], 300, 200)
    key = _fields(p)["faces"][0]["key"]
    with Image.open(p) as im:
        w, h = im.size
    lay, tiles = make_band_layout(w // 2, h, text="x", source_rect=[0, 0, w // 2, h])
    state = {"templateId": "classic-polaroid", "template": {"id": "classic-polaroid"}, "blocks": [], "overrides": {}}
    r = save(SaveRequest(path=p, mode="copy", layout=lay, tiles=tiles, state=state, settings=_settings(),
                         meta_edits={"faces": {key: {"name": "Anne"}}}, fields=_fields(p)))
    assert r.ok, r.error
    assert [x["name"] for x in _fields(r.out_path)["faces"]] == ["Anne"]


def test_long_text_stays_out_of_a_jpegs_exif(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _et(p, "-EXIF:ImageDescription=short")
    _details(p, {"caption": "word " * 8000, "notes": "ü" * 9000})
    md = _md(p)
    assert "IFD0:ImageDescription" not in md and "ExifIFD:UserComment" not in md
    f = _fields(p)
    assert f["caption"] == ("word " * 8000).strip() and f["notes"] == "ü" * 9000
    out = subprocess.run(["exiftool", "-validate", "-warning", "-a", p], capture_output=True, text=True).stdout
    assert "multi-segment" not in out


def test_an_edit_the_file_already_has_changes_nothing(tmp_path):
    p = _img(str(tmp_path / "a.tif"))
    orig = open(p, "rb").read()
    r = _details(p, {"caption": ""})        # the file has no caption to clear
    assert open(p, "rb").read() == orig and "Nothing needed changing" in r.notes[0]


def test_tifffile_shape_notes_are_not_a_caption():
    from photoband.imageio import ImageInfo
    md = {"IFD0:ImageDescription": '{"shape": [10, 10, 3]}'}
    f = normalize(md, ImageInfo("x.tif", "TIFF", 10, 10, 3, "uint8", "RGB", orientation=1))["fields"]
    assert f["caption"] is None


def test_windows_keywords_never_split_a_keyword(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _et(p, "-EXIF:XPKeywords=old")
    _details(p, {"keywords": ["Smith; John", "picnic"]})
    assert _md(p)["IFD0:XPKeywords"] == "picnic"
    _details(p, {"keywords": []})
    assert "IFD0:XPKeywords" not in _md(p)


def test_a_png_copy_of_a_jpeg_gets_no_exif_block(tmp_path):
    from photoband.imageio import probe as _probe
    p = _img(str(tmp_path / "a.png"))
    upd, dels, _ = metaedit.tag_updates({}, _probe(p), {}, {"notes": "n", "date": {"iso": "1952", "level": "year"}}, "PNG")
    assert not any(k.startswith(("ExifIFD:", "IFD0:")) for k in upd)


def test_the_log_keeps_every_replaced_value(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _et(p, "-XMP-dc:Title=Old", "-XMP-photoshop:Headline=Head")
    r = _details(p, {"title": ""})
    prev = next(n for n in r.notes if n.startswith("Previous values: "))
    assert json.loads(prev[len("Previous values: "):]) == {"XMP-dc:Title": "Old", "XMP-photoshop:Headline": "Head"}


def test_an_edit_made_on_another_face_is_refused(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _regions(p, [{"Type": "Face", "Name": "Bob", "Area": _area(0.5, 0.5, 0.1, 0.1)}], 300, 200)
    r = save_details(p, {"faces": {"mwg:0": {"name": "Anne", "was": {"name": "Ann", "box": [0.1, 0.1, 0.1, 0.1]}}}}, _settings())
    assert not r.ok and r.code == "changed"


# -- Codex review regressions -------------------------------------------------------------

def test_clearing_a_name_listed_only_in_person_in_image_removes_it(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _et(p, "-XMP-iptcExt:PersonInImage=Ann", "-XMP-iptcExt:PersonInImage=Bob")
    ann = next(f for f in _fields(p)["faces"] if f["name"] == "Ann")
    _details(p, {"faces": {ann["key"]: {"name": ""}}})
    assert [f["name"] for f in _fields(p)["faces"]] == ["Bob"]


def test_creators_with_commas_in_their_names_stay_two(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _et(p, "-XMP-dc:Creator=Smith, John", "-XMP-dc:Creator=Jones, Mary")
    f = _fields(p)
    assert f["creator"] == "Smith, John; Jones, Mary"
    _details(p, {"creator": ["Smith, John", "Jones, Mary", "Lee, Ann"]})
    assert _md(p)["XMP-dc:Creator"] == ["Smith, John", "Jones, Mary", "Lee, Ann"]


def test_date_copies_follow_the_day_when_the_cameras_time_is_kept(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _et(p, "-ExifIFD:DateTimeOriginal=2017:04:05 17:01:07", "-XMP-exif:DateTimeOriginal=2016:01:01 10:00:00",
        "-IPTC:DateCreated=2016:01:01")
    _details(p, {"date": {"iso": "2017-04-05", "level": "day"}})
    md = _md(p)
    assert md["ExifIFD:DateTimeOriginal"] == "2017:04:05 17:01:07"
    assert str(md["XMP-exif:DateTimeOriginal"]).startswith("2017:04:05 17:01:07")
    assert md["IPTC:DateCreated"] == "2017:04:05"


# -- Codex review, round 2 ---------------------------------------------------------------

def test_a_batch_kept_details_draft_has_the_editors_whole_shape(tmp_path):
    from photoband import drafts
    p = _img(str(tmp_path / "a.jpg"))
    drafts.save_draft(p, {"templateId": "t", "blocks": {}, "meta": {"title": "T"}, "_hash": "h"})
    assert drafts.keep_details_if(p, "h")
    d = drafts.load_draft(p)
    assert d["mode"] == "band" and "sourceRect" in d and "photoRect" in d and d["keepBand"] is False


def test_written_details_drafts_are_compared_as_saved(tmp_path):
    from photoband import drafts
    p = _img(str(tmp_path / "a.jpg"))
    drafts.save_draft(p, {"templateId": "t", "blocks": {}, "meta": {"title": "Picnic "}})
    assert drafts.delete_written_details(p, {"title": "Picnic"})
    assert drafts.load_draft_any(p) is None


def test_person_in_image_follows_a_rename_whatever_its_case(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _regions(p, [{"Type": "Face", "Name": "ann", "Area": _area(0.2, 0.3, 0.1, 0.1)}], 300, 200,
             {"XMP-iptcExt:PersonInImage": ["Ann"]})
    key = _fields(p)["faces"][0]["key"]
    _details(p, {"faces": {key: {"name": "Anne"}}})
    assert L(_md(p)["XMP-iptcExt:PersonInImage"]) == ["Anne"]
    assert [f["name"] for f in _fields(p)["faces"]] == ["Anne"]


def test_a_renamed_person_keeps_their_place_in_lightrooms_hierarchy(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _regions(p, [{"Type": "Face", "Name": "Ann", "Area": _area(0.2, 0.3, 0.1, 0.1)}], 300, 200,
             {"XMP-dc:Subject": ["Ann", "picnic"], "XMP-lr:HierarchicalSubject": ["People|Ann", "Events|picnic"]})
    key = _fields(p)["faces"][0]["key"]
    _details(p, {"faces": {key: {"name": "Anne"}}, "keywords": ["picnic", "Anne"]})
    assert L(_md(p)["XMP-lr:HierarchicalSubject"]) == ["People|Anne", "Events|picnic"]


def test_only_a_backup_of_this_photo_counts_as_kept(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    os.makedirs(tmp_path / "_originals")
    other = _img(str(tmp_path / "_originals" / "a-original.jpg"), size=(200, 300))   # not this photo
    r = _details(p, {"title": "One"}, backupOriginals=True)
    assert r.backup_path and os.path.basename(r.backup_path) == "a-original-2.jpg"
    assert os.path.exists(other)
    r2 = _details(p, {"title": "Two"}, backupOriginals=True)          # now a real backup exists
    assert not r2.backup_path


# -- Codex review, round 3 ---------------------------------------------------------------

def test_a_semicolon_inside_a_creators_name_is_kept(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _et(p, "-XMP-dc:Creator=ACME; Inc.", "-XMP-dc:Creator=Jane")
    f = _fields(p)
    assert f["creator"] == "ACME; Inc., Jane"
    _details(p, {"creator": ["ACME; Inc.", "Jane", "Bo"]})
    assert _md(p)["XMP-dc:Creator"] == ["ACME; Inc.", "Jane", "Bo"]


def test_a_capitalisation_only_rename_reaches_person_in_image_and_the_hierarchy(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _regions(p, [{"Type": "Face", "Name": "ann", "Area": _area(0.2, 0.3, 0.1, 0.1)}], 300, 200,
             {"XMP-iptcExt:PersonInImage": ["ann"], "XMP-dc:Subject": ["ann"], "XMP-lr:HierarchicalSubject": ["People|ann"]})
    key = _fields(p)["faces"][0]["key"]
    _details(p, {"faces": {key: {"name": "Ann"}}, "keywords": ["Ann"]})
    md = _md(p)
    assert L(md["XMP-iptcExt:PersonInImage"]) == ["Ann"]
    assert L(md["XMP-lr:HierarchicalSubject"]) == ["People|Ann"]


def test_a_placeholder_date_does_not_hide_the_cameras_time(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _et(p, "-n", "-ExifIFD:DateTimeOriginal=0000:00:00 00:00:00", "-XMP-exif:DateTimeOriginal=2017:04:05 17:01:07")
    assert _md(p)["ExifIFD:DateTimeOriginal"].startswith("0000")
    _details(p, {"date": {"iso": "2017-04-05", "level": "day"}})
    md = _md(p)
    assert str(md["XMP-exif:DateTimeOriginal"]).startswith("2017:04:05 17:01:07")
    assert md["ExifIFD:DateTimeOriginal"].startswith("2017:04:05 17:01:07")



# -- Codex review, round 4 ---------------------------------------------------------------

def test_date_copies_take_the_cameras_exact_time(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _et(p, "-ExifIFD:DateTimeOriginal=2017:04:05 17:01:07", "-XMP-exif:DateTimeOriginal=2017:04:05 09:00:00")
    _details(p, {"date": {"iso": "2017-04-05", "level": "day"}})
    md = _md(p)
    assert md["ExifIFD:DateTimeOriginal"] == "2017:04:05 17:01:07"
    assert str(md["XMP-exif:DateTimeOriginal"]).startswith("2017:04:05 17:01:07")


def test_a_single_photographer_typed_with_a_comma_stays_one():
    assert metaedit.validate({"creator": "Smith, Ann"}) == {"creator": "Smith, Ann"}
    assert metaedit.apply_to_fields({}, {"creator": ["Smith, Ann", "Bo"]})["creator"] == "Smith, Ann; Bo"
    with pytest.raises(metaedit.EditError):
        metaedit.validate({"creator": ["a\nb"]})


# -- estimated months and days ------------------------------------------------------------

@pytest.mark.parametrize("iso, level, shown, marker", [
    ("1944-11-23", "day", "c. November 23, 1944", "Y!M!D~"),    # around Thanksgiving
    ("1944-07", "month", "c. July 1944", "Y!M~"),               # the summer of 1944
    ("1925", "year", "c. 1925", "Y~"),
])
def test_estimated_dates(tmp_path, iso, level, shown, marker):
    p = _img(str(tmp_path / "a.jpg"))
    _details(p, {"date": {"iso": iso, "level": level, "estimate": True}})
    assert _caption(p) == shown
    md = _md(p)
    assert L(md["XMP-dc:Subject"]) == [f"DATE: {marker}"]
    assert md["ExifIFD:DateTimeOriginal"].endswith("00:00:00")


def test_an_estimated_day_never_keeps_a_cameras_time(tmp_path):
    p = _img(str(tmp_path / "a.jpg"))
    _et(p, "-ExifIFD:DateTimeOriginal=1944:11:23 17:01:07")
    _details(p, {"date": {"iso": "1944-11-23", "level": "day", "estimate": True}})
    assert _md(p)["ExifIFD:DateTimeOriginal"] == "1944:11:23 00:00:00"
    assert _caption(p) == "c. November 23, 1944"


def test_an_older_circa_edit_is_an_estimated_year():
    assert metaedit.validate({"date": {"iso": "1925", "level": "circa"}})["date"] == \
        {"iso": "1925", "level": "year", "estimate": True}
