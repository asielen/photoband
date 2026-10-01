# PyInstaller spec for Photoband (Windows, macOS; Linux works for CI smoke tests).
#   pyinstaller packaging/photoband.spec --noconfirm
# Normally run by packaging/windows/build.ps1 or packaging/macos/build.sh.
# Expects: ui/dist (npm run build), fonts/ (committed), vendor/exiftool (scripts/fetch_vendor.py)
# and optionally vendor/tesseract.
#
# Environment:
#   PHOTOBAND_ALLOW_NO_EXIFTOOL=1  build without vendor/exiftool (the app then needs a system ExifTool)
#   PHOTOBAND_ARCH                 macOS: arm64 or x86_64 (default: this Python's arch). universal2 is
#                                  not possible: numpy, opencv, imagecodecs, pydantic-core and Pillow
#                                  ship no universal2 wheels. Build each arch with its own Python.
#   PHOTOBAND_MACOS_MIN            override LSMinimumSystemVersion (default: highest macOS minimum among
#                                  the installed wheels, so the app never claims to run where it can't)
#   PHOTOBAND_CODESIGN_ID          macOS signing identity ("Developer ID Application: ...")
import os
import platform
import re
import sys
from importlib import metadata

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))
is_mac = sys.platform == 'darwin'
is_win = sys.platform == 'win32'


def read_version():
    with open(os.path.join(ROOT, 'photoband', '__init__.py'), encoding='utf-8') as fh:
        m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', fh.read(), re.M)
    if not m:
        raise SystemExit('photoband/__init__.py has no __version__')
    return m.group(1)


VERSION = read_version()


def fail(msg):
    raise SystemExit(f'\nBUILD FAILED: {msg}\n')


# ---- resources --------------------------------------------------------------------------
if not os.path.isfile(os.path.join(ROOT, 'ui', 'dist', 'index.html')):
    fail('ui/dist is missing. Run: cd ui && npm ci && npm run build')
if not os.path.isfile(os.path.join(ROOT, 'fonts', 'manifest.json')):
    fail('fonts/manifest.json is missing (fonts/ is committed; is this a full checkout?)')

datas = [
    (os.path.join(ROOT, 'ui', 'dist'), 'ui/dist'),
    (os.path.join(ROOT, 'fonts'), 'fonts'),
    (os.path.join(ROOT, 'LICENSE'), '.'),
    (os.path.join(ROOT, 'THIRD_PARTY_NOTICES.md'), '.'),
]
exiftool_dir = os.path.join(ROOT, 'vendor', 'exiftool')
exiftool_bin = os.path.join(exiftool_dir, 'exiftool.exe' if is_win else 'exiftool')
if os.path.isfile(exiftool_bin):
    datas.append((exiftool_dir, 'vendor/exiftool'))
elif os.environ.get('PHOTOBAND_ALLOW_NO_EXIFTOOL') == '1':
    print('WARNING: building WITHOUT a bundled ExifTool (PHOTOBAND_ALLOW_NO_EXIFTOOL=1). '
          'Metadata will not work unless ExifTool is installed on the user\'s machine.')
else:
    fail(f'{exiftool_bin} is missing. Run `python scripts/fetch_vendor.py`, '
         'or set PHOTOBAND_ALLOW_NO_EXIFTOOL=1 to build without it.')
tesseract_dir = os.path.join(ROOT, 'vendor', 'tesseract')
if os.path.isdir(tesseract_dir) and os.listdir(tesseract_dir):
    datas.append((tesseract_dir, 'vendor/tesseract'))

# ---- hidden imports ---------------------------------------------------------------------
# imagecodecs loads its codec extensions lazily (by attribute name), so PyInstaller can't see
# them. These are the ones tifffile needs for the TIFF compressions imageio.py reads and
# writes: LZW/PackBits/predictors (_imcd), Deflate (_zlib, _deflate), Zstd, LZMA, and JPEG
# (old-style and lossless JPEG in TIFF). _shared/_shared_cython are needed by all of them;
# without _shared_cython every LZW TIFF fails. collect_submodules('imagecodecs') would add
# ~45 codecs (~70 MB with their libraries) we never use.
IMAGECODECS = ['imagecodecs._shared', 'imagecodecs._shared_cython', 'imagecodecs._imcd',
               'imagecodecs._zlib', 'imagecodecs._deflate', 'imagecodecs._zstd', 'imagecodecs._lzma',
               'imagecodecs._jpeg8', 'imagecodecs._ljpeg', 'imagecodecs._jpegsof3']
hidden = (collect_submodules('uvicorn') + collect_submodules('webview') + collect_submodules('captiontokens')
          + collect_submodules('photoband') + IMAGECODECS + ['reedsolo', 'psutil'])
if is_win:
    hidden += ['winrt.windows.media.ocr', 'winrt.windows.graphics.imaging', 'winrt.windows.storage.streams',
               'winrt.windows.globalization', 'winrt.windows.foundation', 'winrt.windows.foundation.collections']

# ---- macOS architecture and minimum version -------------------------------------------
target_arch = None
min_macos = None
if is_mac:
    target_arch = os.environ.get('PHOTOBAND_ARCH') or platform.machine()
    if target_arch not in ('arm64', 'x86_64'):
        fail(f'PHOTOBAND_ARCH={target_arch!r}: use arm64 or x86_64 (universal2 wheels do not exist '
             'for numpy/opencv/imagecodecs/pydantic-core/Pillow)')
    if target_arch != platform.machine():
        fail(f'PHOTOBAND_ARCH={target_arch} but this Python runs as {platform.machine()}; '
             f'use a {target_arch} Python (e.g. `arch -{target_arch} python3 ...`)')

    def wheel_min_macos(arch):
        """Highest macosx_X_Y tag among installed wheels for this arch."""
        best = (11, 0)
        for dist in metadata.distributions():
            try:
                wheel = dist.read_text('WHEEL') or ''
            except Exception:
                continue
            for tag in re.findall(r'^Tag:\s*(\S+)', wheel, re.M):
                for m in re.finditer(r'macosx_(\d+)_(\d+)_(\w+)', tag):
                    if m.group(3) in (arch, 'universal2', 'universal'):
                        best = max(best, (int(m.group(1)), int(m.group(2))))
        return f'{best[0]}.{best[1]}'

    min_macos = os.environ.get('PHOTOBAND_MACOS_MIN') or wheel_min_macos(target_arch)
    print(f'macOS build: arch={target_arch}, LSMinimumSystemVersion={min_macos}')

a = Analysis(
    [os.path.join(ROOT, 'packaging', 'launcher.py')],
    pathex=[ROOT, os.path.join(ROOT, 'packages', 'captiontokens')],
    datas=datas,
    hiddenimports=hidden,
    excludes=['tkinter', 'matplotlib', 'IPython', 'pytest', 'playwright'],
    noarchive=False,
)
pyz = PYZ(a.pure)
icon = os.path.join(ROOT, 'packaging', 'icon.ico' if is_win else 'icon.icns')
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name='Photoband',
    console=False,
    icon=icon if os.path.exists(icon) else None,
    target_arch=target_arch,
    codesign_identity=os.environ.get('PHOTOBAND_CODESIGN_ID') or None,
    entitlements_file=os.path.join(ROOT, 'packaging', 'macos', 'entitlements.plist') if is_mac else None,
)
coll = COLLECT(exe, a.binaries, a.datas, name='Photoband')
if is_mac:
    app = BUNDLE(
        coll,
        name='Photoband.app',
        icon=icon if os.path.exists(icon) else None,
        bundle_identifier='app.photoband.Photoband',
        version=VERSION,
        info_plist={
            'CFBundleShortVersionString': VERSION,
            'CFBundleVersion': VERSION,
            'NSHighResolutionCapable': True,
            'LSMinimumSystemVersion': min_macos,
            'NSRequiresAquaSystemAppearance': False,
            'CFBundleDocumentTypes': [{
                'CFBundleTypeName': 'Image', 'CFBundleTypeRole': 'Editor',
                'LSItemContentTypes': ['public.tiff', 'public.jpeg', 'public.png'],
            }],
        },
    )
