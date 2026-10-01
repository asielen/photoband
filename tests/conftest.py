import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile

import pytest
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")


@pytest.fixture(scope="session", autouse=True)
def _home(tmp_path_factory):
    home = tmp_path_factory.mktemp("pbhome")
    os.environ["PHOTOBAND_HOME"] = str(home)
    os.environ["PHOTOBAND_NO_SYSTEM_FONTS"] = "1"
    os.environ["PHOTOBAND_NO_NATIVE_DIALOGS"] = "1"
    yield home


# ---------------------------------------------------------------------------- platform capabilities
# Tests that need something a machine may lack skip with the reason instead of failing.

def _can_symlink() -> bool:
    """Whether this account can create symbolic links (Windows needs Developer Mode or the
    SeCreateSymbolicLinkPrivilege; without it os.symlink fails with WinError 1314)."""
    d = tempfile.mkdtemp(prefix="pb-symlink-probe-")
    try:
        target = os.path.join(d, "t")
        open(target, "wb").close()
        os.symlink(target, os.path.join(d, "l"))
        return True
    except (OSError, NotImplementedError, AttributeError):
        return False
    finally:
        shutil.rmtree(d, ignore_errors=True)


CAN_SYMLINK = _can_symlink()
NO_SYMLINK_REASON = ("this account can't create symbolic links (on Windows: enable Developer Mode or "
                     "grant SeCreateSymbolicLinkPrivilege)")


@pytest.fixture
def symlink():
    """``os.symlink``, or skip the test where symbolic links can't be created. Request it in
    tests that make links; the test runs up to its first link, so earlier checks still count."""
    def make(src, dst, target_is_directory=False):
        if not CAN_SYMLINK:
            pytest.skip(NO_SYMLINK_REASON)
        os.symlink(src, dst, target_is_directory=target_is_directory)
    return make


def best_time(fn, repeats: int = 3):
    """(result, seconds) of the fastest of ``repeats`` calls. Speed checks use it so a busy machine
    (parallel tests, a cold first call) doesn't fail them; a real slowdown still does."""
    import time
    best, out = None, None
    for _ in range(repeats):
        t0 = time.perf_counter()
        out = fn()
        dt = time.perf_counter() - t0
        best = dt if best is None else min(best, dt)
    return out, best


# POSIX permission bits (chmod 0o640 ...): Windows only keeps a read-only flag.
posix_permissions = pytest.mark.skipif(os.name == "nt", reason="needs POSIX file permission bits (not on Windows)")


MAKE_FIXTURES = os.path.join(ROOT, "scripts", "make_fixtures.py")
FIXTURE_STAMP = os.path.join(FIXTURES, ".make_fixtures.sha256")


def _fixtures_hash() -> str:
    with open(MAKE_FIXTURES, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


@pytest.fixture(scope="session")
def fixtures_dir():
    """Fixtures are rebuilt whenever scripts/make_fixtures.py changes (its hash is kept
    in a stamp file) or the last fixture is missing."""
    want = _fixtures_hash()
    try:
        with open(FIXTURE_STAMP, "r", encoding="ascii") as fh:
            have = fh.read().strip()
    except OSError:
        have = ""
    if have != want or not os.path.exists(os.path.join(FIXTURES, "14_near_white_sky.tif")):
        subprocess.run([sys.executable, MAKE_FIXTURES, FIXTURES], check=True)
        os.makedirs(FIXTURES, exist_ok=True)
        with open(FIXTURE_STAMP, "w", encoding="ascii") as fh:
            fh.write(want + "\n")
    return FIXTURES


@pytest.fixture
def work(tmp_path, fixtures_dir):
    """Copy fixtures into a temp folder so saves never touch the originals."""
    def get(name):
        dst = tmp_path / name
        shutil.copy2(os.path.join(fixtures_dir, name), dst)
        return str(dst)
    return get


REPO_FONT = os.path.join(ROOT, "fonts", "noto-serif", "NotoSerif[wdth,wght].ttf")


def _font(size):
    """The repo's Noto Serif, so caption tiles are the same on every OS."""
    return ImageFont.truetype(REPO_FONT, size)


def make_band_layout(pw, ph, text="Ann, Bea and Carl", border=(0.057, 0.076, 0.28), band="#ffffff",
                     color="#1f2a44", source_rect=None):
    """A layout object like the UI produces (Polaroid-ish), with one text tile."""
    side = round(pw * border[0])
    top = round(pw * border[1])
    bottom = round(pw * border[2])
    W, H = pw + 2 * side, ph + top + bottom
    area = [side, top + ph + round(bottom * 0.2), pw, round(bottom * 0.6)]
    fs = max(10, round(bottom * 0.18))
    tile = Image.new("RGBA", (area[2], area[3]), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
    d.text((area[2] // 2, area[3] // 2), text, font=_font(fs), fill=rgb + (255,), anchor="mm")
    buf = io.BytesIO()
    tile.save(buf, "PNG")
    layout = {
        "version": 1, "mode": "band",
        "sourceRect": list(source_rect or [0, 0, pw, ph]),
        "canvas": [W, H], "photoRect": [side, top, pw, ph],
        "fills": [{"rect": [0, 0, W, H], "color": band}],
        "bandColor": band, "protect": [],
        "textAreas": [{"id": "a0", "rect": area}],
        "runs": [{"text": text, "x": area[0], "y": area[1], "size": fs, "color": color}],
        "textColors": [color],
    }
    from photoband.composite import Tile
    return layout, [Tile(area[0], area[1], buf.getvalue())]
