#!/usr/bin/env bash
# macOS build: Photoband.app in a DMG, one per CPU architecture.
#
#   packaging/macos/build.sh                      # this Mac's arch (arm64 or x86_64)
#   PHOTOBAND_ARCH=x86_64 packaging/macos/build.sh  # Intel build (on Apple Silicon: needs a
#                                                 # universal2 or x86_64 Python, run under Rosetta)
#
# Output: dist/Photoband-<version>-macos-<arch>.dmg
# universal2 is not possible: numpy, opencv, imagecodecs, pydantic-core and Pillow ship no
# universal2 wheels. Ship both DMGs; the arm64 one is the default download.
#
# Needs: Python 3.12 from python.org (PHOTOBAND_PYTHON, default python3), Node.js 22.12+,
#        Xcode command line tools.
# Signing + notarization (all or nothing):
#   PHOTOBAND_CODESIGN_ID     "Developer ID Application: Name (TEAMID)"
#   PHOTOBAND_NOTARY_PROFILE  keychain profile from `xcrun notarytool store-credentials`
# Without them the build is unsigned (fine for local testing; Gatekeeper blocks it elsewhere).
# PHOTOBAND_REQUIRE_SIGNING=1 turns a missing identity into an error (release builds).
set -euo pipefail
cd "$(dirname "$0")/../.."
ROOT=$(pwd)

die() { echo "ERROR: $*" >&2; exit 1; }

# ---- inputs, validated up front --------------------------------------------------------
ARCH=${PHOTOBAND_ARCH:-$(uname -m)}
case "$ARCH" in
  arm64|x86_64) ;;
  universal2) die "universal2 is not supported (several dependencies have no universal2 wheels). Build arm64 and x86_64 separately." ;;
  *) die "PHOTOBAND_ARCH must be arm64 or x86_64 (got '$ARCH')" ;;
esac
PY=${PHOTOBAND_PYTHON:-python3}
command -v "$PY" >/dev/null || die "Python not found ($PY). Install Python 3.12 from python.org or set PHOTOBAND_PYTHON."
command -v node >/dev/null || die "Node.js 22.12+ is required (brew install node@22, or nvm install)."
NODE_VER=$(node --version | sed 's/^v//')
[[ "$(printf '%s\n' 22.12.0 "$NODE_VER" | sort -V | head -1)" == "22.12.0" ]] || die "Node.js 22.12+ is required; found $NODE_VER"
command -v hdiutil >/dev/null || die "hdiutil not found: this script runs on macOS only."

SIGN_ID=${PHOTOBAND_CODESIGN_ID:-}
NOTARY=${PHOTOBAND_NOTARY_PROFILE:-}
if [[ -n "$SIGN_ID" || -n "$NOTARY" ]]; then
  [[ -n "$SIGN_ID" ]] || die "PHOTOBAND_NOTARY_PROFILE is set but PHOTOBAND_CODESIGN_ID is not."
  [[ -n "$NOTARY" ]] || die "PHOTOBAND_CODESIGN_ID is set but PHOTOBAND_NOTARY_PROFILE is not (xcrun notarytool store-credentials <profile>)."
  security find-identity -v -p codesigning | grep -qF "$SIGN_ID" \
    || die "Signing identity not found in the keychain: $SIGN_ID (see: security find-identity -v -p codesigning)"
  xcrun notarytool history --keychain-profile "$NOTARY" >/dev/null 2>&1 \
    || die "Notary profile '$NOTARY' does not work (xcrun notarytool store-credentials $NOTARY ...)."
  echo "Signing as: $SIGN_ID; notarizing with profile $NOTARY"
elif [[ "${PHOTOBAND_REQUIRE_SIGNING:-}" == "1" ]]; then
  die "PHOTOBAND_REQUIRE_SIGNING=1 but PHOTOBAND_CODESIGN_ID / PHOTOBAND_NOTARY_PROFILE are not set."
else
  echo "WARNING: PHOTOBAND_CODESIGN_ID not set: building an UNSIGNED app (local testing only)."
fi

# Run Python as the target arch (a universal2 python.org Python can build either one).
pyrun() { arch "-$ARCH" "$@"; }
PY_ARCH=$(pyrun "$PY" -c 'import platform; print(platform.machine())') \
  || die "$PY cannot run as $ARCH. Use a universal2 python.org Python, or one built for $ARCH."
[[ "$PY_ARCH" == "$ARCH" ]] || die "$PY runs as $PY_ARCH, not $ARCH."
VERSION=$("$PY" -c 'import re; print(re.search(r"^__version__\s*=\s*\"([^\"]+)\"", open("photoband/__init__.py").read(), re.M).group(1))')
echo "Building Photoband $VERSION for macOS $ARCH"

# ---- Python build environment (per arch) ------------------------------------------------
VENV="$ROOT/.venv-build-$ARCH"
if [[ ! -x "$VENV/bin/python" ]]; then
  pyrun "$PY" -m venv "$VENV"
fi
VPY=(arch "-$ARCH" "$VENV/bin/python")
"${VPY[@]}" -m pip install --disable-pip-version-check -q --upgrade pip
"${VPY[@]}" -m pip install --disable-pip-version-check -r requirements/build.lock
"${VPY[@]}" -m pip install --disable-pip-version-check --no-deps -e .

# ---- vendor + UI -------------------------------------------------------------------------
[[ -x vendor/exiftool/exiftool ]] || "${VPY[@]}" scripts/fetch_vendor.py
(cd ui && npm ci --no-audit --no-fund && npm run build)

# ---- PyInstaller ---------------------------------------------------------------------------
DIST="dist/$ARCH"
PHOTOBAND_ARCH=$ARCH "${VPY[@]}" -m PyInstaller packaging/photoband.spec --noconfirm --clean \
  --distpath "$DIST" --workpath "build/$ARCH"
APP="$DIST/Photoband.app"
[[ -d "$APP" ]] || die "PyInstaller did not produce $APP"

notarize() {  # notarize <file>; fails unless Apple accepts it
  local out status
  out=$(xcrun notarytool submit "$1" --keychain-profile "$NOTARY" --wait --output-format json)
  echo "$out"
  status=$(printf '%s' "$out" | "${VPY[@]}" -c 'import json,sys; print(json.load(sys.stdin).get("status",""))')
  [[ "$status" == "Accepted" ]] || die "Notarization of $1 failed (status: ${status:-unknown}). See: xcrun notarytool log <id> --keychain-profile $NOTARY"
}

if [[ -n "$SIGN_ID" ]]; then
  # Sign inside-out, never with --deep: every nested Mach-O first (deepest paths first),
  # then the app bundle itself with the entitlements (that also signs the main executable).
  echo "Signing nested code..."
  find "$APP/Contents" -type f \( -name '*.so' -o -name '*.dylib' -o -perm -u+x \) -print0 \
    | while IFS= read -r -d '' f; do
        if file -b "$f" | grep -q 'Mach-O'; then
          printf '%s\t%s\n' "$(awk -F/ '{print NF}' <<<"$f")" "$f"
        fi
      done \
    | sort -t$'\t' -k1,1nr | cut -f2- \
    | while IFS= read -r f; do
        [[ "$f" == "$APP/Contents/MacOS/Photoband" ]] && continue
        codesign --force --options runtime --timestamp --sign "$SIGN_ID" "$f"
      done
  codesign --force --options runtime --timestamp \
    --entitlements packaging/macos/entitlements.plist --sign "$SIGN_ID" "$APP"
  codesign --verify --strict --verbose=2 "$APP"
  echo "Notarizing the app..."
  ditto -c -k --keepParent "$APP" "$DIST/Photoband.zip"
  notarize "$DIST/Photoband.zip"
  rm -f "$DIST/Photoband.zip"
  xcrun stapler staple "$APP"
fi

# ---- DMG -----------------------------------------------------------------------------------
DMG="dist/Photoband-$VERSION-macos-$ARCH.dmg"
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
ditto "$APP" "$STAGE/Photoband.app"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "Photoband" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
if [[ -n "$SIGN_ID" ]]; then
  codesign --force --timestamp --sign "$SIGN_ID" "$DMG"
  echo "Notarizing the DMG..."
  notarize "$DMG"
  xcrun stapler staple "$DMG"
  spctl --assess --type open --context context:primary-signature --verbose "$DMG" || true
fi
echo "Built $DMG"
