"""A plain border is not an existing caption (cases B and C need writing in the border).

Regression: a scanned print with a plain white paper border and nothing written on it was
called case C ("Handwriting or printing on the photo's border", with the band detector's edge
confidence shown as "83% sure"), while a quiet note said "A plain border was found with no
text in it." The scan cues (paper grain, soft edge, lighting drift, 800 dpi) only say what
KIND of border it is; the candidate marks were dust, a hair and the print's own paper edge
against the scanner lid.
"""
from __future__ import annotations

import numpy as np
import pytest

from photoband import detect
from photoband.detect import BandResult, TextBlock, TextLine, decide_case, ink_like
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
