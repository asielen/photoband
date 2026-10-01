# Changelog

All notable changes to Photoband. The version lives in one place: `photoband/__init__.py`.

## Unreleased

### Install and packaging
- One-command developer setup: `python3 scripts/bootstrap.py` (`py scripts\bootstrap.py` on Windows).
- Pinned, hashed Python dependencies in `requirements/*.lock` (universal: Windows, macOS, Linux).
- `pip install .` and `pipx install .` now work outside the repository: the built UI and the
  fonts ship inside the wheel. captiontokens is installed with photoband (no separate step,
  and no lookup of a same-named PyPI package).
- New dependencies declared: `psutil`. `opencv-python-headless` is limited to `<5`.
  The macOS/Windows OCR packages are now installed automatically on their platform.
- Frozen app: fixed the entry point (it could not start at all), LZW/Deflate/Zstd/LZMA/JPEG TIFF
  codecs are bundled, and the windowed Windows build no longer crashes on launch. Start-up
  errors are logged and shown in a message box.
- Logs: `photoband.log` (rotating, 10 MB x 3) in the app data `logs/` folder; set
  `PHOTOBAND_DEBUG=1` for debug logging.
- Windows installer: per-user install without an admin prompt, x64 only, installs the WebView2
  runtime when it is missing, optional code signing.
- macOS: separate arm64 and x86_64 DMGs (universal2 is impossible with the current
  dependencies); the minimum macOS version is taken from the bundled wheels. The DMG is signed,
  notarized and stapled as well as the app.
- The build fails if ExifTool is missing (unless `PHOTOBAND_ALLOW_NO_EXIFTOOL=1`).
- CI: lint and tests on Linux; tests, a PyInstaller build and a smoke test of the frozen app on
  Windows and macOS.

### Safety and correctness
- Saving waits for the existing-caption check; geometry is validated before every save, so a second band can no longer be stacked on a captioned photo.
- The "overwrite" on-exists policy only replaces an earlier copy of the *same* photo (source key or pixel hash); anything else gets the next free name.
- Batch Restore returns each file to its state just before the batch.
- Case C (physical caption) overwrite rules are enforced by the backend, and the UI matches them in every mode.
- Metadata: `%` in file names, XMP over 64 KB to JPEG, and DPI for rotated photos.
- Security: allow-list holes in batch staging, `subfolderName`, and batch exclude/restore; typed settings; one-time launch code instead of the token in the browser's command line; decode-budget and IFD-count limits; ExifTool timeouts.
- Desktop close flushes pending drafts.

### Interface
- Save buttons say exactly what they do: **Save copy** / **Save copy & next**, and **Overwrite original** with its own ▾ menu for **Overwrite original & next** (Ctrl/⌘+Shift+Enter).
- Tooltips everywhere: themed, with shortcut chips, shown on hover and keyboard focus, and on disabled controls they say why. Can be turned off in Settings › General. A test keeps every icon button covered.
- Simple first, deep second: Style, Layout, Settings and Batch show the common controls and keep the rest under "More options" / "Advanced" (remembered).
- Command palette (Ctrl/⌘+K) with every action and its shortcut.
- First-run hint strip and Help › Getting started.
- Batch is a 3-step flow (Choose photos → Check → Save) with a plain-language summary.
- Caption formats: "Insert field" with plain names and examples, and a live example line.
- Works at the 960×640 minimum window: inspector, toolbar labels, status-bar size readout.
- Tab moves focus (panels toggle with Ctrl/Cmd+\); Help lists every shortcut.
- "After only" view removed (spec: before and after always visible).
- Plain-language warnings and batch wording; shaped status badges; toasts never cover dialogs.

## 1.0.0

- First version (see docs/STATUS.md for the acceptance checklist and known limitations).
