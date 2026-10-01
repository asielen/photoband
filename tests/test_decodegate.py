"""Decode gate: size budget, priorities, and the shared full-array cache."""
import threading
import time

import numpy as np
import pytest

from photoband import decodegate, imageio, photos
from photoband.imageio import ImageError, ImageInfo


def _info(w, h, ch=3, dtype="uint8"):
    return ImageInfo(path="/x/huge.tif", format="TIFF", width=w, height=h, channels=ch, dtype=dtype, mode="RGB")


def test_budget_refuses_oversized_images(monkeypatch):
    monkeypatch.setattr(decodegate, "_mem", lambda: (8 * 2 ** 30, 4 * 2 ** 30))
    decodegate.check_budget(_info(10000, 10000, 3, "uint16"))          # 600 MB: fine
    with pytest.raises(ImageError, match="too large to open"):
        decodegate.check_budget(_info(40000, 40000, 3, "uint8"))      # 4.8 GB decoded
    # read_pixels refuses before decoding anything (decompression bombs included)
    with pytest.raises(ImageError, match="too large to open"):
        imageio.read_pixels(_info(60000, 60000))


def test_priority_order_and_single_slot_for_big_decodes(monkeypatch):
    monkeypatch.setattr(decodegate, "_mem", lambda: (8 * 2 ** 30, 2 * 2 ** 30))
    g = decodegate._Gate()
    big = 1800 * 2 ** 20        # two of these never fit the budget together
    order, running, peak = [], [0], [0]
    hold = threading.Event()

    def job(name, prio):
        with g.slot(big, prio):
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            order.append(name)
            if name == "first":
                hold.wait(5)
            running[0] -= 1

    t0 = threading.Thread(target=job, args=("first", decodegate.THUMB))
    t0.start()
    time.sleep(0.1)
    ts = [threading.Thread(target=job, args=(n, p)) for n, p in
          (("thumb", decodegate.THUMB), ("prefetch", decodegate.PREFETCH), ("current", decodegate.CURRENT))]
    for t in ts:
        t.start()
        time.sleep(0.05)
    hold.set()
    for t in [t0] + ts:
        t.join(10)
    assert order == ["first", "current", "prefetch", "thumb"]
    assert peak[0] == 1


def test_background_decodes_wait_for_foreground_work():
    g = decodegate._Gate()
    started = threading.Event()
    with g.foreground():
        t = threading.Thread(target=lambda: g.slot(10, decodegate.THUMB).__enter__() or started.set())
        t.start()
        assert not started.wait(0.3)
    assert started.wait(5)


def test_full_array_shared_and_dropped_when_idle(tmp_path, monkeypatch):
    from PIL import Image
    p = tmp_path / "a.png"
    Image.fromarray(np.full((40, 60, 3), 128, np.uint8)).save(p)
    calls = []
    real = photos.load_upright
    monkeypatch.setattr(photos, "load_upright", lambda *a, **k: calls.append(1) or real(*a, **k))
    photos._drop_full()
    photos.proxy_paths(str(p))                       # current photo: decodes once, builds the proxy
    photos.crop_webp(str(p), 0, 0, 20, 20, 20)       # reuses the decode
    assert len(calls) == 1
    assert photos.proxy.cached_proxy(str(p), imageio.probe(str(p))) is not None
    assert decodegate.stats()["pinned"] == 40 * 60 * 3
    photos._drop_full()
    assert decodegate.stats()["pinned"] == 0 and photos._full["arr"] is None
