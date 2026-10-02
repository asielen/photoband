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
- A scanned print with a plain border and nothing written on it is no longer called "writing on the
  border" (case C): cases B and C now need writing in the border (letters read, or without OCR marks
  shaped like writing), not just paper-like scan cues; dust, hairs and the print's own paper edge
  are not writing. The banner no longer shows a "% sure" (it was the photo-edge detector's
  confidence); a doubtful edge is called out as something to check instead.
- Text recognition no longer "reads" dust and hairs: characters an OCR engine invents on specks and
  slivers (Tesseract read a hair as "az") never count as text, a caption or writing on the border.
  A read counts only as a word-like token on a mark the size of writing, read with confidence
  (or, at low confidence, on a line shaped like writing). The same rule applies to every engine.
- A caption band along one edge (another app's bottom band, a handwritten strip) is recognized
  without text recognition too: a batch whose pre-flight skips OCR, or the save-time check, no
  longer plans such a photo as uncaptioned and adds a second band. Marks shaped like writing count
  whether or not OCR ran; OCR that reads nothing (Windows OCR reads no handwriting) no longer
  turns them into a plain border. Recognized text in any script counts, including words with
  combining vowel signs or accents (Bengali, Devanagari, Thai, decomposed é).
- A print scanned on a white scanner bed is no longer "writing on the border" because of its own
  paper edge, curled corner or plain margin: unread marks count as writing only when they have
  the texture of writing (several glyph-sized pieces, or the loops of a connected cursive name),
  not one straight edge (with or without text recognition).
- The Edge loupe no longer turns black with a broken image when the pointer goes past the image
  edge, and a failed load anywhere in the preview (loupe, proxy, thumbnails, erase preview) shows a
  note or retries instead of freezing.
- Saving waits for the existing-caption check; geometry is validated before every save, so a second band can no longer be stacked on a captioned photo.
- The "overwrite" on-exists policy only replaces an earlier copy of the *same* photo (source key or pixel hash); anything else gets the next free name.
- Batch Restore returns each file to its state just before the batch.
- Case C (physical caption) overwrite rules are enforced by the backend, and the UI matches them in every mode.
- Metadata: `%` in file names, XMP over 64 KB to JPEG, and DPI for rotated photos.
- Face tags from Lightroom on rotated photos (EXIF orientation 5-8) are placed on the faces again: the overlay, the left-to-right name order, `{names:rows}` and the regions in saved copies were all using a swapped frame.
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
