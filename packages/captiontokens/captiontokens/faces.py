"""Face ordering and row grouping.

Boxes are normalized (0..1) top-left ``x, y, w, h`` in the upright image.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import List, Optional, Sequence


@dataclass
class Face:
    name: str
    box: Optional[Sequence[float]] = None  # x, y, w, h (normalized, top-left)
    source: str = ""

    @property
    def cx(self) -> float:
        return self.box[0] + self.box[2] / 2 if self.box else 0.0

    @property
    def cy(self) -> float:
        return self.box[1] + self.box[3] / 2 if self.box else 0.0


def order_names(faces: List[Face], order: str = "lr") -> List[str]:
    """Names left to right by box center x (``rl``: right to left). Faces
    without a position keep their metadata order and follow the positioned
    ones. ``meta`` keeps metadata order for everyone."""
    order = (order or "lr").lower()
    if order == "meta":
        return [f.name for f in faces]
    positioned = sorted((f for f in faces if f.box is not None), key=lambda f: (f.cx, f.cy))
    if order == "rl":
        positioned.reverse()
    return [f.name for f in positioned] + [f.name for f in faces if f.box is None]


def cluster_rows(faces: List[Face], factor: float = 0.6, rows: Optional[int] = None) -> List[List[Face]]:
    """Group faces into rows by center y, ordered front (lowest in frame) to back.

    Faces sorted by center y are cut into rows at the gaps that are large for THIS photo: a gap
    of at least ``factor`` x the median face height that is also at least 2.5 x the median gap
    between neighbours, or at least 1.2 x the median face height whatever the other gaps are.
    People standing side by side differ in height (and lean), so neighbouring heads in one row
    can be most of a face height apart; what sets a row behind them apart is a gap clearly bigger
    than those. Single linkage keeps a tilted row together. ``rows``: cut into exactly that many
    rows (at the largest gaps) instead. Faces without a position are ignored here."""
    positioned = [f for f in faces if f.box is not None]
    if not positioned:
        return [list(faces)] if faces else []
    order = sorted(positioned, key=lambda f: -f.cy)  # front (bottom) first
    gaps = [order[i].cy - order[i + 1].cy for i in range(len(order) - 1)]
    if rows is not None and rows >= 1:
        k = min(int(rows), len(order)) - 1
        cuts = set(sorted(range(len(gaps)), key=lambda i: -gaps[i])[:k])
    else:
        mh = median(f.box[3] for f in positioned)
        thr = max(factor * mh, min(1.2 * mh, 2.5 * median(gaps))) if gaps else 0.0
        cuts = {i for i, g in enumerate(gaps) if g >= thr}
    out: List[List[Face]] = [[order[0]]]
    for i, f in enumerate(order[1:]):
        if i in cuts:
            out.append([f])
        else:
            out[-1].append(f)
    for r in out:
        r.sort(key=lambda f: f.cx)
    return out
