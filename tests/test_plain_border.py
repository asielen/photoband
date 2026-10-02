"""A plain border is not an existing caption (cases B and C need writing in the border).

Regression: a scanned print with a plain white paper border and nothing written on it was
called case C ("Handwriting or printing on the photo's border", with the band detector's edge
confidence shown as "83% sure"), while a quiet note said "A plain border was found with no
text in it." The scan cues (paper grain, soft edge, lighting drift, 800 dpi) only say what
KIND of border it is; the candidate marks were dust, a hair and the print's own paper edge
against the scanner lid.
"""
from __future__ import annotations

import os

import numpy as np
import pytest

from photoband import detect, ocr
from photoband.detect import BandResult, TextBlock, TextLine, decide_case, filter_ocr_lines, ink_like
from test_detect import make_photo as _make_photo
from test_detect import ITALIC, analyze, framed, make_photo, polaroid_sides

PLAIN = detect.PLAIN_BORDER_HINT


def _paper(lines=(), seed=41):
    photo = make_photo(1000, 1000, seed=seed)
    img, truth = framed(photo, (236, 228, 208), polaroid_sides(1000), list(lines), font_px=58,
                        text_color=(40, 45, 90), font_path=ITALIC, texture=4.0, gradient=5.0)
    return img, truth


def _scanner_marks(img, truth):
    """What a flatbed adds to a plain border: dust specks, a hair, a thin shadow line along
    the paper edge (the kinds of marks the woodbury-156 scan's 13 candidate "blocks" were)."""
    img = img.copy()
    x, y, w, h = truth
    H, W = img.shape[:2]
    by = y + h + 40
    img[by:by + 5, 120:126] = 60                    # dust speck
    img[by + 60:by + 64, 400:418] = 90              # short hair
    img[20:H - 20, W - 12:W - 9] = 150              # paper-edge shadow: a thin sliver
    img[by + 120:by + 128, 700:707] = 70            # another speck
    return img


@pytest.mark.parametrize("run_ocr", [True, False])
def test_plain_paper_border_is_no_case(run_ocr):
    img, truth = _paper()
    img = _scanner_marks(img, truth)
    res = analyze(img, run_ocr=run_ocr)
    assert res["case"] is None, (res["case"], res.get("scanCues"))
    # one quiet note, and it agrees with the case (no "found writing" + "no text" pair)
    assert res["warnings"] == [PLAIN], res["warnings"]
    assert res["hasText"] is False and res["blocks"] == []
    # the whole scan is the photo: nothing is offered for erasing
    assert res["band"]["found"] is False
    assert res["band"]["photo_rect"] == [0, 0, img.shape[1], img.shape[0]]
    assert res["edgeConfidence"] == 0.0


def test_written_paper_border_is_still_case_c_without_ocr():
    img, _ = _paper(["Aunt Mary, Easter"])
    res = analyze(img, run_ocr=False)
    assert res["case"] == "C"
    assert PLAIN not in res["warnings"]
    # what the banner's "%" used to show: how sure the photo edge is, now named for that
    assert res["edgeConfidence"] == res["confidence"] > 0.5
    assert res["hasText"] is False     # nothing was read, so nothing to "use"


# --------------------------------------------------------------------------- decide_case


def _band(n_sides=4):
    sides = ["top", "bottom", "left", "right"][:n_sides]
    return BandResult(found=True, photo_rect=(100, 100, 800, 600),
                      bands=[{"side": s, "rect": (0, 0, 10, 10)} for s in sides],
                      band_color=(240, 240, 240), band_color_hex="#f0f0f0", textured=True, noise=3.0,
                      confidence=0.83)


def _blk(*boxes, text=""):
    lines = [TextLine(box=b, text=text, confidence=0.9 if text else 0.0, words=[]) for b in boxes]
    xs = [b[0] for b in boxes]
    ys = [b[1] for b in boxes]
    return TextBlock(box=(min(xs), min(ys), 10, 10), lines=lines)


SHAPE = (2898, 4226, 3)   # the woodbury-156 scan, upright
SPECKS = [_blk((686, 2775, 11, 13)), _blk((4123, 2816, 22, 19)), _blk((104, 2823, 18, 6)),
          _blk((52, 49, 21, 55), (54, 115, 6, 15)), _blk((4150, 1122, 6, 254)), _blk((4157, 544, 4, 143))]


@pytest.mark.parametrize("score", [0.0, 4.0])
def test_border_with_no_writing_is_no_case_whatever_the_scan_cues(score):
    # OCR ran and read nothing (its filter left no lines)
    assert decide_case(_band(), [], score, ocr_ran=True, shape=SHAPE) == (None, PLAIN)
    # no OCR: the woodbury-156 candidates are specks and slivers, not writing
    assert not ink_like(SPECKS, SHAPE)
    assert decide_case(_band(), SPECKS, score, ocr_ran=False, shape=SHAPE) == (None, PLAIN)
    # a single plain strip keeps its own note
    case, hint = decide_case(_band(1), [], score, ocr_ran=True, shape=SHAPE)
    assert case is None and "plain strip along the top edge" in hint and "Edge" not in hint


def test_border_with_writing_is_b_or_c_by_the_scan_cues():
    words = [_blk((300, 2700, 900, 70), text="Aunt Mary 1952")]
    assert decide_case(_band(), words, 4.0, ocr_ran=True, shape=SHAPE) == ("C", None)
    assert decide_case(_band(), words, 0.0, ocr_ran=True, shape=SHAPE) == ("B", None)
    # no OCR: a line shaped like writing, horizontal or sideways on a side border
    unread = [_blk((300, 2700, 900, 70))]
    assert decide_case(_band(), unread, 4.0, ocr_ran=False, shape=SHAPE) == ("C", None)
    assert ink_like([_blk((60, 600, 70, 500))], SHAPE)


def test_marker_proves_photobands_own_band_even_without_text():
    assert decide_case(_band(), [], 0.0, ocr_ran=True, marker=True, shape=SHAPE) == ("B", None)


# --------------------------------------------------------------------------- OCR output on noise
# CI (Linux, Tesseract only) read the scanner marks of the blank border above as text while
# Windows OCR read nothing: an engine's characters on dust and hairs must never count as writing,
# whichever engine it is. These are the exact reads Tesseract 5 returned for the three marks.

NOISE_SHAPE = (1356, 1114, 3)
TESS_NOISE = [((120, 1116, 6, 5), "|", 0.625), ((400, 1176, 18, 4), "az", 0.334), ((700, 1236, 7, 8), "|", 0.357)]


def _read(box, text, conf):
    return TextBlock(box=box, lines=[TextLine(box=box, text=text, confidence=conf,
                                              words=[{"text": text, "confidence": conf, "box": box}])])


@pytest.mark.parametrize("conf", [None, ocr.UNKNOWN_CONFIDENCE, 0.99])
def test_engine_reads_on_specks_and_hairs_are_not_writing(conf):
    # Tesseract's own confidences, then the same text from an engine without confidences
    # (Windows OCR), then a confident engine: the marks are no bigger for it
    blocks = [_read(box, text, conf if conf is not None else c) for box, text, c in TESS_NOISE]
    assert filter_ocr_lines(blocks, shape=NOISE_SHAPE) == []
    assert decide_case(_band(), blocks, 4.0, ocr_ran=True, shape=NOISE_SHAPE) == (None, PLAIN)


@pytest.mark.parametrize("text", ["|", "l", "~~", "-.-", "'", "1"])
def test_single_glyphs_and_punctuation_are_not_writing_even_on_a_text_sized_mark(text):
    # the white-wall test's "|" read at 0.71 on a 15x36 px mark
    blocks = [_read((300, 2700, 40, 70), text, 0.95)]
    assert filter_ocr_lines(blocks, shape=SHAPE) == []
    assert decide_case(_band(), blocks, 4.0, ocr_ran=True, shape=SHAPE)[0] is None


def _initials(tokens, conf, box=(300, 2700, 260, 70)):
    """One line of separate tokens, each word with ``conf`` (Tesseract's per-word values, or
    Windows OCR's UNKNOWN_CONFIDENCE for every word)."""
    x, y, w, h = box
    words = [{"text": t, "confidence": conf, "box": (x + i * 60, y, 50, h)} for i, t in enumerate(tokens)]
    return [TextBlock(box=box, lines=[TextLine(box=box, text=" ".join(tokens), confidence=conf, words=words)])]


@pytest.mark.parametrize("conf", [0.91, ocr.UNKNOWN_CONFIDENCE])
@pytest.mark.parametrize("tokens", [["J", "R"], ["A.", "B."], ["J.R.W."], ["Jo"]])
def test_initials_are_writing_counted_over_the_line(tokens, conf):
    # each token may be one glyph: the evidence is the letters read in the LINE
    blocks = _initials(tokens, conf)
    assert filter_ocr_lines(blocks, shape=SHAPE) == blocks
    assert decide_case(_band(), blocks, 4.0, ocr_ran=True, shape=SHAPE) == ("C", None)
    assert decide_case(_band(1), blocks, 4.0, ocr_ran=True, shape=SHAPE) == ("C", None)   # single strip too


@pytest.mark.parametrize("conf", [0.91, ocr.UNKNOWN_CONFIDENCE])
def test_initials_on_a_speck_or_one_glyph_are_still_noise(conf):
    # the same reads on a speck-sized mark, and a single glyph on a text-sized (not line-shaped) one
    sq = (300, 2700, 90, 70)
    for blocks in (_initials(["J", "R"], conf, box=(300, 2700, 20, 8)), _initials(["a"], conf, box=sq),
                   _initials(["|", "l"], conf, box=sq), _initials([".", "-"], conf, box=sq)):
        assert filter_ocr_lines(blocks, shape=SHAPE) == [], blocks[0].lines[0].text
        assert decide_case(_band(), blocks, 4.0, ocr_ran=True, shape=SHAPE)[0] is None
    # on a line-shaped mark the glyph is still not text: the mark stays an unread candidate
    kept = filter_ocr_lines(_initials(["a"], conf), shape=SHAPE)
    assert [ln.text for b in kept for ln in b.lines] == [""]


def test_low_confidence_initials_count_only_on_a_writing_shaped_line():
    low = _initials(["J", "R"], 0.3)                          # 260x70: shaped like writing
    assert decide_case(_band(), low, 4.0, ocr_ran=True, shape=SHAPE) == ("C", None)
    assert decide_case(_band(1), low, 4.0, ocr_ran=True, shape=SHAPE)[0] is None
    squat = _initials(["J", "R"], 0.3, box=(300, 2700, 90, 70))   # text-sized, not writing-shaped
    assert decide_case(_band(), squat, 4.0, ocr_ran=True, shape=SHAPE)[0] is None


def test_low_confidence_read_counts_only_on_a_writing_shaped_line():
    # cursive reads poorly but it reads: a whole border with a writing-shaped line counts
    cursive = [_read((300, 2700, 900, 70), "Aunt Moy 1952", 0.31)]
    assert decide_case(_band(), cursive, 4.0, ocr_ran=True, shape=SHAPE) == ("C", None)
    assert filter_ocr_lines(cursive, shape=SHAPE) == cursive
    # a single strip: the 900x70 line is shaped like a caption line, so it counts there too
    assert decide_case(_band(1), cursive, 4.0, ocr_ran=True, shape=SHAPE) == ("C", None)
    confident = [_read((300, 2700, 900, 70), "Aunt Mary 1952", 0.9)]
    assert decide_case(_band(1), confident, 4.0, ocr_ran=True, shape=SHAPE) == ("C", None)
    # the same low-confidence read on a speck-sized mark is noise
    assert decide_case(_band(), [_read((300, 2700, 20, 8), "az", 0.31)], 4.0, ocr_ran=True,
                       shape=SHAPE)[0] is None


def test_a_caption_keeps_its_short_companion_lines():
    box = (300, 2700, 900, 70)
    year = (600, 2790, 120, 60)
    blk = TextBlock(box=(300, 2700, 900, 150), lines=[
        TextLine(box=box, text="Mom & Dad, Yosemite", confidence=0.9, words=[]),
        TextLine(box=year, text="'71", confidence=0.4, words=[]),
        TextLine(box=(700, 2860, 6, 5), text="|", confidence=0.6, words=[])])
    out = filter_ocr_lines([blk], shape=SHAPE)
    assert [ln.text for ln in out[0].lines] == ["Mom & Dad, Yosemite", "'71"]


# --------------------------------------------------------------------------- each engine, for real
# The engines read differently (CI has only Tesseract; Windows prefers Windows OCR), so the
# decisions are checked with each one forced. Skipped where the engine is not installed.

def _engine_available(name):
    try:
        return bool(ocr._ADAPTERS[name][0]())
    except Exception:
        return False


@pytest.fixture(params=["tesseract", "winocr"])
def only_engine(request, monkeypatch):
    name = request.param
    if not _engine_available(name):
        pytest.skip(f"OCR engine {name} is not available here")
    for other in ocr.ENGINE_ORDER:
        if other != name:
            monkeypatch.setitem(ocr._ADAPTERS, other, (lambda: False, ocr._ADAPTERS[other][1]))
    assert ocr.engines() == [name]
    return name


def test_plain_border_is_no_case_with_each_engine(only_engine):
    img, truth = _paper()
    res = analyze(_scanner_marks(img, truth), run_ocr=True)
    assert res["case"] is None, (only_engine, res["case"], res["text"], res["blocks"])
    assert res["warnings"] == [PLAIN] and res["hasText"] is False and res["text"] == ""


def test_written_border_is_read_with_each_engine(only_engine):
    img, truth = _paper(["Aunt Mary, Easter"])
    res = analyze(_scanner_marks(img, truth), run_ocr=True)
    assert res["case"] == "C" and res["engine"] == only_engine
    assert res["hasText"] is True and "Mary" in res["text"], res["text"]
    assert "|" not in res["text"] and "az" not in res["text"].split()


def test_caption_fixtures_are_read_with_each_engine(only_engine, fixtures_dir):
    from photoband.imageio import load_upright
    from photoband.existing import analyze_existing
    from test_pipeline import _md
    for name, case, word in (("11_other_tool_colored_band.png", "B", "Rosa"),
                             ("12_scanned_polaroid_handwriting.tif", "C", "Yosemite")):
        p = os.path.join(fixtures_dir, name)
        a, i = load_upright(p)
        res = analyze_existing(a, i, _md(p))
        assert res["case"] == case and res["engine"] == only_engine, (name, res["case"], res["engine"])
        assert res["hasText"] and word in res["text"], (name, res["text"])


# --------------------------------------------------------------------------- with or without OCR
# The evidence is the same whether OCR ran or not: the batch pre-flight runs WITHOUT OCR when
# both "Caption bands from other apps" and "Writing on the border" are Skip, and the save-time
# case check runs without it too. A single strip with a caption line in it was "photo" there, so
# an overwrite batch set to skip existing captions added a second band to the original.

def _bottom_strip(lines=("Aunt Mary, Easter 1952",), seed=43):
    photo = _make_photo(1000, 800, seed=seed)
    return framed(photo, (240, 236, 226), (0, 220, 0, 0), list(lines), font_px=58,
                  text_color=(40, 45, 90), font_path=ITALIC, texture=3.0)


@pytest.mark.parametrize("run_ocr", [False, True])
def test_single_strip_with_a_caption_is_a_case_with_or_without_ocr(run_ocr):
    img, truth = _bottom_strip()
    res = analyze(img, run_ocr=run_ocr)
    assert res["case"] in ("B", "C"), (res["case"], res["warnings"])
    assert res["band"]["photo_rect"] == list(truth)


def test_plain_single_strip_without_ocr_stays_photo():
    img, _ = _bottom_strip(lines=())
    res = analyze(img, run_ocr=False)
    assert res["case"] is None and res["blocks"] == []


def test_caption_fixtures_are_cases_without_ocr(fixtures_dir):
    from photoband.existing import analyze_existing
    from photoband.imageio import load_upright
    from test_pipeline import _md
    for name, case in (("11_other_tool_colored_band.png", "B"), ("12_scanned_polaroid_handwriting.tif", "C"),
                       ("14_near_white_sky.tif", None)):
        p = os.path.join(fixtures_dir, name)
        a, i = load_upright(p)
        res = analyze_existing(a, i, _md(p), run_ocr=False)
        assert res["case"] == case, (name, res["case"], res["warnings"])
        assert res["hasText"] is False


def test_unread_writing_is_still_writing():
    # OCR ran and read nothing from a caption line (Windows OCR reads no handwriting): that is
    # not proof of a plain border, the marks still count as they do without OCR
    line = [_blk((300, 2700, 900, 70))]
    for n in (1, 4):
        assert decide_case(_band(n), filter_ocr_lines(line, shape=SHAPE), 4.0, ocr_ran=True,
                           shape=SHAPE) == ("C", None)
    kept = filter_ocr_lines([_read((300, 2700, 900, 70), "~~ ||", 0.2)], shape=SHAPE)
    assert [ln.text for b in kept for ln in b.lines] == [""]      # junk read is not shown as text
    # a short upright mark on a single strip (the white wall's light switch) is not a caption line
    switch = [_blk((1118, 958, 15, 36))]
    assert decide_case(_band(1), switch, 0.0, ocr_ran=False, shape=(1100, 1500, 3))[0] is None
    assert decide_case(_band(1), switch, 0.0, ocr_ran=True, shape=(1100, 1500, 3))[0] is None


# --------------------------------------------------------------------------- any script
# Combining marks belong to the word: Bengali and Devanagari vowel signs, Thai tone marks and a
# decomposed accent are not punctuation.

@pytest.mark.parametrize("word", ["বাংলা", "हिन्दी", "ภาษาไทย", "Rene\u0301e", "e\u0301té"])
@pytest.mark.parametrize("conf", [0.9, ocr.UNKNOWN_CONFIDENCE])
def test_words_with_combining_marks_are_text(word, conf):
    blocks = [_read((300, 2700, 260, 70), word, conf)]
    assert detect.has_real_text(blocks, shape=SHAPE), word
    assert filter_ocr_lines(blocks, shape=SHAPE) == blocks
    assert decide_case(_band(1), blocks, 4.0, ocr_ran=True, shape=SHAPE) == ("C", None)


@pytest.mark.parametrize("junk", ["\u0301\u0301", "|\u0301", "--", "\u0e48"])
def test_marks_and_punctuation_alone_are_not_text(junk):
    assert not detect.has_real_text([_read((300, 2700, 260, 70), junk, 0.9)], shape=SHAPE)


# --------------------------------------------------------------------------- the print's own edge
# Real scans (woodbury-263, the README demo, and -034, -034b, -156b, -172b, lakearrowhead-008,
# -026) are prints lying on a white scanner bed. The candidate "lines" in their borders were the
# print's paper edge and curled corner against the bed, and whole plain paper margins: shaped like
# a line (2:1 or longer, thick enough) but made of ONE long piece plus specks, where writing is
# many glyph-sized pieces. They were called "writing on the border" without OCR (and, after OCR
# stopped vetoing unread marks, with it too).

def _print_on_bed(lines=(), seed=44):
    photo = _make_photo(1400, 900, seed=seed)
    pr, _ = framed(photo, (236, 228, 208), (70, 70, 70, 70), list(lines), font_px=46, text_color=(40, 45, 90),
                   font_path=ITALIC, texture=3.0)
    ph, pw = pr.shape[:2]
    H, W = ph + 260, pw + 300
    rng = np.random.default_rng(seed)
    bed = np.full((H, W, 3), 246, np.float32) + rng.normal(0, 1.0, (H, W, 1)).astype(np.float32)
    ox, oy = 140, 110
    bed[oy:oy + ph, ox:ox + pw] = pr
    # the print's edge against the bed: a soft grey shadow along the right and bottom edges,
    # widening into a curled corner (the 259x27 px candidate on woodbury-263)
    bed[oy + 6:oy + ph + 4, ox + pw:ox + pw + 4] -= 45
    bed[oy + ph:oy + ph + 4, ox + 6:ox + pw + 4] -= 45
    for i in range(30):
        bed[oy + ph - 2 + i // 3:oy + ph + i // 3 + 1, ox + pw - 260 + i * 8:ox + pw + 2] -= 3
    return np.clip(bed, 0, 255).astype(np.uint8)


@pytest.mark.parametrize("run_ocr", [False, True])
def test_plain_print_on_a_scanner_bed_is_no_case(run_ocr):
    res = analyze(_print_on_bed(), run_ocr=run_ocr)
    assert res["case"] is None, (res["case"], res["warnings"])
    assert res["hasText"] is False and res["blocks"] == []


@pytest.mark.parametrize("run_ocr", [False, True])
def test_written_print_on_a_scanner_bed_is_still_a_case(run_ocr):
    res = analyze(_print_on_bed(["Aunt Mary at the lake, 1952"]), run_ocr=run_ocr)
    assert res["case"] in ("B", "C"), (res["case"], res["warnings"])
    if run_ocr:
        assert "Mary" in res["text"], res["text"]


def test_written_print_on_a_scanner_bed_with_each_engine(only_engine):
    assert analyze(_print_on_bed(), run_ocr=True)["case"] is None
    res = analyze(_print_on_bed(["Aunt Mary at the lake, 1952"]), run_ocr=True)
    assert res["case"] in ("B", "C") and "Mary" in res["text"], (res["case"], res["text"])


def _inked(box, pieces):
    """A candidate line with its own ink: ``pieces`` are (x, y, w, h) rectangles inside the box."""
    x, y, w, h = box
    m = np.zeros((h, w), bool)
    for px, py, pw, ph in pieces:
        m[py:py + ph, px:px + pw] = True
    return TextBlock(box=box, lines=[TextLine(box=box, ink=(x, y, m))])


REAL_SHAPE = (2675, 3951, 3)   # woodbury-263
EDGE_MARKS = {
    # the curled corner of woodbury-263: one 259x27 wedge (and two specks)
    "corner wedge": _inked((3479, 2450, 259, 27), [(0, 20, 259, 4), (180, 10, 79, 17), (40, 2, 3, 2), (90, 5, 2, 2)]),
    # the print's side edge against the bed (woodbury-172b, lakearrowhead-026): one upright piece
    "side edge": _inked((4743, 3045, 56, 215), [(40, 0, 6, 215), (20, 190, 30, 25)]),
    # a whole plain paper margin (woodbury-034b, lakearrowhead-008): one long edge plus specks
    "paper margin": _inked((21, 1117, 302, 1634), [(280, 0, 22, 1634)] + [(40 + 9 * i, 100 * i, 4, 5) for i in range(12)]),
    # a dashed edge (woodbury-034): thin fragments along the line, none glyph-sized
    "dashed edge": _inked((4216, 1869, 41, 213), [(18, 10 + 25 * i, 5, 16) for i in range(8)]),
}


@pytest.mark.parametrize("name", sorted(EDGE_MARKS))
def test_paper_edges_are_not_writing(name):
    blk = EDGE_MARKS[name]
    assert detect._writing_shaped(blk.lines[0].box, REAL_SHAPE)       # the box alone looked like writing
    assert not ink_like([blk], REAL_SHAPE)
    for n in (1, 4):
        assert decide_case(_band(n), [blk], 4.0, ocr_ran=False, shape=REAL_SHAPE)[0] is None
        # OCR ran: a junk read on it, kept as an unread candidate, is still not writing
        assert filter_ocr_lines([blk], shape=REAL_SHAPE) == []


def test_a_row_of_glyphs_is_writing():
    # "HOTEL PORTLAND, PORTLAND, ORE." on church-misc-043: 26 glyph pieces in a 1368x48 line
    glyphs = [(i * 52, 6, 40, 36) for i in range(26)]
    blk = _inked((1607, 2677, 1368, 48), glyphs)
    assert ink_like([blk], REAL_SHAPE)
    assert decide_case(_band(1), [blk], 0.0, ocr_ran=False, shape=REAL_SHAPE) == ("B", None)
    # an underlined caption: the rule is one long piece and no glyphs, not "a long piece"
    under = _inked((1607, 2677, 1368, 60), glyphs + [(0, 54, 1368, 4)])
    assert ink_like([under], REAL_SHAPE)
    # two words in cursive (two pieces, neither spanning the line) still count
    assert ink_like([_inked((300, 2700, 600, 60), [(0, 5, 260, 50), (320, 5, 280, 50)])], REAL_SHAPE)
