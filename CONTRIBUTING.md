# Developing Photoband

This is the developer guide: setup, tests, packaging and architecture. If you only want to use the app, see the [README](README.md).

Photoband is built for family-archive scans. TIFF (8 and 16-bit) is edited directly, and photo pixels are never resampled or recompressed. All metadata is carried over, face regions are remapped onto the new canvas, and every save is verified and atomic.

The design spec is a separate Claude Doc. `docs/spec-extract.txt` holds a plain-text copy.

## Setup

You need **Python 3.11+** and **Node.js 22.12+** (`nvm install` reads `.nvmrc`). Then run one command from the repository folder:

```bash
python3 scripts/bootstrap.py        # macOS / Linux
py scripts\bootstrap.py             # Windows
```

It does the following:

- creates a virtual environment in `.venv`
- installs the pinned dependencies (`requirements/dev.lock`, or `requirements/app.lock` with `--no-dev`)
- installs `photoband` and `captiontokens` in editable mode
- builds the UI (skipped when `ui/dist` is up to date)
- finds or fetches ExifTool

At the end it prints how to run the app. Re-running it is safe. Options:

- `--e2e`: also install Playwright's Chromium for the browser tests
- `--no-dev`: runtime dependencies only
- `--rebuild-ui`: rebuild the UI even if it looks up to date
- `--skip-ui`, `--skip-exiftool`: leave out those steps
- `--venv <folder>`: use another virtual environment folder

Run the app with the venv's Python, or activate the venv first (`source .venv/bin/activate`, or `.venv\Scripts\activate` on Windows):

```bash
.venv/bin/python -m photoband serve    # browser mode: opens http://127.0.0.1:<port>/?b=<one-time code>
.venv/bin/python -m photoband          # desktop app (pywebview window; on Linux use serve)
```

On Windows, use `.venv\Scripts\python` instead of `.venv/bin/python`.

In serve mode the browser is opened with a one-time code (`?b=`), never the API token, because a browser's command line is visible to other local users. The server trades the code for the token once. The console also prints the full `?t=<token>` address for opening the app by hand; `--no-browser` skips opening the browser and `--port` picks the port.

ExifTool is needed for metadata. The bootstrap uses one already on `PATH`. Otherwise install it yourself with `brew install exiftool`, `winget install OliverBetz.ExifTool` or `sudo apt install libimage-exiftool-perl`. Tesseract is optional: it is the OCR fallback when the OS engine is unavailable.

For UI hot-reload, run `python -m photoband serve --port 8765 --no-browser` together with `cd ui && npm run dev`. Vite proxies `/api` and `/fonts` to the backend. You still need the token from the `?t=` address the backend prints.

To set things up by hand instead of with the bootstrap script:

```bash
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -r requirements/dev.lock
python -m pip install --no-deps -e .          # installs photoband AND captiontokens
(cd ui && npm ci && npm run build)
```

### Environment variables

Runtime:

| Variable | Effect |
|---|---|
| `PHOTOBAND_HOME` | The app data folder (settings, templates, drafts, cache, logs). The default is `%APPDATA%\Photoband`, `~/Library/Application Support/Photoband` or `~/.local/share/Photoband`. |
| `PHOTOBAND_TOKEN` | Uses this per-launch API token (at least 16 characters) instead of a random one. Used by tests and scripts. |
| `PHOTOBAND_EXIFTOOL` | The path to the ExifTool program. It is tried before the bundled copy and `PATH`. |
| `PHOTOBAND_TESSERACT` | The path to the Tesseract program, for the OCR fallback. |
| `PHOTOBAND_UI_DIST` | Serves the UI from this folder instead of the bundled or repository `ui/dist`. |
| `PHOTOBAND_NO_SYSTEM_FONTS` | Set to any value to skip scanning installed system fonts (faster, reproducible). |
| `PHOTOBAND_NO_NATIVE_DIALOGS` | Set to any value to never open OS file dialogs. The in-app browser is used instead (tests, headless). |
| `PHOTOBAND_DEBUG` | `1` turns on debug-level logging in `logs/photoband.log`, also printed to the console. |
| `PHOTOBAND_TEST_WORKER_DELAY` | Tests only: seconds each batch worker waits before placing a file. |
| `PHOTOBAND_SHOTS` | Tests only: where `tests/e2e/test_ui_review.py` saves its screenshots. |
| `PYWEBVIEW_LOG` | pywebview's own log level (for example `debug`) when the desktop window misbehaves. |

Build (packaging scripts):

| Variable | Effect |
|---|---|
| `PHOTOBAND_ALLOW_NO_EXIFTOOL` | `1` lets the PyInstaller build continue without `vendor/exiftool`. |
| `PHOTOBAND_ARCH` | macOS: `arm64` or `x86_64`. The default is this Mac's architecture. |
| `PHOTOBAND_MACOS_MIN` | macOS: overrides the computed `LSMinimumSystemVersion`. |
| `PHOTOBAND_PYTHON` | The Python used to create the build venv. |
| `PHOTOBAND_CODESIGN_ID`, `PHOTOBAND_NOTARY_PROFILE` | macOS signing identity and notarytool keychain profile. Set both or neither. |
| `PHOTOBAND_REQUIRE_SIGNING` | macOS: `1` makes an unsigned build an error. |
| `PHOTOBAND_SIGN_CERT_SHA1`, `PHOTOBAND_SIGNTOOL`, `PHOTOBAND_SIGN_TIMESTAMP` | Windows: signs the app and installer with this certificate thumbprint, signtool path and timestamp URL. |

## Tests

```bash
python -m pytest -q --ignore=tests/e2e # ~470 backend tests; fixtures are generated on first run (scripts/make_fixtures.py, add --big for the 600 MB scan)
python -m pytest -q -n auto --ignore=tests/e2e   # the same, in parallel (pytest-xdist); run once serially first so the fixtures exist
ruff check .                           # lint
(cd ui && npx vitest run && npm run check)   # layout engine + editor markup, type check
python -m pytest -q tests/e2e          # Playwright: the real UI against the real backend (bootstrap --e2e and a built ui/dist first)
```

The e2e suite saves screenshots of every major screen to `tests/_artifacts/ui/`. CI (`.github/workflows/ci.yml`) runs the following:

- Linux: lint, a UI build, svelte-check, vitest and the non-e2e tests.
- Windows and macOS: the non-e2e tests, a PyInstaller build, and `scripts/smoke_frozen.py` against the frozen app. The smoke test starts it in serve mode, loads the UI and fonts, and opens an LZW TIFF.

## Packaging

The version has one source, `photoband/__init__.py`. The wheel, the PyInstaller bundle, the Windows installer and the DMG name all read it.

| OS | Command | Output |
|---|---|---|
| Windows | `powershell -ExecutionPolicy Bypass -File packaging\windows\build.ps1` (Python 3.12 x64, Node 22.12+, Inno Setup 6) | `dist\PhotobandSetup-<version>.exe` |
| macOS | `packaging/macos/build.sh` once per arch (`PHOTOBAND_ARCH=arm64` / `x86_64`). Set `PHOTOBAND_CODESIGN_ID` and `PHOTOBAND_NOTARY_PROFILE` for a signed, notarized build. | `dist/Photoband-<version>-macos-<arch>.dmg` |

Both scripts work the same way:

1. Create their own build venv (`.venv-build`, or `.venv-build-<arch>` on macOS) from `requirements/build.lock`.
2. Fetch ExifTool if it is missing.
3. Build the UI.
4. Run PyInstaller with `packaging/photoband.spec`. Its entry point is `packaging/launcher.py`.

The spec bundles `ui/dist`, `fonts/`, `vendor/exiftool` (required) and `vendor/tesseract` (optional).

A universal2 macOS build isn't possible, because numpy, opencv, imagecodecs, pydantic-core and Pillow ship no universal2 wheels.

When the WebView2 runtime is missing on Windows (or, on Linux, GTK/Qt for pywebview), the app opens in the default browser with a one-line notice. The installer normally prevents this on Windows by installing the runtime.

When the desktop window is closed, it first asks the editor to send any edits not yet saved as a draft (waiting at most 3 seconds), then closes.

### Maintainer tasks

- **Dependencies**: edit `pyproject.toml`, then run `python -m pip install uv && python scripts/update_locks.py` (add `--upgrade` to refresh all pins). Commit `requirements/*.lock`. The locks are universal (`uv pip compile --universal`), so one file covers all platforms.
- **ExifTool version**: edit `scripts/vendor.lock.json`, which holds the version, the file names and the SHA-256 of each file. `python scripts/fetch_vendor.py --no-verify` prints the hash of what it downloaded. Check that hash against https://exiftool.org/checksums.txt before pinning it. exiftool.org removes old versions, so the script falls back to SourceForge.
- **Fonts**: `fonts/` is committed, so nobody needs to fetch fonts to develop or build. `scripts/fetch_fonts.py` re-downloads the fonts from a pinned google/fonts commit (`GOOGLE_FONTS_COMMIT`) and checks their script coverage.

## License

MIT (see `LICENSE`). `packages/captiontokens` is MIT as well. Third-party components and their licenses are listed in `THIRD_PARTY_NOTICES.md`; keep that file current when adding a bundled dependency.

---

## Architecture

```
┌──────────────────────── pywebview window (WebView2 / WKWebView) ─────────────────────────┐
│  Svelte 5 + TypeScript UI (ui/)                                                           │
│   layout.ts  ── the ONE layout engine: positioned text runs in output pixels             │
│   render.ts  ── draws runs: preview (scaled transform) and save tiles (≤4096 px PNGs)    │
│   store.svelte.ts ── per-photo sessions, drafts, undo, save flow; BatchView.svelte       │
└──────────────────────────────────┬───────────────────────────────────────────────────────┘
                                   │ HTTP on 127.0.0.1, per-launch token, CSP
┌──────────────────────────────────▼───────────────────────────────────────────────────────┐
│  FastAPI backend (photoband/)                                                             │
│   imageio.py   decode/encode TIFF/JPEG/PNG (tifffile, Pillow, own 16-bit PNG writer)     │
│   metadata.py  ExifTool JSON → token fields, MWG/MP face regions                          │
│   composite.py borders + byte-exact photo copy + text tiles (ICC-correct colors)         │
│   save.py      backup → composite → marker → encode → metadata → verify → atomic replace │
│   metawrite.py ExifTool copy, dimensions, region remap, photoband XMP record             │
│   detect.py / ocr.py / erase.py / existing.py   existing captions: cases A–D             │
│   marker.py    hidden band marker (robust DCT-chip layer + LSB payload)                   │
│   batch.py     journal, spawn-worker pool, resume, restore originals                      │
│  packages/captiontokens  token parser shared with photokin                                │
└───────────────────────────────────────────────────────────────────────────────────────────┘
```

The main design decisions:

- **What you see is what gets written.** The UI lays text out once, in output pixels. The preview draws those same runs under a scale transform, and saving draws them at full size into transparent tiles. The backend never renders text. It fills borders, copies the photo pixels byte for byte, and alpha-composites the tiles, converting text and band colors into the file's ICC space.
- **Archival safety.**
  - Saves go through a temp file next to the destination, then a verify step, fsync and `os.replace`.
  - Overwrites make a verified backup first (`_originals/`, versioned), and never reuse a stale one.
  - A save refuses when the file changed since it was opened, is read-only or locked, or is a scan with a handwritten caption.
- **The photoband record.** A compressed XMP record (`XMP-photoband:Record`) stores the template, layout, block text, the photo's offset and a pixel hash. Re-opening one of the app's own outputs crops back to the original pixels exactly (case A), so bands never stack.
- **Hidden marker.** If metadata is stripped, a marker in the band's background pixels still identifies the band. Its robust layer survives JPEG q70 and 50% downsizing for photos ≥1600 px wide. Its payload survives lossless copies. The marker holds only what is printed in the band. "Save copy without hidden marker" removes it, including old markers inside an erased band.

## Spec coverage

The table maps each spec section to its code and tests.

| Spec section | Where | Tests |
|---|---|---|
| Core workflow, drafts, undo | `ui/src/lib/store.svelte.ts`, `actions.ts`, `photoband/drafts.py` | e2e, `test_safety.py` |
| Batch mode | `BatchView.svelte`, `photoband/batch.py` | `test_batch.py`, e2e `test_batch` |
| File formats | `photoband/imageio.py`, `colors.py` | `test_formats.py`, `test_pipeline.py` |
| Metadata extraction, face regions | `photoband/metadata.py`, `captiontokens/faces.py` | `test_metadata.py` |
| Token system | `packages/captiontokens` | `packages/captiontokens/tests/*` |
| Editor UI | `ui/src/components/*` | e2e |
| Templates and styling | `photoband/templates.py`, `ui/src/lib/layout.ts` | `ui/src/tests/layout.test.ts` |
| Rendering engine | `layout.ts`, `render.ts`, `composite.py` | vitest, `test_pipeline.py` |
| Saving and metadata on output | `save.py`, `metawrite.py`, `record.py` | `test_pipeline.py`, `test_metawrite.py`, `test_safety.py` |
| Existing text (cases A–D) | `detect.py`, `ocr.py`, `erase.py`, `existing.py` | `test_detect.py`, `test_existing_hard.py` |
| Hidden band marker | `marker.py` | `test_marker.py`, `test_marker_pipeline.py` |
| Security | `security.py`, `server.py` | `test_security.py` |

See `docs/STATUS.md` for the acceptance checklist, the adversarial review log, and known limitations, and `CHANGELOG.md` for changes.
