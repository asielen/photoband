"""Hidden band marker: a robust marker plus a fragile payload hidden in the
background pixels of a caption band.

A captioned file should still identify itself after its metadata has been
stripped.  :func:`embed` writes two independent layers into the *background*
pixels of the band (never photo pixels, never text or its neighbourhood, never
pixels of the caller's ``exclude`` mask); :func:`read` finds them again and
:func:`scrub` removes them (erase in place, re-caption, "Remove hidden
marker").

Everything marked FORMAT below is the on-disk contract.  Files written by
format version 1 (the first release) are still read; new files are written as
version 2.  Changing a FORMAT constant breaks old files: bump ``VERSION``.

Units and conventions
=====================
* Canvas: numpy ``H x W`` or ``H x W x C`` (C = 1, 2, 3, 4; for C = 2/4 the
  last channel is alpha and is never touched), dtype uint8 or uint16, upright.
* "Level" = one 8-bit unit.  For uint16 canvases a level is 257 units.
* Luma = mean of the colour channels (in levels).
* ``diff8(p)`` = max over colour channels of ``|p_c - band_c|`` in levels.
* Anchor band: the largest (by area) of the four bands around the photo.  It
  is viewed in "anchor orientation" (top: flipped vertically, right:
  transposed, left: transposed then flipped) so it always lies below the photo
  edge ``y0`` and spans the full canvas dimension ``base`` (the canvas width
  for a bottom/top anchor, the height for a right/left anchor).

Robust marker (layer 1), version 2
==================================
Data block, 32 bytes (FORMAT), big endian::

    0   2  magic  b"PB"
    2   1  version (2)
    3   1  flags: bits 0-1 anchor side (0 bottom, 1 top, 2 right, 3 left)
                  bit  2   fragile payload present
                  bits 3-4 grain modulation depth, index into DEPTHS
                           (0.5, 0.42, 0.35, 0.3)
                  bits 5-7 reserved, 0
    4  10  photo rect x, y, w, h: 4 x 20 bit = round(v / base * 2**18)
    14  2  canvas aspect: round(other / base * 2**14), saturating at 0xFFFF
           (``other`` = the canvas dimension across the band)
    16  3  band colour, 8-bit R, G, B (gray canvases store g, g, g)
    19  8  short photo hash (first 8 bytes of the caller's hex hash)
    27  2  payload length in bytes (uint16)
    29  1  payload salt (binds payload units to this save)
    30  2  base at embed time (uint16, saturating)

The rect is normalised by ``base`` only, so cropping the far edge of the band
leaves it exact and a uniform resize scales it; 2**18 fixed point gives
< 0.5 px error for any base below 2**17 px.  The aspect lets the reader tell a
uniform resize or a far-edge crop (fine) from a side crop, padding or a
non-uniform resize (the stored rect is then no longer exact, see "Reading").

Channel code (FORMAT): the 32 bytes are Reed-Solomon encoded
(``reedsolo.RSCodec(16)``, GF(256), systematic) to 48 bytes = 384 bits, MSB
first, which are then convolutionally encoded (rate 1/2, constraint length 7,
generators 171/133 octal, terminated with 6 zero bits) to ``NCODED`` = 780
code bits.  The decoder is soft-decision Viterbi followed by RS; this is what
lets one or two copies of the code survive a 50 % resize plus JPEG q70 on a
thin, text-heavy band (repetition + RS alone needed four or more).  Because
the data starts with the known magic and version, the first 48 code bits are
a known sync word, used to reject wrong grids and edges cheaply.

Grid.  ``B = base / NB`` (float) with ``NB`` from ``NB_CANDIDATES``.  A pixel
with centre ``(x + .5, y + .5)`` (anchor orientation) is in block column
``c = floor((x + .5) / B)`` and block row ``r = floor((y + .5 - y0) / B)``;
only full rows are used.  Each block is split into ``CELLS x CELLS`` = 4 x 4
chips; chip ``(i, j) = (floor(4 fy), floor(4 fx))`` of the fractional
position in the block.  Because B is a fixed fraction of the canvas, a
resized copy has exactly the same grid (the decoder tries every NB).

Pattern.  Block ``k = r * NB + c`` carries code bit ``perm_t[k % NCODED]``,
``t = k // NCODED``, with ``perm_t = numpy.random.RandomState(PERM_SEED +
t).permutation(NCODED)`` (every code bit once per copy, at a different place
in every copy).  From the splitmix64 finaliser ``z`` of k (:func:`_mix`):
``pn`` = bit 40 (+-1, scrambles the data) and ``shape = (z >> 44) % 5``
indexes ``SHAPES``, five two-region chip patterns (FORMAT)::

    0 halves top/bottom   1 halves left/right   2 2x2 checker
    3 diagonal staircase  4 anti-diagonal staircase

The unit pattern of the block is ``P = (2 bit - 1) * pn * SHAPES[shape][i, j]``
in {-1, +1}: a random mosaic of blotches at the scale of B/2 with no periodic
component (the mean over the random signs is zero everywhere, so a stretched
band has no spectral line at 1/B).  It is written as *grain*: one random byte
per pixel (shared by the colour channels so gray stays gray) decides whether
the pixel takes the "+" step, with probability ``0.5 + m P`` for modulation
depth ``m`` (``DEPTHS``, stored in the flags):

* two-sided band colours: every free pixel moves +1 ("+") or -1 level;
* a channel >= 254 (white): only darkens, 0 ("+") or -2 levels, a mean of
  -1 level (a uniform tint, not a pattern);
* a channel <= 1: the mirror image (only brightens).

So each block's sign is spread over per-pixel chips: at 1:1 the band looks
like fine grain and only the local grain density carries the data, at a scale
that JPEG's DC and lowest AC terms keep.  ``m`` = 0.5 makes every pixel of a
chip move the same way (most robust, a crisp mosaic after a levels stretch);
lower depths leave more random grain in each chip.  The encoder picks the
depth from the margin it has, ``copies * (B / 16)**2``: < 1.5 -> 0.5,
< 2.5 -> 0.42, < 6 -> 0.35, else 0.3 (see ``_depth_index``).

Pixels written: in the anchor band grid, outside the photo (and within 4 px
of it), outside ``exclude`` and outside the text mask (``diff8 > max(1.5,
4 x median band diff8)``, capped at 16 levels) grown by ``TEXT_GROW`` = 4 px.
Change <= 2 levels.

Encoder grid choice (in copies of the 780-bit code, counting blocks that are
at least 20 % writable): the largest block with B >= 16 px and >= 3 copies;
else the largest with B >= 12 px (6 px after a 50 % resize) and >= 2 copies;
else B >= 12 px with the most copies if >= 1; else any B >= 8 px with the
most copies if >= 0.7; otherwise the marker is skipped and the reason logged.
Fewer than 1.5 copies are noted in the save log.  The 256 x 64 px minimum of
writable background is counted in the anchor band.

Decoding (per candidate NB, on a luma decimated so a block is ~8 px): chips
are summed separably over pixels within ``TAU`` levels of the band median,
dropping pixels within ~5 px of anything farther off (text, its JPEG ringing,
the photo edge, dust); each block's amplitude is the least-squares fit of its
shape with the block mean removed, clipped to +-3, weighted by its coverage
and ``1 / (1 + (var/9)**2)``, multiplied by ``pn`` and soft-voted per code
bit.  Sync check (<= 30 % of the 48 known bits wrong) -> Viterbi -> RS ->
accept only when magic, version, reserved bits and anchor side match.  The
photo edge from band detection is refined by trying ``y0 +- DY_SEARCH`` px;
``sweep=True`` additionally sweeps the edge across the anchor band in steps of
B/4 (sync bits only, vectorised), for a detection that is confidently wrong
(e.g. a high-key photo).  Grids much finer than the encoder would pick for the
band's shape are not tried, and only bands at least half the area of the
largest are.

Fragile payload (layer 2), version 2
====================================
Stored in the least significant bit of every colour channel (not alpha) of
whole 16 x 16 tiles (``TILE``, grid from the canvas's top-left corner) whose
pixels are all writable (see above; tiles never touch the photo).  For uint16
the native 16-bit LSB.  One tile = one self-identifying *unit* of
``U = 32 * cc`` bytes (cc = colour channels), bits MSB first in pixel
row-major, channel-minor order::

    0  1  salt (from the robust marker)
    1  2  unit index (uint16)
    3  D  payload bytes [index * D, (index + 1) * D), zero padded, D = U - 7
    U-4 4 crc32(hash8 + bytes 0 .. U-5)

Units are placed in up to ``PAYLOAD_COPIES`` = 3 copies, in the anchor band
alone when it has room for all three (else in all bands); the usable tiles are
ordered along the anchor band and each copy gets its own contiguous third, so
a scribble or dust spot in one place costs at most one copy of a few units.
The reader checks the salt/index header of every tile (vectorised), then the
CRC of the candidates, and needs one valid copy of every unit.  Because units
carry their index, a tile that turns busy (dust, a scribble) never shifts the
others, and there is no threshold to agree on: text-heavy bands only lose the
tiles that touch text.  The reader only tries when the canvas geometry (base
and aspect) is the one saved.  Payloads are limited to 65535 bytes; an empty
payload reads back as None.

The LSB is set after the robust layer by moving a mismatching value by one
level towards whichever side keeps it within 2 levels of the original.

Reading
=======
:func:`read` returns the decoded data plus ``rect_ok``.  An untouched geometry
(same base and aspect) is trusted.  Otherwise the rect is trusted only when
(a) it fits the canvas, (b) the aspect shows no side crop, padding or
non-uniform stretch (a far-edge crop is fine), and (c) no photo edge
contradicts it: for every photo edge next to a band, the strongest step of the
row (column) profile of ``median |L - band|`` within +-max(6 px, 5 %) must sit
at the predicted pixel boundary (canvas of the saved size) or within 0.6 px of
the fractional predicted position (resized canvas, centroid of the step).
Edges that show no step at all (high-key photos) neither confirm nor refute.

Version 1 (read only)
=====================
Same data block except: rect 4 x uint24 at 2**22 (bytes 4-15), flags bits 2-4
fragile threshold index into ``V1_T_CANDIDATES``, bit 5 payload present, bits
6-7 reserved; payload length uint24 (27-29); no aspect or salt; no
convolutional code (the 384 RS bits repeat in tiles of 384 blocks, hard
decisions + RS with erasures).  Grid ``V1_NB_CANDIDATES``, pattern ``K(fy, fx)
= cos(pi fx) - o cos(pi fy)`` (o = bit 50 of the mix), two-sided +-1.5
levels, white/black half-wave.  Payload: LSB of the free pixels of 16 px
tiles that are not busy at threshold T (tile max diff8 > T, dilated 3x3, or
touching the photo), 3 sequential copies of ``[len uint32][data][crc32]`` at
slot ``(k * Q) % N``.  :func:`_embed_v1` still writes it, for the
backward-compatibility tests only.

Survival and limits (measured on real saves of the five built-in template
geometries, 8 and 16 bit; tests/test_marker_pipeline.py)
====================================================================
* Lossless copies (PNG/TIFF, metadata stripped): marker, exact rect, payload.
* JPEG q70 and up (q60 from 1600 px); 50 % resize (any filter) followed by
  JPEG q70 from 1600 px photo width on every template; 33 % resize (+ q85)
  from 1600 px; 25 % resize (+ q85) from 3000 px; cropping half of the far
  band edge (+ q85) from 1600 px (800 px: only where the band is tall).
* Not survived: 50 % + q70 below ~1600 px (blocks under 12 px at the saved
  size, or less than one code copy in a thin, text-heavy band), 25 % below
  ~3000 px, q60 on 800 px thin bands, side crops (the grid follows the canvas
  width; for 1-8 px crops the marker may still decode, but its rect is
  rejected and detection is used), padding, printing and rescanning, heavy
  filters.
* Payload: lossless copies only; any lossy step or resize loses it (by
  design; the reader then falls back to marker-only or detection).
* Invisibility: +-1 level of grain (white: 0/-2).  After a +3 stop levels
  stretch the band shows grain with a blotchy density variation at the block
  scale (random, no grid, no spectral line at 1/B); the lower depths used on
  roomy bands look more like film grain.  At 400 % without a stretch it is not
  visible.
* Speed: 12000 px wide canvas, 8 or 16 bit: embed ~0.8 s, read ~0.3 s (no
  marker: ~0.7 s; with the edge sweep ~1.2-2 s), scrub ~1.5 s; < 150 MB extra.
"""
from __future__ import annotations

import math
import zlib
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

import cv2
import numpy as np
from reedsolo import ReedSolomonError, RSCodec

__all__ = ["EmbedResult", "MarkerInfo", "embed", "read", "scrub", "short_hash"]

# --------------------------------------------------------------------------
# FORMAT constants (changing any of these breaks old files)
# --------------------------------------------------------------------------
MAGIC = b"PB"
VERSION = 2                                 # written; 1 and 2 are read
DATA_LEN = 32
ECC_LEN = 16
NBITS = (DATA_LEN + ECC_LEN) * 8           # 384 coded bits
PERM_SEED = 0x5042                          # block -> bit permutations
TILE = 16
PHOTO_MARGIN = 4
SIDES = ("bottom", "top", "right", "left")
# version 2
NB_CANDIDATES = (16, 18, 20, 23, 26, 29, 32, 36, 40, 45, 50, 56, 63, 70, 78, 87, 97, 108, 121, 135, 151, 169, 189,
                 211, 236, 264, 295, 330, 369, 413, 462, 512)
CELLS = 4
_I, _J = np.mgrid[0:CELLS, 0:CELLS]
SHAPES = np.array([np.where(_I < 2, 1, -1),
                   np.where(_J < 2, 1, -1),
                   np.where((_I < 2) == (_J < 2), 1, -1),
                   np.where(_I + _J <= 3, 1, -1),
                   np.where(_I + (3 - _J) <= 3, 1, -1)], np.int8)
CONV_POLYS = (0o171, 0o133)                 # rate 1/2, constraint length 7 (the NASA code)
CONV_K = 7
NCODED = 2 * (NBITS + CONV_K - 1)           # 780 coded bits per copy (terminated)
DEPTHS = (0.5, 0.42, 0.35, 0.3)             # grain modulation depth, flags bits 3-4
RECT_FRAC_BITS = 18                         # 20-bit fields, range [0, 4)
ASPECT_FRAC_BITS = 14                       # 16 bits, range [0, 4)
UNIT_HEAD = 3                               # salt + uint16 index
UNIT_CRC = 4
PAYLOAD_COPIES = 3
# version 1 (read only)
V1_NB_CANDIDATES = (24, 32, 48, 64, 96, 128, 192, 256, 384)
V1_PAIR = ((0, 1), (1, 0))
V1_RECT_FRAC_BITS = 22
V1_T_CANDIDATES = (8, 12, 16, 20, 24, 32, 40, 48)
GOLDEN = 0.6180339887498949

# --------------------------------------------------------------------------
# Tunables (encoder/decoder behaviour, not part of the format)
# --------------------------------------------------------------------------
MIN_USABLE_AREA = 256 * 64      # px of writable background in the anchor band
B_TARGET = 12.0                 # px; >= 6 px after a 50 % resize
B_LARGE = 16.0
TILES_TARGET = 3.0              # copies of the 780-bit code
TILES_OK = 2.0
TILES_MIN = 0.7
TILES_NOTE = 1.5                # fewer copies are noted in the save log
MIN_BLOCK_EMBED = 8.0
MIN_BLOCK_DECODE = 3.0          # px; smallest block the reader tries
BLOCK_MIN_FREE = 0.2            # a block counts as usable above this
GRAIN = 2                       # levels, one-sided grain step
MAX_DELTA = 2                   # levels, hard clamp
TEXT_GROW = 4                   # px (>= 4 required by the spec)
TEXT_MIN_THRESHOLD = 1.5        # levels
TEXT_MAX_THRESHOLD = 16.0       # levels
MAX_ERASURES = 12               # RS erasures tried (<= ECC_LEN - 4 keeps a check)
DY_SEARCH = 4                   # px around the approximate photo edge
CLIP_ESTIMATE = 3.0             # levels
VAR_SCALE = 9.0                 # levels^2
TAU = 6.0                       # levels; decoder pixel mask around the band median
WORK_BLOCK = 8.0                # px per block at the decoder's working resolution
RING_GROW = 5.0                 # px (at the image's own scale) dropped around off-band pixels
SCRUB_RANGE = 3                 # levels; flat-band scrub flattens pixels this close
CHUNK = 1 << 22                 # pixels per processing chunk (memory bound)
# v1 decoder/legacy writer tunables
V1_MIN_TILES = 4
V1_MIN_BLOCK_EMBED = 5.0
V1_MIN_BLOCK_DECODE = 2.4
V1_BLOCK_FREE_FRACTION = 0.6
V1_AMP_TWO_SIDED = 1.5
V1_AMP_ONE_SIDED = 2.0
V1_WHITE_GAIN = 1.5
V1_T_MARGIN = 3
V1_DEFAULT_T_INDEX = 2

_RS = RSCodec(ECC_LEN)


def _depth_index(b: float, copies: float) -> int:
    """Grain modulation depth (index into ``DEPTHS``) for block size b (px at
    the saved size) and the number of code copies that fit.

    0.5 = every pixel of a chip moves the same way (most robust, a crisp
    mosaic after a levels stretch); lower values leave more random grain in
    each chip (looks like film grain after a stretch) at the cost of
    robustness.  Bands with margin to spare (large blocks, several copies)
    get the lower depths."""
    score = copies * (b / 16.0) ** 2
    if score < 1.5:
        return 0
    if score < 2.5:
        return 1
    if score < 6.0:
        return 2
    return 3


# --------------------------------------------------------------------------
# Public types
# --------------------------------------------------------------------------
@dataclass
class EmbedResult:
    robust: bool            # robust marker written
    payload: bool           # fragile payload written
    reason: str = ""        # why something was skipped (for the save log)
    nb: int = 0             # grid used (diagnostics)
    tiles: float = 0.0      # how many times the bit sequence is repeated


@dataclass
class MarkerInfo:
    version: int
    photo_rect: tuple       # (x, y, w, h) integer pixels in THIS canvas (see rect_ok)
    photo_rect_norm: tuple  # (x, y, w, h) as stored, in units of the base
                            # (band-parallel) canvas dimension at embed time
    band_color: tuple       # (r, g, b) 8-bit values as stored
    short_hash: str         # 16 hex chars
    payload: Optional[bytes]  # None if the fragile layer is missing/corrupt
    payload_len: int
    side: str = "bottom"    # anchor band the marker was found in
    nb: int = 0             # grid that decoded (diagnostics)
    rect_ok: bool = True    # photo_rect is exact for this canvas (validated)
    geometry: str = "same"  # same | resized | cropped | changed
    raw: bytes = field(default=b"", repr=False)   # the decoded 32-byte data block


def short_hash(photo_hash: str) -> str:
    """First 16 hex chars (8 bytes) of a hex photo hash, lower case."""
    return photo_hash[:16].lower()


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _as3(canvas: np.ndarray) -> np.ndarray:
    """H x W x C view of the canvas (writes go through to the caller)."""
    if canvas.ndim == 2:
        return canvas[:, :, None]
    if canvas.ndim != 3:
        raise ValueError("canvas must be H x W or H x W x C")
    return canvas


def _color_channels(c3: np.ndarray) -> int:
    c = c3.shape[2]
    if c not in (1, 2, 3, 4):
        raise ValueError(f"unsupported channel count {c}")
    return c - 1 if c in (2, 4) else c


def _scale(dtype: np.dtype) -> int:
    if dtype == np.uint8:
        return 1
    if dtype == np.uint16:
        return 257
    raise ValueError(f"unsupported dtype {dtype}")


def _band8(band_color: tuple, cc: int, scale: int) -> Tuple[Tuple[int, ...], Tuple[int, int, int]]:
    bc = [int(v) for v in band_color]
    if len(bc) < cc:
        bc = [bc[0]] * cc
    b8c = tuple(int(min(255, max(0, round(v / scale)))) for v in bc[:cc])
    b8 = (b8c[0], b8c[1], b8c[2]) if cc == 3 else (b8c[0],) * 3
    return b8c, b8


def _diff8(c3: np.ndarray, cc: int, scale: int, band8: Tuple[int, ...]) -> np.ndarray:
    out: Optional[np.ndarray] = None
    for ch in range(cc):
        d = np.abs(c3[:, :, ch].astype(np.float32) * (1.0 / scale) - float(band8[ch]))
        out = d if out is None else np.maximum(out, d, out=out)
    assert out is not None
    return out


def _diff_units(block: np.ndarray, cc: int, band_units: Tuple[int, ...]) -> np.ndarray:
    """max_c |p_c - band_c| in canvas units (int32), for a chunk."""
    out: Optional[np.ndarray] = None
    for ch in range(cc):
        d = np.abs(block[:, :, ch].astype(np.int32) - int(band_units[ch]))
        out = d if out is None else np.maximum(out, d, out=out)
    assert out is not None
    return out


def _orient(a: np.ndarray, side: int) -> np.ndarray:
    """View of a 2-D (or H x W x C) array with the given band below the photo."""
    if side == 0:
        return a
    if side == 1:
        return a[::-1]
    t = a.transpose((1, 0) + tuple(range(2, a.ndim)))
    return t if side == 2 else t[::-1]


def _side_geometry(rect: Tuple[int, int, int, int], h: int, w: int) -> List[Tuple[int, int, int]]:
    """[(side, y0 in oriented coords, thickness)] for the four bands."""
    px, py, pw, ph = rect
    return [
        (0, py + ph, h - (py + ph)),
        (1, h - py, py),
        (2, px + pw, w - (px + pw)),
        (3, w - px, px),
    ]


def _mix(k: np.ndarray) -> np.ndarray:
    z = k.astype(np.uint64) + np.uint64(0x9E3779B97F4A7C15)
    z = (z ^ (z >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
    z = (z ^ (z >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
    return z ^ (z >> np.uint64(31))


def _pn(k: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Two pseudo-random +-1 values per block index: bits 40 and 50 of the
    splitmix64 finaliser of k (v1: sign, orientation)."""
    z = _mix(k)

    def bit(n: int) -> np.ndarray:
        return np.where(((z >> np.uint64(n)) & np.uint64(1)) == 1, 1.0, -1.0).astype(np.float32)

    return bit(40), bit(50)


_PERMS: List[np.ndarray] = []


def _bit_index(n: int) -> np.ndarray:
    """Coded-bit index of block k = 0..n-1.

    Blocks are taken in runs of NBITS ("tiles"); tile t is the permutation
    ``numpy.random.RandomState(PERM_SEED + t).permutation(NBITS)`` (the legacy
    RandomState stream is frozen by NumPy, NEP 19).  Every bit occurs once per
    tile, and its position changes from tile to tile, so text or a crop that
    hides one area never removes the same bit from every copy.
    """
    tiles = -(-n // NBITS)
    while len(_PERMS) < tiles:
        _PERMS.append(np.random.RandomState(PERM_SEED + len(_PERMS)).permutation(NBITS))
    return np.concatenate(_PERMS[:tiles])[:n] if tiles else np.zeros(0, np.int64)


_PERMS2: List[np.ndarray] = []


def _bit_index2(n: int) -> np.ndarray:
    """v2: coded-bit index of block k = 0..n-1 (runs of NCODED blocks, run t
    is ``RandomState(PERM_SEED + t).permutation(NCODED)``)."""
    runs = -(-n // NCODED)
    while len(_PERMS2) < runs:
        _PERMS2.append(np.random.RandomState(PERM_SEED + len(_PERMS2)).permutation(NCODED))
    return np.concatenate(_PERMS2[:runs])[:n] if runs else np.zeros(0, np.int64)


def _conv_tables() -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    ns = 1 << (CONV_K - 1)
    nxt = np.zeros((ns, 2), np.int64)
    out = np.zeros((ns, 2, 2), np.int8)
    for st in range(ns):
        for u in (0, 1):
            reg = (u << (CONV_K - 1)) | st
            nxt[st, u] = reg >> 1
            out[st, u] = [bin(reg & g).count("1") & 1 for g in CONV_POLYS]
    prev = np.zeros((ns, 2), np.int64)
    pin = np.zeros(ns, np.int64)
    for n_ in range(ns):
        u = n_ >> (CONV_K - 2)
        prev[n_] = [st for st in range(ns) if nxt[st, u] == n_]
        pin[n_] = u
    return nxt, out, prev, pin


_NXT, _OUT, _PREV, _PIN = _conv_tables()
_O0 = (2 * _OUT[_PREV[:, 0], _PIN] - 1).astype(np.float64)   # branch outputs (+-1) per next state
_O1 = (2 * _OUT[_PREV[:, 1], _PIN] - 1).astype(np.float64)


def _conv_encode(bits: np.ndarray) -> np.ndarray:
    """Terminated rate-1/2 convolutional code (FORMAT): out[2t], out[2t+1]."""
    st = 0
    out = np.empty(2 * (len(bits) + CONV_K - 1), np.int8)
    for t, u in enumerate(list(int(b) for b in bits) + [0] * (CONV_K - 1)):
        out[2 * t: 2 * t + 2] = _OUT[st, u]
        st = _NXT[st, u]
    return out


def _viterbi(v: np.ndarray) -> np.ndarray:
    """Soft-decision Viterbi decoding of :func:`_conv_encode` (votes > 0 mean 1)."""
    steps = v.size // 2
    pm = np.full(_PIN.size, -1e30)
    pm[0] = 0.0
    dec = np.zeros((steps, _PIN.size), bool)
    p0, p1 = _PREV[:, 0], _PREV[:, 1]
    for t in range(steps):
        a, b = v[2 * t], v[2 * t + 1]
        c0 = pm[p0] + _O0[:, 0] * a + _O0[:, 1] * b
        c1 = pm[p1] + _O1[:, 0] * a + _O1[:, 1] * b
        d = c1 > c0
        dec[t] = d
        pm = np.where(d, c1, c0)
    st = 0
    bits = np.empty(steps, np.uint8)
    for t in range(steps - 1, -1, -1):
        bits[t] = _PIN[st]
        st = _PREV[st, int(dec[t, st])]
    return bits[: steps - (CONV_K - 1)]


def _coded_v2(data: bytes) -> np.ndarray:
    return _conv_encode(np.unpackbits(np.frombuffer(bytes(_RS.encode(data)), np.uint8)))


# the first 24 input bits (magic + version) are known, so are the first 48 coded bits
_SYNC = _conv_encode(np.unpackbits(np.frombuffer(MAGIC + bytes([2]), np.uint8)))[:48].astype(bool)


def _sync_errors(votes: np.ndarray) -> Tuple[int, int]:
    v = votes[:48]
    have = v != 0
    return int(np.count_nonzero(((v > 0) != _SYNC) & have)), int(np.count_nonzero(have))


def _sync_ok(votes: np.ndarray, frac: float = 0.3) -> bool:
    errs, n = _sync_errors(votes)
    return n >= 24 and errs <= frac * n


def _try_decode_v2(votes: np.ndarray) -> Optional[dict]:
    if not _sync_ok(votes):
        return None
    bits = _viterbi(votes)
    hard = bytearray(np.packbits(bits).tobytes())
    try:
        msg = _RS.decode(hard)[0]
    except (ReedSolomonError, ValueError, IndexError, ZeroDivisionError):
        return None
    fields = _unpack_data(bytes(msg), 2)
    if fields is not None:
        fields["raw"] = bytes(msg)
    return fields


_MAPS2: Dict[Tuple[int, int], Tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
# chip sums -> [block sum, sum weighted by each shape]
_PROJ = np.concatenate([np.ones((CELLS * CELLS, 1)), SHAPES.reshape(len(SHAPES), -1).T.astype(np.float64)], axis=1)


def _block_maps2(nrows: int, nb: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """v2: (coded-bit index, pn sign, shape index) per block, each nrows x nb."""
    key = (nrows, nb)
    got = _MAPS2.get(key)
    if got is None:
        k = np.arange(nrows * nb, dtype=np.int64).reshape(nrows, nb)
        z = _mix(k)
        pn = np.where(((z >> np.uint64(40)) & np.uint64(1)) == 1, 1, -1).astype(np.int8)
        shape = ((z >> np.uint64(44)) % np.uint64(len(SHAPES))).astype(np.int64)
        got = (_bit_index2(nrows * nb).reshape(nrows, nb), pn, shape)
        if len(_MAPS2) > 64:
            _MAPS2.clear()
        _MAPS2[key] = got
    return got


def _axis(n: int, start: float, b: float) -> Tuple[np.ndarray, np.ndarray]:
    t = (np.arange(n, dtype=np.float64) + 0.5 - start) / b
    idx = np.floor(t).astype(np.int64)
    return idx, (t - idx)


def _starts(idx: np.ndarray) -> np.ndarray:
    """Start positions of runs in a sorted index vector."""
    return np.concatenate(([0], np.flatnonzero(np.diff(idx)) + 1))


def _band_regions(rect: Tuple[int, int, int, int], h: int, w: int) -> Dict[str, Tuple[int, int, int, int]]:
    """Non-overlapping band regions (y0, y1, x0, x1) around the photo."""
    px, py, pw, ph = rect
    regs = {}
    if py > 0:
        regs["top"] = (0, py, 0, w)
    if py + ph < h:
        regs["bottom"] = (py + ph, h, 0, w)
    if px > 0:
        regs["left"] = (py, py + ph, 0, px)
    if px + pw < w:
        regs["right"] = (py, py + ph, px + pw, w)
    return regs


def _oriented_band_view(c3: np.ndarray, side: int, rect: Tuple[int, int, int, int]) -> np.ndarray:
    """Canvas view of the anchor band in anchor orientation (writes go through)."""
    px, py, pw, ph = rect
    if side == 0:
        return c3[py + ph:]
    if side == 1:
        return c3[:py][::-1]
    if side == 2:
        return c3[:, px + pw:].transpose(1, 0, 2)
    return c3[:, :px].transpose(1, 0, 2)[::-1]


# --------------------------------------------------------------------------
# Data block
# --------------------------------------------------------------------------
def _pack_v2(flags: int, rect_q: Tuple[int, ...], aspect_q: int, band8: Tuple[int, int, int],
             hash8: bytes, plen: int, salt: int, base: int) -> bytes:
    b = bytearray(MAGIC)
    b += bytes([2, flags])
    r = 0
    for v in rect_q:
        r = (r << 20) | int(v)
    b += r.to_bytes(10, "big")
    b += int(aspect_q).to_bytes(2, "big")
    b += bytes(band8)
    b += hash8
    b += int(plen).to_bytes(2, "big")
    b += bytes([salt & 0xFF])
    b += min(base, 0xFFFF).to_bytes(2, "big")
    assert len(b) == DATA_LEN
    return bytes(b)


def _pack_v1(flags: int, rect_q: Tuple[int, ...], band8: Tuple[int, int, int],
             hash8: bytes, plen: int, base: int) -> bytes:
    b = bytearray(MAGIC)
    b += bytes([1, flags])
    for v in rect_q:
        b += int(v).to_bytes(3, "big")
    b += bytes(band8)
    b += hash8
    b += int(plen).to_bytes(3, "big")
    b += min(base, 0xFFFF).to_bytes(2, "big")
    assert len(b) == DATA_LEN
    return bytes(b)


def _unpack_data(d: bytes, version: int) -> Optional[dict]:
    if d[:2] != MAGIC or d[2] != version:
        return None
    if version == 1:
        if d[3] & 0xC0:
            return None
        rect_q = tuple(int.from_bytes(d[4 + 3 * i: 7 + 3 * i], "big") for i in range(4))
        return {
            "version": 1, "flags": d[3], "side": d[3] & 3, "has_payload": bool(d[3] & 0x20),
            "rect_norm": tuple(q / (1 << V1_RECT_FRAC_BITS) for q in rect_q),
            "aspect": None, "band8": (d[16], d[17], d[18]), "hash8": bytes(d[19:27]),
            "plen": int.from_bytes(d[27:30], "big"), "salt": None,
            "base": int.from_bytes(d[30:32], "big"),
        }
    if d[3] & 0xE0:
        return None
    r = int.from_bytes(d[4:14], "big")
    rect_q = tuple((r >> (20 * (3 - i))) & 0xFFFFF for i in range(4))
    aq = int.from_bytes(d[14:16], "big")
    return {
        "version": 2, "flags": d[3], "side": d[3] & 3, "has_payload": bool(d[3] & 0x04),
        "depth": DEPTHS[(d[3] >> 3) & 3],
        "rect_norm": tuple(q / (1 << RECT_FRAC_BITS) for q in rect_q),
        "aspect": None if aq == 0xFFFF else aq / (1 << ASPECT_FRAC_BITS),
        "band8": (d[16], d[17], d[18]), "hash8": bytes(d[19:27]),
        "plen": int.from_bytes(d[27:29], "big"), "salt": d[29],
        "base": int.from_bytes(d[30:32], "big"),
    }


def _try_decode(votes: np.ndarray, version: int = 1) -> Optional[dict]:
    """v1: hard bits + RS with erasures."""
    hard = np.packbits((votes > 0).astype(np.uint8)).tobytes()
    # cheap prefilter on the (systematic) magic bytes
    if _magic_errors(votes) > 5:
        return None
    # byte reliability = weakest bit; bytes whose bits got (almost) no votes
    # (hidden by text or cropped away) are natural erasures
    conf = np.abs(votes).reshape(-1, 8).min(axis=1)
    order = [int(i) for i in np.argsort(conf, kind="stable")]
    missing = int(np.count_nonzero(conf <= 0.02 * max(float(np.median(np.abs(votes))), 1e-9)))
    for n_erase in sorted({0, min(missing, MAX_ERASURES), 6, MAX_ERASURES}):
        try:
            erase = order[:n_erase] if n_erase else None
            msg = _RS.decode(bytearray(hard), erase_pos=erase)[0]
        except (ReedSolomonError, ValueError, IndexError, ZeroDivisionError):
            continue
        fields = _unpack_data(bytes(msg), version)
        if fields is not None:
            fields["raw"] = bytes(msg)
            return fields
    return None


_MAGIC_BITS = np.unpackbits(np.frombuffer(MAGIC, np.uint8)).astype(bool)


def _magic_errors(votes: np.ndarray) -> int:
    return int(np.count_nonzero((votes[:16] > 0) != _MAGIC_BITS))


# --------------------------------------------------------------------------
# Writable ("free") background
# --------------------------------------------------------------------------
def _text_threshold(c3: np.ndarray, cc: int, scale: int, band8_c: Tuple[int, ...],
                    regions: Dict[str, Tuple[int, int, int, int]], cap: float = TEXT_MAX_THRESHOLD) -> float:
    """Pixel threshold (levels) for "text": just above the band's own noise.

    A flat band gives 1.5 levels (every anti-aliased ink pixel counts as
    text); paper texture raises it, up to ``cap``."""
    units = tuple(v * scale for v in band8_c)
    samples = []
    for (y0, y1, x0, x1) in regions.values():
        s = c3[y0:y1:3, x0:x1:3]
        if s.size:
            samples.append(_diff_units(s, cc, units).ravel()[::7])
    if not samples:
        return TEXT_MIN_THRESHOLD
    noise = float(np.median(np.concatenate(samples))) / scale
    return float(min(cap, max(TEXT_MIN_THRESHOLD, 4.0 * noise)))


def _region_free(c3: np.ndarray, cc: int, scale: int, band8_c: Tuple[int, ...],
                 reg: Tuple[int, int, int, int], rect: Tuple[int, int, int, int],
                 exclude: Optional[np.ndarray], thr: float) -> np.ndarray:
    """Writable mask (bool) of one band region: not text grown by TEXT_GROW,
    not within PHOTO_MARGIN of the photo, not excluded."""
    h, w = c3.shape[:2]
    y0, y1, x0, x1 = reg
    g = TEXT_GROW
    hy0, hy1, hx0, hx1 = max(0, y0 - g), min(h, y1 + g), max(0, x0 - g), min(w, x1 + g)
    sub = c3[hy0:hy1, hx0:hx1]
    lim = int(math.floor(thr * scale))       # text: |p - band| > lim on some channel
    vmax = 255 * scale
    nch = sub.shape[2]
    lo = [max(0, band8_c[ch] * scale - lim) if ch < cc else 0 for ch in range(nch)]
    hi = [min(vmax, band8_c[ch] * scale + lim) if ch < cc else vmax for ch in range(nch)]
    if nch == 1:
        inside = cv2.inRange(sub[:, :, 0], lo[0], hi[0])
    else:
        inside = cv2.inRange(sub, np.array(lo, np.float64), np.array(hi, np.float64))
    text = cv2.bitwise_not(inside)
    del inside
    k = 2 * g + 1
    grown = cv2.dilate(text, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    free = grown[y0 - hy0: y1 - hy0, x0 - hx0: x1 - hx0] == 0
    del text, grown
    px, py, pw, ph = rect
    m = PHOTO_MARGIN
    ry0, ry1 = max(y0, py - m), min(y1, py + ph + m)
    rx0, rx1 = max(x0, px - m), min(x1, px + pw + m)
    if ry1 > ry0 and rx1 > rx0:
        free[ry0 - y0: ry1 - y0, rx0 - x0: rx1 - x0] = False
    if exclude is not None:
        free &= ~exclude[y0:y1, x0:x1]
    return free


def _anchor_strip(free: Dict[str, np.ndarray], regions: Dict[str, Tuple[int, int, int, int]],
                  side: int, rect: Tuple[int, int, int, int]) -> np.ndarray:
    """Free mask of the anchor band in anchor orientation (thick x base)."""
    px, py, pw, ph = rect
    if side == 0:
        return free["bottom"]
    if side == 1:
        return free["top"][::-1]
    parts = []
    lo, hi = (px + pw, None) if side == 2 else (0, px)
    if "top" in free:
        parts.append(free["top"][:, lo:hi])
    parts.append(free["right" if side == 2 else "left"])
    if "bottom" in free:
        parts.append(free["bottom"][:, lo:hi])
    col = np.concatenate(parts, axis=0)          # h x thick
    return col.T if side == 2 else col.T[::-1]


# --------------------------------------------------------------------------
# Embedding (version 2)
# --------------------------------------------------------------------------
def embed(canvas: np.ndarray, photo_rect: tuple, band_color: tuple, photo_hash: str,
          payload: Optional[bytes] = None, exclude: Optional[np.ndarray] = None) -> EmbedResult:
    """Write the robust marker and (optionally) the fragile payload IN PLACE.

    ``band_color`` is in the canvas's own units/channels (e.g. (65535,)*3 for
    16-bit RGB, (255,) for 8-bit gray; an alpha value, if given, is ignored).
    ``photo_hash`` is a hex string; its first 8 bytes are stored.  ``exclude``
    is a bool H x W mask of pixels that must never be touched (hairline,
    keyline).  On skip the canvas is left unchanged.  The canvas must not
    carry an older marker (see :func:`scrub`).
    """
    c3 = _as3(canvas)
    h, w = c3.shape[:2]
    scale = _scale(c3.dtype)
    cc = _color_channels(c3)
    px, py, pw, ph = (int(v) for v in photo_rect)
    rect = (px, py, pw, ph)
    if pw <= 0 or ph <= 0 or px < 0 or py < 0 or px + pw > w or py + ph > h:
        return EmbedResult(False, False, "photo rect outside canvas")
    band8_c, band8 = _band8(band_color, cc, scale)
    if exclude is not None:
        if exclude.shape != (h, w):
            raise ValueError("exclude mask must be H x W")
        exclude = exclude.astype(bool, copy=False)

    geo = [g for g in _side_geometry(rect, h, w) if g[2] > 0]
    if not geo:
        return EmbedResult(False, False, "no band around the photo")
    side, y0, thick = max(geo, key=lambda g: g[2] * (w if g[0] < 2 else h))
    base, other = (w, h) if side < 2 else (h, w)
    q = [round(v / base * (1 << RECT_FRAC_BITS)) for v in rect]
    if max(q) >= (1 << 20):
        return EmbedResult(False, False, "photo too tall relative to the band for the marker")
    if any(round(qi * base / (1 << RECT_FRAC_BITS)) != v for qi, v in zip(q, rect)):
        return EmbedResult(False, False, "canvas too large for exact rect encoding")
    aq = min(0xFFFF, round(other / base * (1 << ASPECT_FRAC_BITS)))

    regions = _band_regions(rect, h, w)
    thr = _text_threshold(c3, cc, scale, band8_c, regions)
    free = {k: _region_free(c3, cc, scale, band8_c, r, rect, exclude, thr) for k, r in regions.items()}
    strip = _anchor_strip(free, regions, side, rect)
    usable = int(np.count_nonzero(strip))
    if usable < MIN_USABLE_AREA:
        return EmbedResult(False, False, f"usable background in the {SIDES[side]} band is {usable} px, "
                                         f"less than {MIN_USABLE_AREA} px (256x64)")
    nb, tiles = _choose_grid(strip, base)
    if nb == 0:
        return EmbedResult(False, False, f"the {SIDES[side]} band is too small (or too full of text) for one "
                                         "copy of the marker")

    hash8 = bytes.fromhex(short_hash(photo_hash).ljust(16, "0"))
    plan: Optional[_UnitPlan] = None
    pay_reason = ""
    salt = 0
    if payload is not None and len(payload) > 0:
        salt = zlib.crc32(hash8 + payload) & 0xFF
        plan, pay_reason = _plan_units(payload, free, regions, side, cc, hash8, salt)
    pay_ok = plan is not None
    plen = len(payload) if pay_ok and payload is not None else 0

    di = _depth_index(base / nb, tiles)
    flags = side | (0x04 if pay_ok else 0) | (di << 3)
    data = _pack_v2(flags, tuple(q), aq, band8, hash8, plen, salt if pay_ok else 0, base)
    code = _coded_v2(data)

    rng = np.random.default_rng(int.from_bytes(hash8, "big") ^ 0x50484F544F42)
    orig = None
    if plan is not None:  # original values, before the robust layer moves them
        orig = c3[plan.ys, plan.xs, :cc].astype(np.int32)
    _write_robust(c3, cc, scale, band8_c, side, rect, strip, nb, code, rng, DEPTHS[di], thr)
    if plan is not None and orig is not None:
        _write_units(c3, cc, scale, plan, orig, rng)

    reason = pay_reason
    if tiles < TILES_NOTE:
        reason = "; ".join(r for r in (reason, f"marker stored only {tiles:.1f}x (little free band space): it "
                                               "survives lossless copies and JPEG re-saves, but heavy "
                                               "recompression after resizing may lose it") if r)
    return EmbedResult(True, pay_ok, reason, nb, tiles)


def _choose_grid(strip: np.ndarray, base: int) -> Tuple[int, float]:
    """Pick NB (see module docstring); returns (nb, repetitions) or (0, 0)."""
    hv = strip.shape[0]
    fd = max(1, int(math.sqrt(strip.size / 4e6)))
    small = np.ascontiguousarray(strip[: hv // fd * fd, : base // fd * fd]).view(np.uint8)
    if fd > 1:
        small = cv2.resize(small * np.uint8(255), (base // fd, hv // fd), interpolation=cv2.INTER_AREA)
        small = small.astype(np.float32) * np.float32(1 / 255)
    else:
        small = small.astype(np.float32)
    hd, wd = small.shape
    integ = cv2.integral(small, sdepth=cv2.CV_64F)
    cands = []
    for nb in NB_CANDIDATES:
        b = base / nb
        if b < MIN_BLOCK_EMBED:
            continue
        nrows = int(math.floor(hv / b))
        if nrows < 1:
            continue
        yb = np.clip(np.round(np.arange(nrows + 1) * b / fd).astype(int), 0, hd)
        xb = np.clip(np.round(np.arange(nb + 1) * b / fd).astype(int), 0, wd)
        s = integ[yb][:, xb]
        blk = s[1:, 1:] - s[:-1, 1:] - s[1:, :-1] + s[:-1, :-1]
        area = np.outer(np.diff(yb), np.diff(xb)).astype(np.float64)
        frac = np.divide(blk, area, out=np.zeros_like(blk), where=area > 0)
        cands.append((nb, b, float(np.count_nonzero(frac >= BLOCK_MIN_FREE)) / NCODED))

    def largest(pool, need):
        ok = [c for c in pool if c[2] >= need]
        return max(ok, key=lambda c: c[1]) if ok else None

    got = (largest([c for c in cands if c[1] >= B_LARGE], TILES_TARGET)
           or largest([c for c in cands if c[1] >= B_TARGET], TILES_OK))
    if got is None:
        ok = [c for c in cands if c[1] >= B_TARGET and c[2] >= 1.0]
        got = max(ok, key=lambda c: c[2]) if ok else None
    if got is None:
        ok = [c for c in cands if c[2] >= TILES_MIN]
        got = max(ok, key=lambda c: c[2]) if ok else None
    return (got[0], got[2]) if got else (0, 0.0)


def _write_robust(c3: np.ndarray, cc: int, scale: int, band8_c: Tuple[int, ...], side: int,
                  rect: Tuple[int, int, int, int], strip: np.ndarray, nb: int, code: np.ndarray,
                  rng: np.random.Generator, depth: float, thr: float) -> None:
    """Write the grain pattern into the free pixels of the anchor band, in
    place, one chunk of pixel rows at a time (integer math, no full-size
    temporaries)."""
    view = _oriented_band_view(c3, side, rect)
    thick, base = strip.shape
    b = base / nb
    nrows = int(math.floor(thick / b))
    ridx, fy = _axis(thick, 0.0, b)
    nrow_px = int(np.searchsorted(ridx, nrows))
    bidx, pn, shp = _block_maps2(nrows, nb)
    sign = ((2 * code[bidx] - 1) * pn).astype(np.int8)
    cidx, fx = _axis(base, 0.0, b)
    cidx = np.minimum(cidx, nb - 1)
    jx = np.minimum((fx * CELLS).astype(np.int64), CELLS - 1)
    iy = np.minimum((fy[:nrow_px] * CELLS).astype(np.int64), CELLS - 1)
    # the pattern has only CELLS distinct pixel rows per block row
    prows = np.empty((nrows * CELLS, base), np.int8)
    for k in range(CELLS):
        prows[k::CELLS] = sign[:, cidx] * SHAPES[shp[:, cidx], k, jx[None, :]]
    prow_of = ridx[:nrow_px] * CELLS + iy
    # probability (in 1/256) that a pixel takes the "+" grain step, for P = +1 and P = -1
    t_pos = int(round(256 * (0.5 + depth)))
    t_neg = int(round(256 * (0.5 - depth)))
    crisp = t_pos >= 256 and t_neg <= 0
    if not crisp:
        prows = np.where(prows > 0, np.uint8(min(255, t_pos)), np.uint8(max(0, t_neg)))
    if max(band8_c) >= 254:
        mode = "down"            # "+" = stays, "-" = GRAIN levels darker
    elif min(band8_c) <= 1:
        mode = "up"              # "+" = GRAIN levels brighter
    else:
        mode = "two"             # "+" = one level up, "-" = one level down
    vmax = 255 * scale
    g = GRAIN * scale
    dt = c3.dtype.type
    # free pixels are within ``thr`` levels of the band colour: no clamping needed unless that
    # range reaches the ends of the value range
    lo8, hi8 = min(band8_c) - thr, max(band8_c) + thr
    safe = {"down": lo8 >= GRAIN, "up": hi8 <= 255 - GRAIN, "two": lo8 >= 1 and hi8 <= 254}[mode]
    step_rows = max(1, CHUNK // max(1, base))
    for r0 in range(0, nrow_px, step_rows):
        r1 = min(nrow_px, r0 + step_rows)
        fr = strip[r0:r1]
        if not fr.any():
            continue
        p = prows[prow_of[r0:r1]]
        if crisp:
            plus = p > 0
        else:
            plus = np.frombuffer(rng.bytes(p.size), np.uint8).reshape(p.shape) < p
        del p
        if mode == "two":
            up, dn = fr & plus, fr & ~plus
        else:
            up = dn = (fr & plus) if mode == "up" else (fr & ~plus)
        del plus
        if safe:
            # all colour channels in one call (the where mask broadcasts over them)
            tgt = view[r0:r1, :, :cc]
            if mode == "down":
                np.subtract(tgt, dt(g), out=tgt, where=dn[:, :, None])
            elif mode == "up":
                np.add(tgt, dt(g), out=tgt, where=up[:, :, None])
            else:
                np.add(tgt, dt(scale), out=tgt, where=up[:, :, None])
                np.subtract(tgt, dt(scale), out=tgt, where=dn[:, :, None])
            continue
        for ch in range(cc):
            plane = view[r0:r1, :, ch]
            if mode == "down":
                np.subtract(plane, dt(g), out=plane, where=dn & (plane >= g))
            elif mode == "up":
                np.add(plane, dt(g), out=plane, where=up & (plane <= vmax - g))
            else:
                np.add(plane, dt(scale), out=plane, where=up & (plane <= vmax - scale))
                np.subtract(plane, dt(scale), out=plane, where=dn & (plane >= scale))


@dataclass
class _UnitPlan:
    ys: np.ndarray      # (N, 16, 1) pixel rows of each used tile
    xs: np.ndarray      # (N, 1, 16) pixel columns
    bits: np.ndarray    # (N, 16, 16, cc) uint8


def _unit_bytes(payload: bytes, cc: int, hash8: bytes, salt: int) -> np.ndarray:
    u = 32 * cc
    d = u - UNIT_HEAD - UNIT_CRC
    k = max(1, -(-len(payload) // d))
    data = payload + bytes(k * d - len(payload))
    out = np.empty((k, u), np.uint8)
    for i in range(k):
        head = bytes([salt]) + i.to_bytes(2, "big") + data[i * d: (i + 1) * d]
        out[i] = np.frombuffer(head + zlib.crc32(hash8 + head).to_bytes(4, "big"), np.uint8)
    return out


def _plan_units(payload: bytes, free: Dict[str, np.ndarray], regions: Dict[str, Tuple[int, int, int, int]],
                side: int, cc: int, hash8: bytes, salt: int) -> Tuple[Optional[_UnitPlan], str]:
    if len(payload) > 0xFFFF:
        return None, f"payload {len(payload)} B too large (max 65535 B)"
    d = 32 * cc - UNIT_HEAD - UNIT_CRC
    k = -(-len(payload) // d)
    if k > 0xFFFF:
        return None, "payload too large"
    tiles: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}
    for name, (y0, y1, x0, x1) in regions.items():
        ty0, ty1 = -(-y0 // TILE), y1 // TILE
        tx0, tx1 = -(-x0 // TILE), x1 // TILE
        if ty1 <= ty0 or tx1 <= tx0:
            continue
        sub = free[name][ty0 * TILE - y0: ty1 * TILE - y0, tx0 * TILE - x0: tx1 * TILE - x0]
        ok = sub.reshape(ty1 - ty0, TILE, tx1 - tx0, TILE).all(axis=(1, 3))
        yy, xx = np.nonzero(ok)
        tiles[name] = (yy + ty0, xx + tx0)
    # the anchor band alone when it holds all copies (the other bands stay untouched)
    anchor = SIDES[side]
    use = [anchor] if anchor in tiles and tiles[anchor][0].size >= PAYLOAD_COPIES * k else list(tiles)
    ty = np.concatenate([tiles[u][0] for u in use]) if use else np.zeros(0, np.int64)
    tx = np.concatenate([tiles[u][1] for u in use]) if use else np.zeros(0, np.int64)
    n = int(ty.size)
    copies = min(PAYLOAD_COPIES, n // k) if k else 0
    if copies < 1:
        return None, f"payload {len(payload)} B does not fit ({n * d} B of free tiles)"
    along, across = (tx, ty) if side < 2 else (ty, tx)
    order = np.lexsort((across, along))
    ty, tx = ty[order], tx[order]
    g = n // copies
    pos = (np.arange(copies)[:, None] * g + (np.arange(k)[None, :] * g) // k).ravel()
    units = _unit_bytes(payload, cc, hash8, salt)
    bits = np.unpackbits(units, axis=1).reshape(k, TILE, TILE, cc)
    uid = np.tile(np.arange(k), copies)
    sel_y, sel_x = ty[pos], tx[pos]
    ys = sel_y[:, None, None] * TILE + np.arange(TILE)[None, :, None]
    xs = sel_x[:, None, None] * TILE + np.arange(TILE)[None, None, :]
    reason = f"payload stored {copies}x (space)" if copies < PAYLOAD_COPIES else ""
    return _UnitPlan(ys, xs, bits[uid]), reason


def _write_units(c3: np.ndarray, cc: int, scale: int, plan: _UnitPlan, orig: np.ndarray,
                 rng: np.random.Generator) -> None:
    cur = c3[plan.ys, plan.xs, :cc].astype(np.int32)
    b = plan.bits.astype(np.int32)
    if scale == 1:
        need = (cur & 1) != b
        lo = np.maximum(0, orig - MAX_DELTA)
        hi = np.minimum(255, orig + MAX_DELTA)
        up_ok = cur + 1 <= hi
        dn_ok = cur - 1 >= lo
        go_up = up_ok & ((rng.random(cur.shape) < 0.5) | ~dn_ok)
        new = np.where(need, np.where(go_up, cur + 1, cur - 1), cur)
    else:
        new = (cur & ~1) | b
        lim = MAX_DELTA * scale
        new = np.where(new - orig > lim, new - 2, new)
        new = np.where(orig - new > lim, new + 2, new)
    c3[plan.ys, plan.xs, :cc] = new.astype(c3.dtype)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------
def read(canvas: np.ndarray, approx_photo_rect: Optional[tuple] = None, sweep: bool = False) -> Optional[MarkerInfo]:
    """Look for the marker; ``approx_photo_rect`` comes from band detection
    (a few px of error is fine).  With ``sweep`` the photo edge of the largest
    band is also searched across the whole band (slower, < 1 s at 3000 px),
    for a detection that may be confidently wrong.  Returns None when not
    found."""
    if approx_photo_rect is None:
        return None
    c3 = _as3(canvas)
    h, w = c3.shape[:2]
    scale = _scale(c3.dtype)
    cc = _color_channels(c3)
    ax, ay, aw, ah = (int(round(v)) for v in approx_photo_rect)
    geo = [g for g in _side_geometry((ax, ay, aw, ah), h, w) if g[2] >= 4]
    geo.sort(key=lambda g: -g[2] * (w if g[0] < 2 else h))
    if geo:
        # the encoder anchors in the largest band; only bands of a similar size can be it
        top_area = geo[0][2] * (w if geo[0][0] < 2 else h)
        geo = [g for g in geo if g[2] * (w if g[0] < 2 else h) >= 0.5 * top_area]
    for side, y0a, _ in geo:
        found = _decode_v2(c3, cc, scale, side, y0a, sweep=False)
        if found is None:
            found = _decode_v1(c3, cc, scale, side, y0a)
        if found is None:
            continue
        fields, nb = found
        if fields["side"] != side:
            continue
        return _finish(c3, cc, scale, fields, side, nb)
    if sweep and geo:
        side, y0a, _ = geo[0]
        found = _decode_v2(c3, cc, scale, side, y0a, sweep=True)
        if found is not None and found[0]["side"] == side:
            return _finish(c3, cc, scale, found[0], side, found[1])
    return None


def _luma_region(c3: np.ndarray, cc: int, scale: int, side: int, start: int, f: int) -> np.ndarray:
    """Luma (levels, float32) of oriented rows [start, end) decimated by the
    integer factor f (block means anchored at ``start`` and at column 0), in
    anchor orientation."""
    h, w = c3.shape[:2]
    if side == 0:
        reg, flip, tr = c3[start:], False, False
    elif side == 1:
        n = h - start
        reg, flip, tr = c3[n % f: n], True, False
    elif side == 2:
        reg, flip, tr = c3[:, start:], False, True
    else:
        n = w - start
        reg, flip, tr = c3[:, n % f: n], True, True
    if tr:
        reg = reg.transpose(1, 0, 2)          # rows = oriented rows, columns = canvas rows
    rr, rc = reg.shape[0] // f, reg.shape[1] // f
    out = np.empty((rr, rc), np.float32)
    step = max(1, CHUNK // max(1, reg.shape[1] * f))
    shift = 2 if scale > 1 else 0          # 16 bit: 3 x (v >> 2) fits in uint16
    unit = np.float32((1 << shift) / (cc * scale))
    for i in range(0, rr, step):
        n_ = min(step, rr - i)
        blk = reg[i * f: (i + n_) * f, : rc * f]
        acc = (blk[:, :, 0] >> shift).astype(np.uint16) if shift else blk[:, :, 0].astype(np.uint16)
        for ch in range(1, cc):
            acc += (blk[:, :, ch] >> shift) if shift else blk[:, :, ch]
        if f > 1:
            acc = cv2.resize(acc, (rc, n_), interpolation=cv2.INTER_AREA)
        out[i:i + n_] = acc * unit
    if flip:
        out = out[::-1]
    return np.ascontiguousarray(out)


def _decode_v2(c3: np.ndarray, cc: int, scale: int, side: int, y0a: int, sweep: bool) -> Optional[Tuple[dict, int]]:
    h, w = c3.shape[:2]
    hv, base = (h, w) if side < 2 else (w, h)
    if sweep:
        start = max(0, y0a - max(DY_SEARCH, (hv - y0a) // 2) - 1)
    else:
        start = max(0, y0a - DY_SEARCH - 1)
    if hv - start < 8:
        return None
    # the encoder takes the largest blocks that give a few copies of the code, so for a band of
    # this shape much finer grids never occur (and are the most expensive to try)
    ratio = max(hv - y0a, 1) / base
    nb_max = math.sqrt(TILES_TARGET * NCODED / (0.15 * ratio))
    nbs = [nb for nb in NB_CANDIDATES
           if base / nb >= MIN_BLOCK_DECODE and (hv - y0a) / (base / nb) >= 1 and nb <= nb_max]
    if not nbs:
        return None
    bmin = min(base / nb for nb in nbs)
    f0 = max(1, int(bmin // WORK_BLOCK))
    l0 = _luma_region(c3, cc, scale, side, start, f0)
    rr, rc = l0.shape
    if rr < 2 or rc < 8:
        return None
    for nb in sorted(nbs):
        b = base / nb
        s_ = (b / f0) / WORK_BLOCK           # further decimation so a block is ~WORK_BLOCK px
        if s_ < 1.3:
            lw = l0
        else:
            lw = cv2.resize(l0, (max(8, round(rc / s_)), max(2, round(rr / s_))), interpolation=cv2.INTER_AREA)
        got = _decode_nb(lw, f0 * rc / lw.shape[1], f0 * rr / lw.shape[0], start, hv, base, nb, y0a, sweep)
        if got is not None:
            return got, nb
    return None


def _decode_nb(lw: np.ndarray, fx: float, fy: float, start: int, hv: int, base: int, nb: int, y0a: int,
               sweep: bool) -> Optional[dict]:
    """Decode one grid NB on a working luma whose pixel (i, j) covers oriented
    native rows start + [i fy, (i+1) fy) and columns [j fx, (j+1) fx)."""
    b = base / nb
    hw, ww = lw.shape
    if hw < 2:
        return None
    sub_s = lw[:: max(1, hw // 256), :: max(1, ww // 512)]
    med = float(np.median(sub_s))
    mad = float(np.median(np.abs(sub_s - med)))
    tau = max(TAU, 4.0 * 1.4826 * mad)
    dev = lw - np.float32(med)
    bad = (np.abs(dev) > tau).astype(np.uint8)
    g = int(round(RING_GROW / fx))
    if g > 0:
        # JPEG ringing around text and the photo edge reaches about one 8 px block
        bad = cv2.dilate(bad, np.ones((2 * g + 1, 2 * g + 1), np.uint8))
    m = (bad == 0).astype(np.float32)
    lc = dev * m
    # sub-cell (chip) column of every working column
    t = ((np.arange(ww) + 0.5) * fx) / b * CELLS
    sc = np.minimum(np.floor(t).astype(np.int64), nb * CELLS - 1)
    st = _starts(sc)
    present = sc[st]
    ncol = nb * CELLS

    def xsum(a: np.ndarray) -> np.ndarray:
        r = np.add.reduceat(a, st, axis=1)
        if present.size != ncol:
            full = np.zeros((a.shape[0], ncol), np.float32)
            full[:, present] = r
            return full
        return r

    zero = np.zeros((1, ncol), np.float64)
    cm = np.concatenate([zero, np.cumsum(xsum(m), axis=0, dtype=np.float64)])
    cl = np.concatenate([zero, np.cumsum(xsum(lc), axis=0, dtype=np.float64)])
    cq = np.concatenate([zero, np.cumsum(xsum(lc * lc), axis=0, dtype=np.float64)])
    nfull = (b / fx) * (b / fy)
    end_native = start + hw * fy

    def votes_at(y0: float, max_blocks: Optional[int] = None) -> Optional[np.ndarray]:
        nrows = int(math.floor((min(hv, end_native) - y0) / b))
        if max_blocks is not None:
            nrows = min(nrows, -(-max_blocks // nb))
        if nrows < 1 or nrows * nb < 0.5 * NCODED:
            return None
        yb = y0 + np.arange(nrows * CELLS + 1) * (b / CELLS)
        idx = np.clip(np.ceil((yb - start) / fy - 0.5).astype(np.int64), 0, hw)
        nblk = nrows * nb

        def chips(c: np.ndarray) -> np.ndarray:        # per block: its 16 chip sums
            g_ = np.diff(c[idx], axis=0)
            return g_.reshape(nrows, CELLS, nb, CELLS).transpose(0, 2, 1, 3).reshape(nblk, CELLS * CELLS)

        bidx, pn, shp = _block_maps2(nrows, nb)
        pick = (np.arange(nblk), shp.ravel() + 1)
        pm = chips(cm) @ _PROJ                          # [sum, sum x shape_0 .. shape_4]
        pl = chips(cl) @ _PROJ
        n, sQ = pm[:, 0].reshape(nrows, nb), pm[pick].reshape(nrows, nb)
        sL, sLQ = pl[:, 0].reshape(nrows, nb), pl[pick].reshape(nrows, nb)
        sq_ = np.diff(cq[idx], axis=0).reshape(nrows, CELLS, nb, CELLS).sum(axis=(1, 3))
        nz = np.maximum(n, 1e-9)
        den = n - sQ * sQ / nz
        cov = sLQ - sL * sQ / nz
        ok = (n >= 0.15 * nfull) & (den > 0.05 * nz)
        est = np.where(ok, cov / np.maximum(den, 1e-9), 0.0)
        var = np.maximum(sq_ / nz - (sL / nz) ** 2 - est * cov / nz, 0.0)
        est = np.clip(est, -CLIP_ESTIMATE, CLIP_ESTIMATE)
        wgt = np.minimum(1.0, n / nfull) / (1.0 + (var / VAR_SCALE) ** 2)
        return np.bincount(bidx.ravel(), weights=(est * wgt * pn).ravel(), minlength=NCODED)

    tried = set()
    fine = max(1.0, b / 16)
    dys = sorted(set(float(round(d / fine) * fine) for d in range(-DY_SEARCH, DY_SEARCH + 1)), key=abs)
    for dy in dys:
        y0 = y0a + dy
        tried.add(round(y0))
        v = votes_at(y0)
        if v is not None:
            fields = _try_decode_v2(v)
            if fields is not None:
                return fields
    if not sweep:
        return None
    # sweep: sync-bit votes for every candidate edge at once (vectorised), full
    # decodes only where the 48 known code bits agree
    coarse = max(1.0, b / 4)
    lo, hi = start + 1.0, min(hv, end_native) - 2 * b
    if hi <= lo:
        return None
    ys = np.arange(lo, hi, coarse)
    ys = ys[np.abs(ys - y0a) > DY_SEARCH]
    if ys.size == 0:
        return None
    nr1 = -(-NCODED // nb)
    bidx1, pn1, shp1 = _block_maps2(nr1, nb)
    sel = np.flatnonzero(bidx1.ravel() < 48)
    rs, cs_ = np.divmod(sel, nb)
    jbit = bidx1.ravel()[sel]
    q1 = SHAPES[shp1.ravel()[sel]].astype(np.float64)              # S x 4 x 4
    pns = pn1.ravel()[sel].astype(np.float64)
    onehot = np.zeros((sel.size, 48))
    onehot[np.arange(sel.size), jbit] = 1.0
    sub_rows = (rs[:, None] * CELLS + np.arange(CELLS + 1)[None, :]) * (b / CELLS)   # S x 5, from y0
    cols = rs[:, None] * 0 + cs_[:, None] * CELLS + np.arange(CELLS)[None, :]        # S x 4
    passed = []
    for i0 in range(0, ys.size, 256):
        yy = ys[i0:i0 + 256]
        ok_rows = (yy + nr1 * b) <= min(hv, end_native)
        idx = np.clip(np.ceil((yy[:, None, None] + sub_rows[None] - start) / fy - 0.5).astype(np.int64), 0, hw)
        gm = np.diff(cm[idx[..., None], cols[None, :, None, :]], axis=2)             # P x S x 4 x 4
        gl = np.diff(cl[idx[..., None], cols[None, :, None, :]], axis=2)
        n = gm.sum(axis=(2, 3))
        sQ = (gm * q1).sum(axis=(2, 3))
        sL = gl.sum(axis=(2, 3))
        sLQ = (gl * q1).sum(axis=(2, 3))
        nz = np.maximum(n, 1e-9)
        den = n - sQ * sQ / nz
        est = np.where((n >= 0.15 * nfull) & (den > 0.05 * nz), (sLQ - sL * sQ / nz) / np.maximum(den, 1e-9), 0.0)
        est = np.clip(est, -CLIP_ESTIMATE, CLIP_ESTIMATE) * np.minimum(1.0, n / nfull)
        v = (est * pns) @ onehot                                                     # P x 48
        have = v != 0
        errs = (((v > 0) != _SYNC) & have).sum(axis=1)
        good = ok_rows & (have.sum(axis=1) >= 24) & (errs <= 0.3 * have.sum(axis=1))
        passed.extend(yy[good].tolist())
    for y in passed[:64]:
        for d in sorted({0.0, -coarse / 2, coarse / 2, -fine, fine, -2 * fine, 2 * fine}, key=abs):
            vv = votes_at(y + d)
            if vv is not None:
                fields = _try_decode_v2(vv)
                if fields is not None:
                    return fields
    return None


def _finish(c3: np.ndarray, cc: int, scale: int, f: dict, side: int, nb: int) -> Optional[MarkerInfo]:
    h, w = c3.shape[:2]
    base, other = (w, h) if side < 2 else (h, w)
    norm = f["rect_norm"]
    x, y, rw, rh = (int(round(v * base)) for v in norm)
    rect = (x, y, rw, rh)
    if rw <= 0 or rh <= 0:
        return None
    fits = x >= 0 and y >= 0 and x + rw <= w and y + rh <= h
    same_base = min(base, 0xFFFF) == f["base"]
    geometry = "same" if same_base else "resized"
    rect_ok = fits
    if f["aspect"] is not None:
        exp = f["aspect"] * base
        tol = max(1.0, base / (1 << ASPECT_FRAC_BITS)) + (0.0 if same_base else 1.0 + other / base)
        if other > exp + tol:
            geometry, rect_ok = "changed", False      # side crop, padding or non-uniform stretch
        elif other < exp - tol:
            geometry = "cropped"                        # far-edge crop (or a non-uniform squash)
    if rect_ok and geometry != "same":
        # the canvas was resized or cropped since the save: the stored rect is
        # exact only if the photo edges agree with it
        nx, ny, nw, nh = (v * base for v in norm)
        if geometry == "resized" and f["aspect"]:
            # both canvas sides were rounded to whole pixels: the scale across the band differs a little
            rho = other / (f["aspect"] * base)
            if side < 2:
                ny, nh = ny * rho, nh * rho
            else:
                nx, nw = nx * rho, nw * rho
        ev = _edges_ok(c3, cc, scale, rect, (nx, ny, nx + nw, ny + nh), f["band8"], strict=same_base)
        if ev is False:
            rect_ok, geometry = False, "changed"
    payload = None
    if f["has_payload"] and f["plen"] > 0 and fits and same_base:
        if f["version"] == 2:
            if f["aspect"] is None or abs(other - f["aspect"] * base) <= max(1.0, base / (1 << ASPECT_FRAC_BITS)):
                payload = _read_units(c3, cc, f, rect)
        else:
            payload = _read_payload_v1(c3, cc, scale, f, rect)
    return MarkerInfo(
        version=f["version"],
        photo_rect=rect,
        photo_rect_norm=norm,
        band_color=f["band8"],
        short_hash=f["hash8"].hex(),
        payload=payload,
        payload_len=f["plen"],
        side=SIDES[side],
        nb=nb,
        rect_ok=bool(rect_ok),
        geometry=geometry,
        raw=f.get("raw", b""),
    )


STEP_MIN = 3.0          # levels; a photo edge weaker than this is "invisible"


def _edges_ok(c3: np.ndarray, cc: int, scale: int, rect: Tuple[int, int, int, int],
              exact: Tuple[float, float, float, float], band8: Tuple[int, int, int],
              strict: bool) -> Optional[bool]:
    """Check the photo rect against the image: at every photo edge next to a
    band, the strongest step of the row (column) profile of
    ``median |L - band|`` within +-max(6 px, 5 %) must be where the stored
    rect puts the edge: at that exact pixel boundary when the canvas has its
    saved size (``strict``), else within 0.6 px of the fractional position
    (``exact`` = x0, y0, x1, y1 before rounding), measured by the centroid of
    the ``mean |L - band|`` steps around it (a resampled edge is spread over
    two rows).  True = confirmed, False = contradicted, None = no visible
    edge (e.g. a high-key photo)."""
    h, w = c3.shape[:2]
    x, y, rw, rh = rect
    fx0, fy0, fx1, fy1 = exact
    band_l = float(np.mean(band8[:cc] if cc == 3 else band8[:1]))
    verdicts = []
    # (edge position, fractional position, axis, photo side sign, extent along the edge, band thickness)
    edges = [(y + rh, fy1, 0, -1, (x, x + rw), h - (y + rh)), (y, fy0, 0, +1, (x, x + rw), y),
             (x + rw, fx1, 1, -1, (y, y + rh), w - (x + rw)), (x, fx0, 1, +1, (y, y + rh), x)]
    for e, ef, axis, psign, (a0, a1), thick in edges:
        if thick < 2 or a1 - a0 < 8:
            continue
        dim = h if axis == 0 else w
        rad = max(6, int(round(0.05 * dim)))
        lo, hi = max(1, e - rad), min(dim - 1, e + rad)
        # the band side stops at the far edge, the photo side inside the photo
        if psign < 0:
            hi = min(hi, e + thick - 1)
            lo = max(lo, e - (rh if axis == 0 else rw) + 1)
        else:
            lo = max(lo, e - thick + 1)
            hi = min(hi, e + (rh if axis == 0 else rw) - 1)
        if hi - lo < 3 or not (lo <= e <= hi):
            continue
        idx = np.linspace(a0, a1 - 1, num=min(1024, a1 - a0)).astype(np.int64)
        if axis == 0:
            strip = c3[lo - 1: hi + 1][:, idx, :cc]
        else:
            strip = c3[:, lo - 1: hi + 1][idx, :, :cc].transpose(1, 0, 2)
        dev = np.abs(strip.astype(np.float32).mean(axis=2) / scale - band_l)   # rows: positions lo-1 .. hi
        med = np.median(dev, axis=1)
        mean = dev.mean(axis=1)
        # step at boundary p (between p-1 and p), signed so that photo -> band is positive
        sm = (med[:-1] - med[1:]) if psign < 0 else (med[1:] - med[:-1])
        sa = (mean[:-1] - mean[1:]) if psign < 0 else (mean[1:] - mean[:-1])
        i = int(np.argmax(sm))
        if float(sm[i]) < STEP_MIN:
            verdicts.append(None)
            continue
        p = lo + i
        if strict:
            verdicts.append(p == e)
            continue
        j0, j1 = max(0, i - 1), min(sa.size, i + 2)
        wts = np.maximum(sa[j0:j1], 0.0)
        c = float((np.arange(lo + j0, lo + j1) * wts).sum() / max(float(wts.sum()), 1e-9))
        verdicts.append(abs(c - ef) <= 0.6)
    if any(v is False for v in verdicts):
        return False
    if any(v is True for v in verdicts):
        return True
    return None


def _read_units(c3: np.ndarray, cc: int, f: dict, rect: Tuple[int, int, int, int]) -> Optional[bytes]:
    h, w = c3.shape[:2]
    plen, salt, hash8 = f["plen"], f["salt"], f["hash8"]
    u = 32 * cc
    d = u - UNIT_HEAD - UNIT_CRC
    k = -(-plen // d)
    th, tw = h // TILE, w // TILE
    if th == 0 or tw == 0 or k == 0:
        return None
    rows = [c3[r: th * TILE: TILE, : tw * TILE, :cc].reshape(th, tw, TILE, cc) for r in range(2)]
    hb = (np.concatenate(rows, axis=2).reshape(th, tw, 2 * TILE * cc)[:, :, : 8 * UNIT_HEAD] & 1).astype(np.uint8)
    head = np.packbits(hb, axis=2)
    idx = head[:, :, 1].astype(np.int64) * 256 + head[:, :, 2]
    cand = (head[:, :, 0] == salt) & (idx < k)
    px, py, pw, ph = rect
    cand[py // TILE: -(-(py + ph) // TILE), px // TILE: -(-(px + pw) // TILE)] = False
    got: Dict[int, bytes] = {}
    for ty, tx in zip(*np.nonzero(cand)):
        i = int(idx[ty, tx])
        if i in got:
            continue
        tile = c3[ty * TILE:(ty + 1) * TILE, tx * TILE:(tx + 1) * TILE, :cc]
        unit = np.packbits((tile & 1).astype(np.uint8).ravel()).tobytes()
        if zlib.crc32(hash8 + unit[:-UNIT_CRC]) != int.from_bytes(unit[-UNIT_CRC:], "big"):
            continue
        got[i] = unit[UNIT_HEAD:-UNIT_CRC]
        if len(got) == k:
            break
    if len(got) < k:
        return None
    return b"".join(got[i] for i in range(k))[:plen]


# --------------------------------------------------------------------------
# Scrub
# --------------------------------------------------------------------------
def scrub(canvas: np.ndarray, photo_rect: tuple, band_color: tuple,
          exclude: Optional[np.ndarray] = None) -> dict:
    """Remove any hidden marker (version 1 or 2, robust layer and payload)
    from the band IN PLACE, e.g. before an erase-in-place save re-embeds (or
    must not carry) a marker.  Photo pixels, pixels of ``exclude`` and text
    (pixels more than 3 levels off the band colour, grown by 2 px) are never
    touched.

    Flat bands (noise <= 1 level): every other pixel within 3 levels of the
    band colour is set to the band colour.  Textured bands (paper): in every
    pixel within the encoders' text threshold the expected pattern of a marker
    found in the band is subtracted and the least significant bit randomised.
    Returns ``{"found": bool, "flat": bool, "removed": bool}``; ``removed`` is
    False only when a marker can still be read afterwards."""
    c3 = _as3(canvas)
    h, w = c3.shape[:2]
    scale = _scale(c3.dtype)
    cc = _color_channels(c3)
    px, py, pw, ph = (int(v) for v in photo_rect)
    rect = (max(0, px), max(0, py), pw, ph)
    band8_c, _ = _band8(band_color, cc, scale)
    bc = [int(v) for v in band_color]
    if len(bc) < cc:
        bc = [bc[0]] * cc
    units = tuple(bc[:cc])
    if exclude is not None:
        exclude = exclude.astype(bool, copy=False)
    old = read(canvas, rect)
    regions = _band_regions(rect, h, w)
    ref8 = tuple(old.band_color[:cc]) if (old is not None and cc == 3) else (
        (old.band_color[0],) if old is not None else band8_c)
    noise = _text_threshold(c3, cc, scale, ref8, regions, cap=1e9) / 4.0
    flat = noise <= 1.0
    rng = np.random.default_rng(0x5343)
    if flat:
        lim = SCRUB_RANGE * scale
        ref = units
    else:
        # the encoders wrote wherever a pixel was within their text threshold (v1: up to 48 levels)
        lim = int(min(48.0, max(TEXT_MIN_THRESHOLD, 4.0 * noise)) * scale)
        ref = tuple(v * scale for v in ref8)
    delta = _old_delta(old, c3, cc, scale, rect) if (old is not None and not flat) else None
    vmax = 255 * scale
    for (y0, y1, x0, x1) in regions.values():
        sub = c3[y0:y1, x0:x1]
        text = np.zeros(sub.shape[:2], bool)
        step = max(1, CHUNK // max(1, sub.shape[1]))
        for r in range(0, sub.shape[0], step):
            t = text[r:r + step]
            for ch in range(cc):
                plane = sub[r:r + step, :, ch]
                if ref[ch] - lim > 0:
                    t |= plane < ref[ch] - lim
                if ref[ch] + lim < vmax:
                    t |= plane > ref[ch] + lim
        keep = cv2.dilate(text.view(np.uint8), np.ones((5, 5), np.uint8)) == 0
        del text
        if exclude is not None:
            keep &= ~exclude[y0:y1, x0:x1]
        if flat:
            for ch in range(cc):
                np.copyto(sub[:, :, ch], c3.dtype.type(units[ch]), where=keep)
            continue
        d = None
        if delta is not None:
            dy0, dy1, dx0, dx1, darr = delta
            full = np.zeros(sub.shape[:2], np.int16)
            iy0, iy1, ix0, ix1 = max(y0, dy0), min(y1, dy1), max(x0, dx0), min(x1, dx1)
            if iy1 > iy0 and ix1 > ix0:
                full[iy0 - y0: iy1 - y0, ix0 - x0: ix1 - x0] = darr[iy0 - dy0: iy1 - dy0, ix0 - dx0: ix1 - dx0]
            d = full[keep].astype(np.int32)
            del full
        for ch in range(cc):
            plane = sub[:, :, ch]
            v = plane[keep].astype(np.int32)
            if d is not None:
                v -= d
            v ^= rng.integers(0, 2, v.size, dtype=np.int32)
            np.clip(v, 0, vmax, out=v)
            plane[keep] = v
    left = read(canvas, rect)
    return {"found": old is not None, "flat": bool(flat), "removed": left is None}


def _old_delta(old: MarkerInfo, c3: np.ndarray, cc: int, scale: int,
               rect: Tuple[int, int, int, int]) -> Optional[Tuple[int, int, int, int, np.ndarray]]:
    """Expected change the robust layer of a found marker made (canvas units,
    int16), regenerated from its decoded data (textured-band scrub):
    ``(y0, y1, x0, x1, array)`` in canvas coordinates."""
    h, w = c3.shape[:2]
    side = SIDES.index(old.side)
    base = w if side < 2 else h
    geo = {g[0]: g for g in _side_geometry(tuple(old.photo_rect), h, w)}
    _, y0, thick = geo[side]
    if thick <= 0 or len(old.raw) != DATA_LEN:
        return None
    b = base / old.nb
    nrows = int(math.floor(thick / b))
    if nrows < 1:
        return None
    code = (_coded_v2(old.raw) if old.version == 2
            else np.unpackbits(np.frombuffer(bytes(_RS.encode(old.raw)), np.uint8)).astype(np.int8))
    ridx, fy = _axis(thick, 0.0, b)
    n = int(np.searchsorted(ridx, nrows))
    cidx, fx = _axis(base, 0.0, b)
    cidx = np.minimum(cidx, old.nb - 1)
    band8_c = old.band_color[:cc] if cc == 3 else old.band_color[:1]
    e = np.empty((n, base), np.int16)
    step = max(1, CHUNK // max(1, base))
    if old.version == 2:
        bidx, pn, shp = _block_maps2(nrows, old.nb)
        sign = (2 * code[bidx] - 1) * pn
        jx = np.minimum((fx * CELLS).astype(np.int64), CELLS - 1)
        m = DEPTHS[(old.raw[3] >> 3) & 3]
    else:
        bidx, pn, orient = _block_maps_v1(nrows, old.nb)
        sign = (2.0 * code[bidx] - 1.0) * pn
        (ua, va), (ub, vb) = V1_PAIR
    for r0 in range(0, n, step):
        r1 = min(n, r0 + step)
        ri, fyk = ridx[r0:r1], fy[r0:r1]
        if old.version == 2:
            iy = np.minimum((fyk * CELLS).astype(np.int64), CELLS - 1)
            p = sign[ri][:, cidx] * SHAPES[shp[ri][:, cidx], iy[:, None], jx[None, :]]
            if max(band8_c) >= 254:
                ev = -GRAIN * (0.5 - m * p)
            elif min(band8_c) <= 1:
                ev = GRAIN * (0.5 + m * p)
            else:
                ev = 2 * m * p
        else:
            pat = (np.cos(np.pi * ua * fyk)[:, None] * np.cos(np.pi * va * fx)[None, :]
                   - orient[ri][:, cidx] * (np.cos(np.pi * ub * fyk)[:, None] * np.cos(np.pi * vb * fx)[None, :]))
            ev = _shape_delta_v1(pat * sign[ri][:, cidx] * 0.5, tuple(band8_c))
        e[r0:r1] = np.round(ev * scale)
    if side == 0:
        return y0, y0 + n, 0, w, e
    if side == 1:
        return h - y0 - n, h - y0, 0, w, e[::-1]
    if side == 2:
        return 0, h, y0, y0 + n, e.T
    return 0, h, w - y0 - n, w - y0, e[::-1].T


# --------------------------------------------------------------------------
# Version 1: reader (and the legacy writer used by the compatibility tests)
# --------------------------------------------------------------------------
def _block_maps_v1(nrows: int, nb: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    k = np.arange(nrows * nb, dtype=np.int64).reshape(nrows, nb)
    pn, orient = _pn(k)
    return _bit_index(nrows * nb).reshape(nrows, nb), pn, orient


def _decode_v1(c3: np.ndarray, cc: int, scale: int, side: int, y0a: int) -> Optional[Tuple[dict, int]]:
    h, w = c3.shape[:2]
    hv, base = (h, w) if side < 2 else (w, h)
    # the first format took the coarsest grid with 4 repetitions of 384 bits: finer grids
    # than that never occur for a band of this shape
    ratio = max(hv - y0a, 1) / base
    nb_max = math.sqrt(V1_MIN_TILES * NBITS / (0.25 * ratio))
    nbs = [nb for nb in V1_NB_CANDIDATES if nb <= nb_max and base / nb >= V1_MIN_BLOCK_DECODE]
    if not nbs:
        return None
    f0 = max(1, int(min(base / nb for nb in nbs) // WORK_BLOCK))
    start = max(0, y0a - (DY_SEARCH + 2) * f0)
    l0 = _luma_region(c3, cc, scale, side, start, f0)
    rr, rc = l0.shape
    if rr < 3 or rc < 8:
        return None
    for nb in nbs:
        s_ = (base / nb / f0) / WORK_BLOCK
        lw = l0 if s_ < 1.3 else cv2.resize(l0, (max(8, round(rc / s_)), max(3, round(rr / s_))),
                                             interpolation=cv2.INTER_AREA)
        fy = f0 * rr / lw.shape[0]
        y0 = int(round((y0a - start) / fy))
        for nb_, _dy, votes in _iter_votes_v1(lw, y0, only=nb):
            fields = _try_decode(votes)
            if fields is not None:
                return fields, nb_
    return None


def _iter_votes_v1(lv: np.ndarray, y0a: int, only: Optional[int] = None) -> Iterator[Tuple[int, int, np.ndarray]]:
    """Yield ``(nb, dy, votes)`` soft votes per coded bit for each candidate
    grid and photo-edge offset.  ``lv`` is the oriented luma (levels)."""
    hv, base = lv.shape
    ya = max(0, y0a - DY_SEARCH)
    if hv - ya < 3:
        return
    region = lv[ya:]
    med = float(np.median(region[:, ::7]))
    lr = region - np.float32(med)
    lq = lr * lr
    dys = sorted(range(-DY_SEARCH, DY_SEARCH + 1), key=abs)
    (ua, va), (ub, vb) = V1_PAIR
    for nb in V1_NB_CANDIDATES if only is None else (only,):
        b = base / nb
        if b < V1_MIN_BLOCK_DECODE or (hv - y0a) / b < 2:
            continue
        cidx, fx = _axis(base, 0.0, b)
        cidx = np.minimum(cidx, nb - 1)
        cs = _starts(cidx)
        cxa = np.cos(np.pi * va * fx).astype(np.float32)
        cxb = np.cos(np.pi * vb * fx).astype(np.float32)
        h0 = np.add.reduceat(lr, cs, axis=1)
        ha = np.add.reduceat(lr * cxa, cs, axis=1)
        hb = np.add.reduceat(lr * cxb, cs, axis=1)
        hq = np.add.reduceat(lq, cs, axis=1)
        ka = np.add.reduceat(cxa, cs)
        kb = np.add.reduceat(cxb, cs)
        kaa = np.add.reduceat(cxa * cxa, cs)
        kbb = np.add.reduceat(cxb * cxb, cs)
        kab = np.add.reduceat(cxa * cxb, cs)
        ncol = np.bincount(cidx, minlength=nb).astype(np.float64)
        for dy in dys:
            y0 = y0a + dy
            nrows = int(math.floor((hv - y0) / b))
            if nrows < 1 or nrows * nb < NBITS:
                continue
            ridx, fy = _axis(hv - ya, y0 - ya, b)
            sel = np.flatnonzero((ridx >= 0) & (ridx < nrows))
            if sel.size == 0:
                continue
            r = ridx[sel]
            rs = _starts(r)
            cya = np.cos(np.pi * ua * fy[sel])
            cyb = np.cos(np.pi * ub * fy[sel])
            ra, rb = sel[0], sel[-1] + 1

            def rsum(a: np.ndarray) -> np.ndarray:
                return np.add.reduceat(a, rs, axis=0)

            s0 = rsum(h0[ra:rb])
            sq = rsum(hq[ra:rb])
            bidx, pn, orient = _block_maps_v1(rs.size, nb)
            slk = rsum(cya[:, None] * ha[ra:rb]) - orient * rsum(cyb[:, None] * hb[ra:rb])
            sk = rsum(cya)[:, None] * ka[None, :] - orient * (rsum(cyb)[:, None] * kb[None, :])
            skk = (rsum(cya * cya)[:, None] * kaa[None, :] + rsum(cyb * cyb)[:, None] * kbb[None, :]
                   - 2 * orient * (rsum(cya * cyb)[:, None] * kab[None, :]))
            n = np.bincount(r)[r[rs]][:, None] * ncol[None, :]
            mean = s0 / n
            var = np.maximum(sq / n - mean * mean, 0.0)
            est = 2.0 * (slk - mean * sk) / np.maximum(skk, 1e-6)
            est = np.clip(est, -CLIP_ESTIMATE, CLIP_ESTIMATE)
            wgt = 1.0 / (1.0 + (var / VAR_SCALE) ** 2)
            votes = np.bincount(bidx.ravel(), weights=(est * wgt * pn).ravel(), minlength=NBITS)
            yield nb, dy, votes


def _coprime_step(n: int) -> int:
    q = max(1, int(n * GOLDEN))
    while math.gcd(q, n) != 1:
        q += 1
    return q


def _photo_tiles(rect: Tuple[int, int, int, int], ht: int, wt: int) -> np.ndarray:
    px, py, pw, ph = rect
    m = np.zeros((ht, wt), bool)
    x0 = max(0, (px - PHOTO_MARGIN) // TILE)
    y0 = max(0, (py - PHOTO_MARGIN) // TILE)
    x1 = min(wt, -(-(px + pw + PHOTO_MARGIN) // TILE))
    y1 = min(ht, -(-(py + ph + PHOTO_MARGIN) // TILE))
    m[y0:y1, x0:x1] = True
    return m


def _tile_max(diff8: np.ndarray) -> np.ndarray:
    h, w = diff8.shape
    ht, wt = h // TILE, w // TILE
    return diff8[: ht * TILE, : wt * TILE].reshape(ht, TILE, wt, TILE).max(axis=(1, 3))


def _tile_free_v1(diff8: np.ndarray, t: float, rect: Tuple[int, int, int, int]) -> np.ndarray:
    h, w = diff8.shape
    ht, wt = h // TILE, w // TILE
    if ht == 0 or wt == 0:
        return np.zeros((ht, wt), bool)
    busy = (_tile_max(diff8) > t).astype(np.uint8)
    busy = cv2.dilate(busy, np.ones((3, 3), np.uint8)).astype(bool)
    busy |= _photo_tiles(rect, ht, wt)
    return ~busy


def _slots_v1(free_tiles: np.ndarray, h: int, w: int) -> np.ndarray:
    ht, wt = free_tiles.shape
    m = np.zeros((h, w), bool)
    m[: ht * TILE, : wt * TILE] = np.repeat(np.repeat(free_tiles, TILE, 0), TILE, 1)
    return np.flatnonzero(m)


def _read_payload_v1(c3: np.ndarray, cc: int, scale: int, f: dict,
                     rect: Tuple[int, int, int, int]) -> Optional[bytes]:
    h, w = c3.shape[:2]
    t_val = V1_T_CANDIDATES[(f["flags"] >> 2) & 7]
    diff8 = _diff8(c3, cc, scale, f["band8"][:cc] if cc == 3 else f["band8"][:1])
    px_idx = _slots_v1(_tile_free_v1(diff8, t_val, rect), h, w)
    n = int(px_idx.size) * cc
    plen = f["plen"]
    copy_bits = (plen + 8) * 8
    copies = min(PAYLOAD_COPIES, n // copy_bits) if n else 0
    q = _coprime_step(n) if n else 1
    for c in range(copies):
        kk = np.arange(c * copy_bits, (c + 1) * copy_bits, dtype=np.int64)
        s = (kk * q) % n
        sy, sx = np.divmod(px_idx[s // cc], w)
        raw = np.packbits((c3[sy, sx, s % cc] & 1).astype(np.uint8)).tobytes()
        if int.from_bytes(raw[:4], "big") != plen:
            continue
        if zlib.crc32(raw[: 4 + plen]) != int.from_bytes(raw[4 + plen:], "big"):
            continue
        return raw[4: 4 + plen]
    return None


def _shape_delta_v1(pat: np.ndarray, band8: Tuple[int, ...]) -> np.ndarray:
    if max(band8) >= 254:
        return -V1_AMP_ONE_SIDED * np.clip(-V1_WHITE_GAIN * pat, 0.0, 1.0)
    if min(band8) <= 1:
        return V1_AMP_ONE_SIDED * np.clip(V1_WHITE_GAIN * pat, 0.0, 1.0)
    return V1_AMP_TWO_SIDED * pat


def _embed_v1(canvas: np.ndarray, photo_rect: tuple, band_color: tuple, photo_hash: str,
              payload: Optional[bytes] = None, exclude: Optional[np.ndarray] = None) -> EmbedResult:
    """The format-1 writer of the first release, kept only so the tests can
    prove files written by it still read.  Never used for saving."""
    c3 = _as3(canvas)
    h, w = c3.shape[:2]
    scale = _scale(c3.dtype)
    cc = _color_channels(c3)
    rect = tuple(int(v) for v in photo_rect)
    px, py, pw, ph = rect
    band8_c, band8 = _band8(band_color, cc, scale)
    diff8 = _diff8(c3, cc, scale, band8_c)
    tmax = _tile_max(diff8)
    relevant = tmax[~_photo_tiles(rect, *tmax.shape)]
    t_index = None
    for i, t in enumerate(V1_T_CANDIDATES):
        if relevant.size and np.any(np.abs(relevant - t) <= V1_T_MARGIN):
            continue
        if exclude is not None:
            ft = _tile_free_v1(diff8, t, rect)
            ht, wt = ft.shape
            if np.any(exclude[: ht * TILE, : wt * TILE].reshape(ht, TILE, wt, TILE).any(axis=(1, 3)) & ft):
                continue
        t_index = i
        break
    t_val = V1_T_CANDIDATES[t_index if t_index is not None else V1_DEFAULT_T_INDEX]
    sample = diff8[::3, ::3].copy()
    sample[py // 3: -(-(py + ph) // 3), px // 3: -(-(px + pw) // 3)] = np.nan
    noise = float(np.nanmedian(sample))
    thr = float(min(t_val, max(TEXT_MIN_THRESHOLD, 4.0 * noise)))
    text = (diff8 > thr).astype(np.uint8)
    k = 2 * TEXT_GROW + 1
    free = ~cv2.dilate(text, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))).astype(bool)
    free[max(0, py - PHOTO_MARGIN): py + ph + PHOTO_MARGIN, max(0, px - PHOTO_MARGIN): px + pw + PHOTO_MARGIN] = False
    if exclude is not None:
        free &= ~exclude.astype(bool)
    geo = [g for g in _side_geometry(rect, h, w) if g[2] > 0]
    side, y0, thick = max(geo, key=lambda g: g[2] * (w if g[0] < 2 else h))
    free_v = _orient(free, side)
    base = free_v.shape[1]
    q = [round(v / base * (1 << V1_RECT_FRAC_BITS)) for v in rect]
    band = free_v[y0:].astype(np.float32)
    nb, tiles = 0, 0.0
    for cand in V1_NB_CANDIDATES:
        b = base / cand
        if b < V1_MIN_BLOCK_EMBED or int(band.shape[0] / b) < 1:
            continue
        nrows = int(math.floor(band.shape[0] / b))
        ridx, _ = _axis(band.shape[0], 0.0, b)
        rows = band[: int(np.searchsorted(ridx, nrows))]
        cidx, _ = _axis(base, 0.0, b)
        cidx = np.minimum(cidx, cand - 1)
        colsum = np.add.reduceat(rows, _starts(cidx), axis=1)
        r = ridx[: rows.shape[0]]
        blk = np.add.reduceat(colsum, _starts(r), axis=0)
        cnt = np.outer(np.bincount(r), np.bincount(cidx))
        t_ = float(np.count_nonzero(blk >= V1_BLOCK_FREE_FRACTION * cnt)) / NBITS
        if t_ >= V1_MIN_TILES:
            nb, tiles = cand, t_
            break
        if t_ >= 1.0 and t_ > tiles:
            nb, tiles = cand, t_
    if nb == 0:
        return EmbedResult(False, False, "anchor band too small")
    plan = None
    if payload is not None and t_index is not None:
        free_tiles = _tile_free_v1(diff8, t_val, rect)
        px_idx = _slots_v1(free_tiles, h, w)
        n = int(px_idx.size) * cc
        head = len(payload).to_bytes(4, "big")
        one = np.unpackbits(np.frombuffer(head + payload + zlib.crc32(head + payload).to_bytes(4, "big"), np.uint8))
        copies = min(PAYLOAD_COPIES, n // one.size) if n else 0
        if copies >= 1:
            bits = np.tile(one, copies)
            s = (np.arange(bits.size, dtype=np.int64) * _coprime_step(n)) % n
            ys, xs = np.divmod(px_idx[s // cc], w)
            plan = (ys, xs, s % cc, bits)
    flags = side | ((t_index if t_index is not None else V1_DEFAULT_T_INDEX) << 2) | (0x20 if plan else 0)
    hash8 = bytes.fromhex(short_hash(photo_hash).ljust(16, "0"))
    data = _pack_v1(flags, tuple(q), band8, hash8, len(payload) if plan else 0, base)
    code = np.unpackbits(np.frombuffer(bytes(_RS.encode(data)), np.uint8)).astype(np.float32)
    b = base / nb
    nrows = int(math.floor(thick / b))
    bidx, pn, orient = _block_maps_v1(nrows, nb)
    sign = (2.0 * code[bidx] - 1.0) * pn
    ridx, fy = _axis(free_v.shape[0] - y0, 0.0, b)
    keep = ridx < nrows
    ridx, fy = ridx[keep], fy[keep]
    cidx, fx = _axis(base, 0.0, b)
    cidx = np.minimum(cidx, nb - 1)
    (ua, va), (ub, vb) = V1_PAIR
    pat = (np.cos(np.pi * ua * fy)[:, None] * np.cos(np.pi * va * fx)[None, :]
           - orient[ridx][:, cidx] * (np.cos(np.pi * ub * fy)[:, None] * np.cos(np.pi * vb * fx)[None, :])).astype(np.float32)
    pat *= sign[ridx][:, cidx] * 0.5
    d = _shape_delta_v1(pat, band8_c) * free_v[y0: y0 + ridx.size]
    delta = np.zeros((h, w), np.float32)
    _orient(delta, side)[y0: y0 + ridx.size] = d
    rng = np.random.default_rng(int.from_bytes(hash8, "big") ^ 0x50484F544F42)
    orig = c3[plan[0], plan[1], plan[2]].astype(np.int64) if plan else None
    ys_, xs_ = np.nonzero(delta)
    stepv = np.floor(delta[ys_, xs_] + rng.random(ys_.size, dtype=np.float32)).astype(np.int64)
    vmax, lim = 255 * scale, MAX_DELTA * scale
    for ch in range(cc):
        v = c3[ys_, xs_, ch].astype(np.int64)
        c3[ys_, xs_, ch] = np.clip(v + stepv * scale, np.maximum(0, v - lim), np.minimum(vmax, v + lim)).astype(c3.dtype)
    if plan is not None and orig is not None:
        sy, sx, ch, bits = plan
        bb = bits.astype(np.int64)
        lo, hi = np.maximum(0, orig - lim), np.minimum(vmax, orig + lim)
        if scale == 1:
            t = orig + delta[sy, sx].astype(np.float64)
            low = 2 * np.floor((t - bb) / 2).astype(np.int64) + bb
            new = low + 2 * (rng.random(t.size) < (t - low) / 2)
        else:
            new = (c3[sy, sx, ch].astype(np.int64) & ~1) | bb
        new = np.where(new > hi, new - 2, new)
        new = np.where(new < lo, new + 2, new)
        c3[sy, sx, ch] = new.astype(c3.dtype)
    return EmbedResult(True, plan is not None, "", nb, tiles)
