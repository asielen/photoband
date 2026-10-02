"""OCR engine bookkeeping: an engine is turned off only for a binding problem it has had
from the start, the engines-used list is ordered deterministically, and image size
limits are applied to the image the engine actually gets (after padding)."""
import numpy as np
import pytest

from photoband import ocr
from photoband.detect import TextBlock, TextLine


def _ink():
    img = np.full((40, 120, 3), 255, np.uint8)
    img[12:28, 10:110] = 0
    return img


@pytest.fixture
def fake_engines(monkeypatch):
    """vision / winocr / tesseract replaced by controllable fakes; bookkeeping reset."""
    calls = {"vision": [], "winocr": [], "tesseract": []}
    behaviour = {"vision": "ok", "winocr": "ok", "tesseract": "ok"}

    def make(name):
        def run(img):
            calls[name].append(1)
            b = behaviour[name]
            if isinstance(b, list):
                b = b.pop(0) if b else "ok"
            if isinstance(b, BaseException):
                raise b
            return {"text": name, "confidence": 0.9, "words": [], "engine": name}
        return (lambda: True, run)

    monkeypatch.setattr(ocr, "_ADAPTERS", {n: make(n) for n in ocr.ENGINE_ORDER})
    monkeypatch.setattr(ocr, "_broken", set())
    monkeypatch.setattr(ocr, "_ok", set())
    return calls, behaviour


def test_type_error_before_any_success_turns_the_engine_off(fake_engines):
    calls, beh = fake_engines
    beh["vision"] = TypeError("binding changed")
    assert ocr.recognize(_ink())["engine"] == "winocr"
    assert "vision" in ocr._broken
    assert ocr.recognize(_ink())["engine"] == "winocr"
    assert len(calls["vision"]) == 1          # not tried again


def test_type_error_after_a_success_fails_only_that_crop(fake_engines):
    calls, beh = fake_engines
    beh["vision"] = ["ok", TypeError("odd crop"), "ok"]
    assert ocr.recognize(_ink())["engine"] == "vision"
    assert ocr.recognize(_ink())["engine"] == "winocr"   # this crop falls through
    assert "vision" not in ocr._broken
    assert ocr.recognize(_ink())["engine"] == "vision"   # and the engine is still used


@pytest.mark.parametrize("exc", [ImportError("gone"), AttributeError("renamed")])
def test_other_binding_errors_follow_the_same_rule(fake_engines, exc):
    calls, beh = fake_engines
    beh["vision"] = ["ok", exc]
    ocr.recognize(_ink())
    ocr.recognize(_ink())
    assert "vision" not in ocr._broken


def test_ordinary_errors_never_turn_an_engine_off(fake_engines):
    calls, beh = fake_engines
    beh["vision"] = RuntimeError("busy")
    for _ in range(3):
        assert ocr.recognize(_ink())["engine"] == "winocr"
    assert not ocr._broken and len(calls["vision"]) == 3


def test_a_transient_probe_failure_is_not_cached():
    state = {"n": 0}

    @ocr._probe_once
    def probe():
        state["n"] += 1
        if state["n"] == 1:
            raise OSError("service busy")
        return True

    assert probe() is False      # this call only
    assert probe() is True       # asked again, and the answer is now kept
    assert probe() is True and state["n"] == 2

    @ocr._probe_once
    def missing():
        state["n"] += 1
        raise ImportError("no package")

    n = state["n"]
    assert missing() is False and missing() is False
    assert state["n"] == n + 1   # a missing package is a definitive answer


def test_engines_used_ties_go_to_the_preferred_engine(monkeypatch):
    # one line each: a tie; set iteration order (hash seed) must not decide
    import threading
    lock = threading.Lock()
    seq = ["tesseract", "vision", "winocr"]

    def fake(crop):
        with lock:
            eng = seq.pop(0)
        return {"text": "x", "confidence": 1.0, "words": [], "engine": eng}

    monkeypatch.setattr(ocr, "recognize", fake)
    arr = np.full((200, 200, 3), 255, np.uint8)

    def blocks(n):
        return [TextBlock(box=(0, 0, 10, 10), lines=[TextLine(box=(10, 10 + 20 * i, 50, 12)) for i in range(n)])]

    assert ocr.recognize_blocks(arr, blocks(3)) == ["vision", "winocr", "tesseract"]
    seq[:] = ["tesseract", "tesseract", "winocr"]
    assert ocr.recognize_blocks(arr, blocks(3)) == ["tesseract", "winocr"]


@pytest.mark.parametrize("shape", [(40, 9968), (40, 9980), (40, 10100), (60, 20000), (20, 3000), (9990, 30)])
def test_size_limit_counts_the_padding(shape):
    for maxdim, border in ((10000, 32), (ocr.TESSERACT_MAX_DIM, 2 * ocr.PAD)):
        s = ocr._fit_scale(4.0, shape, maxdim, border)
        resized = max(1, round(max(shape) * s))
        if abs(s - 1.0) <= 0.01 and ocr._fits(shape, maxdim, border):
            resized = max(shape)       # the callers skip a resize this close to 1
        assert resized + border <= maxdim, (shape, maxdim, s)


def test_tesseract_prepare_stays_under_its_limit():
    # a long thin line: upscaled ~6x it would be ~36000 px wide
    img = np.full((12, 6000), 255, np.uint8)
    img[3:9, 10:5990:7] = 0
    g, scale, pad, _ = ocr._prepare(img)
    assert max(g.shape) <= ocr.TESSERACT_MAX_DIM


@pytest.mark.skipif(not ocr._winocr_available(), reason="Windows.Media.Ocr not available")
@pytest.mark.parametrize("w", [9980, 10050])
def test_winocr_reads_a_crop_near_its_size_limit(w):
    img = np.full((60, w, 3), 255, np.uint8)
    import cv2
    for x in range(40, w - 300, 400):
        cv2.putText(img, "HELLO", (x, 45), cv2.FONT_HERSHEY_SIMPLEX, 1.4, (0, 0, 0), 3)
    r = ocr._winocr_recognize(img)       # 9980 px used to raise "Image dimensions are too large"
    assert r["text"].upper().count("HELLO") >= 10
