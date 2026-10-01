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


def cluster_rows(faces: List[Face], factor: float = 0.6) -> List[List[Face]]:
    """Group faces into rows by center y, ordered front (lowest in frame) to back.

    Two faces share a row when their centers differ by less than
    ``factor`` x the median face height. Rows are the connected groups of
    that rule (single linkage): faces sorted by center y are chained while
    each gap to the next face is below the threshold, so a tilted row stays
    together. Faces without a position are ignored here.
    """
    positioned = [f for f in faces if f.box is not None]
    if not positioned:
        return [list(faces)] if faces else []
    mh = median(f.box[3] for f in positioned)
    thr = factor * mh
    rows: List[List[Face]] = []
    prev = None
    for f in sorted(positioned, key=lambda f: -f.cy):  # front (bottom) first
        if rows and prev is not None and abs(prev.cy - f.cy) < thr:
            rows[-1].append(f)
        else:
            rows.append([f])
        prev = f
    for r in rows:
        r.sort(key=lambda f: f.cx)
    return rows
