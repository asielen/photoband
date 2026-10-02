"""_group_lines steps 2-3b: merges are found without rescanning every pair after every
merge (so a page of many lines stays fast), and a fragment joins a line only when it
touches it."""
import time

import numpy as np

from photoband import detect


def _line(x, y, n, h=26, w=14, step=16):
    return [(x + k * step, y, w, h) for k in range(n)]


def test_comma_touching_a_line_joins_it():
    boxes = _line(20, 100, 12) + [(20 + 6 * 16 - 4, 100 + 20, 5, 12)]   # tail overlaps the line by 6 px
    groups = detect._group_lines(np.array(boxes, float))
    assert len(groups) == 1


def test_tiny_label_a_few_px_under_a_line_stays_separate():
    # a 40 px line, and two 15 px characters 6 px under it ("a)"): used to join the line
    boxes = _line(20, 100, 14, h=40, w=20, step=22) + [(150, 146, 7, 15), (158, 146, 7, 15)]
    groups = detect._group_lines(np.array(boxes, float))
    assert sorted(len(g) for g in groups) == [2, 14]


def _naive_merge_fragments(lines, bb):
    """The original restart-after-every-merge loop, as the reference."""
    lines, bb = [list(g) for g in lines], [list(b) for b in bb]
    while len(lines) > 1:
        arr = np.array(bb, float)
        idx = np.arange(len(lines))
        F = detect._frag_pairs(arr, idx, idx)
        hit = np.flatnonzero(F.any(axis=1))
        if not hit.size:
            break
        s = int(hit[0])
        tgt = np.flatnonzero(F[s])
        vg = np.maximum(arr[s, 1], arr[tgt, 1]) - np.minimum(arr[s, 3], arr[tgt, 3])
        b = int(tgt[np.argmin(vg)])
        lines[b] = lines[b] + lines[s]
        bb[b] = [min(bb[b][0], bb[s][0]), min(bb[b][1], bb[s][1]), max(bb[b][2], bb[s][2]), max(bb[b][3], bb[s][3])]
        del lines[s], bb[s]
    return lines


def test_fragment_merge_matches_the_restarting_loop():
    rng = np.random.default_rng(7)
    merged_any = 0
    for _ in range(300):
        n = int(rng.integers(2, 25))
        x0 = rng.uniform(0, 300, n)
        y0 = rng.choice([0.0, 40.0, 80.0], n) + rng.uniform(-6, 30, n)
        hgt = rng.choice([rng.uniform(4, 12), rng.uniform(24, 32)], n) if False else \
            np.where(rng.random(n) < 0.5, rng.uniform(4, 12, n), rng.uniform(24, 32, n))
        wid = np.where(hgt < 13, rng.uniform(2, 10, n), rng.uniform(60, 200, n))
        bb = np.stack([x0, y0, x0 + wid, y0 + hgt], axis=1).round()
        lines = [[i] for i in range(n)]
        got = detect._merge_fragments(lines, bb)
        assert got == _naive_merge_fragments(lines, bb)
        merged_any += len(got) < n
    assert merged_any > 30


def test_many_lines_group_quickly():
    # 400 lines, each split in two groups across a 2-x-height gap (step 2 merges them) and
    # each with a comma tail (step 3b): the old loops rescanned every pair after every
    # merge, O(lines^3): ~6 s for 100 lines, minutes for 400
    boxes = []
    for i in range(400):
        y = 40 + 60 * i
        boxes += [(20, y, 120, 26), (180, y, 120, 26), (290, y + 22, 5, 12)]
    b = np.array(boxes, float)
    t = time.perf_counter()
    groups = detect._group_lines(b)
    dt = time.perf_counter() - t
    assert len(groups) == 400 and all(len(g) == 3 for g in groups)
    assert dt < 5.0, dt


def test_many_fragments_merge_quickly():
    n = 1500
    bb = np.array([[0, 30 * i, 600, 30 * i + 26] for i in range(n)] +
                  [[100, 30 * i + 20, 105, 30 * i + 32] for i in range(n)], float)
    t = time.perf_counter()
    got = detect._merge_fragments([[i] for i in range(2 * n)], bb)
    assert len(got) == n
    assert time.perf_counter() - t < 5.0
