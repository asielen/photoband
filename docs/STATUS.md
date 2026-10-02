# Photoband MVP: status report

Built overnight on 30 Sep 2026 from the spec doc "Photo Caption Band Editor — Spec" (plain-text copy in `docs/spec-extract.txt`). The work was done in a 2-vCPU Linux sandbox with no Windows or Mac available. Anything that needs those machines is marked **unverified**.

## Test suite

| Suite | Count | Result |
|---|---|---|
| Python backend (`pytest --ignore=tests/e2e`) | 468 tests | all pass |
| UI unit tests (`cd ui && npx vitest run`) | 73 tests | all pass |
| End-to-end, real UI + backend in Chromium (`pytest tests/e2e`) | 39 flows | all pass |
| Type check (`npx svelte-check --tsconfig ./tsconfig.app.json`) | — | 0 errors |
| Lint (`ruff check .`) | — | clean |

The e2e flows are:

- open a folder
- prefill and edit a caption
- save a copy (16-bit ProPhoto TIFF round trip)
- switch templates (custom text is kept)
- the Style, Layout and Metadata tabs
- the faces overlay
- row-grouped names and Cyrillic names
- existing captions, cases B, C and D
- blocked formats
- the Settings and format editor with autocomplete
- overwrite with backup, which then re-opens as case A
- a full batch run

Screenshots are written to `tests/_artifacts/ui/`.

## Acceptance checklist (from the spec)

| # | Item | Status | Evidence / notes |
|---|---|---|---|
| 1 | Photo region pixel-identical for TIFF and PNG | ✅ | Save verify step plus `test_pipeline`, `test_formats`. Covers 8/16-bit, gray, alpha, planar, tiled, BigTIFF and big-endian files. |
| 2 | Bit depth, channels, compression, ICC and DPI match the source | ✅ | LZW/Deflate/PackBits/ZSTD/LZMA and predictor are all kept. As the spec allows, JPEG-in-TIFF is written as Deflate and tiled files as striped, with a note in the save log. |
| 3 | Preview and output line breaks match at every zoom | ✅ | Preview and saved file use one layout engine and the same text runs. Measured ink-width difference at 5–50% zoom is ≤1.45%. |
| 4 | Face regions land on the same faces | ✅ (math) | MWG, MP and IPTC ImageRegion regions are remapped for orientations 1–8, crops and re-captions. Not yet opened in Lightroom or digiKam themselves. |
| 5 | Rotated JPEG gets its band at the visual bottom | ✅ | `test_rotated_jpeg_band_at_visual_bottom_and_regions` |
| 6 | Editing a custom block survives a template switch | ✅ | e2e `test_template_switch_keeps_custom` |
| 7 | Overwrite with backup leaves an untouched copy in `_originals` | ✅ | Backups are verified by SHA-256 and versioned; a stale backup is never reused. |
| 8 | Re-caption restores the editor state without stacking bands | ✅ | Tested on TIFF and JPEG over repeated re-captions, including after a lossless rotation in another app. |
| 9 | A stripped-metadata file from this app is detected, read and rebuilt without losing photo pixels | ✅ | Marker + payload → case A. JPEG re-save → marker edge + OCR. |
| 10 | A handwritten Polaroid caption is read, erased on a copy with no visible patch, and kept as originalText | ✅ synthetic / ⚠️ real | Passes on synthetic scans (local-background inpainting plus grain). Real cursive needs Apple Vision, which was written but is **unverified**. Tesseract reads print well and handwriting poorly. |
| 11 | Text over the photo is flagged and never erased | ⚠️ | Not officially supported: best-effort warning only. Case D: 30/30 date stamps flagged with Tesseract; without it, coloured stamps are flagged unverified and other printed text isn't looked for. Never erased either way. |
| 12 | Near-white sky at the photo edge is not mistaken for a band | ✅ | Fixture 14 and the reviewer's harder set. |
| 13 | Killing the app mid-edit and relaunching restores the draft | ✅ | Drafts autosave within about 0.8–3 s. Tested with a reload. |
| 14 | Killing the app mid-save leaves the original intact | ✅ | 55 random SIGKILLs during a 100 MB overwrite: 0 corrupt files, 0 bad backups. |
| 15 | All performance targets met on the reference laptop | ⚠️ partial | See Performance below. |
| 16 | Marker read after JPEG q70 + 50% resize gives the exact photo edge | ✅ ≥1600 px | Passes for all 5 templates at photo widths of 1600 px and up. Below about 1600 px it can fail (documented in `marker.py`). |
| 17 | Payload from a metadata-stripped PNG restores the editor state exactly | ✅ | `test_stripped_png_payload_restores_state`, `test_marker_pipeline` |
| 18 | No marker pattern visible at 400% or after a +3 stop stretch | ⚠️ partial | Not visible at 400%. After the +3 stop stretch the old grid is gone, but a faint random grain mosaic remains. That is the trade-off for surviving q70 + 50%; the numbers are in `marker.py`. |
| 19 | Batch output matches single-photo output | ✅ | Pixel-identical, record identical apart from timestamps. |
| 20 | Batch overwrite can be fully reversed with Restore originals | ✅ | 198/198 byte-identical, including after a killed run. Files edited after the batch are skipped unless you confirm. |
| 21 | Before/After panes stay in sync in all three views | ✅ | Batch review and replacement both use the editor preview. |

### Performance

Measured on this 2-vCPU sandbox while other jobs were running. The laptop column is an estimate.

| Target | Measured here | Est. 2022 laptop |
|---|---|---|
| Launch < 3 s | 1.3 s | ✅ ~1 s |
| Open 50 MP 8-bit TIFF < 1.5 s | ~1.4 s proxy, 2.3 s including caption check | ✅ ~0.6–1 s |
| Open 600 MB 16-bit < 5 s + progress | 4.0 s (indeterminate spinner) | ✅ |
| Cached photo < 300 ms | 130–300 ms | ✅ |
| Keystroke → preview < 50 ms | median 3 ms | ✅ |
| Save 50 MP TIFF incl. verify < 5 s | 4.5 s (LZW), 5.1 s (uncompressed, earlier build) | ✅ |
| Pre-flight 200 photos < 60 s | ~0.8 s per photo with OCR | ⚠️ ~70–100 s. The next step is running the caption check on the proxy first. |
| Batch 100 × 50 MP < 8 min | ~19 s per file per worker | ⚠️ borderline. Workers are now capped by available memory. |

Memory: full decodes go through a priority gate with a RAM budget. Images over min(4 GB, 25% of RAM) decoded are refused with a clear message, which also blocks decompression bombs. Opening four 50 MP TIFFs peaks around 1 GB.

## How it was reviewed

There were 12 independent adversarial reviews during the build, then a 13th full code review (six parallel reviewers plus a verification pass on the merged fixes; details below). Each reviewer found problems, and every finding was either fixed with regression tests or is listed under Known limitations below.

| # | Review | Worst findings (now fixed) |
|---|---|---|
| 1 | Visual design | Preview text 10–27% wider than output; toolbar overflow at the minimum window size; HUD covering the band; template sizes too small |
| 2 | UX flows and copy | "Save copy as" could silently replace originals; dialogs hidden behind Settings; edits lost during a save; focus loss breaking the keyboard loop |
| 3 | Tokens and metadata | Bad EXIF dates blocking the fallback; empty styled tokens turning the rest bold; tilted rows split apart; raw braces leaking into captions |
| 4 | Layout fidelity | Letter-spacing and kerning mismatch between measure and draw; tile clipping; soft hyphens; small caps faked in italics |
| 5 | Pixel and format fidelity | MINISWHITE alpha inverted; YCbCr TIFF colours; 16-bit gray+alpha PNG; sRGB detected by profile name |
| 6 | Metadata write-back | ExifTool failures not detected; custom XMP namespaces dropped; IPTC regions and points not remapped; Lightroom crop would cut the band off |
| 7 | Save safety | A stale backup could block backing up a newer original; restore destroyed later edits; verify gaps; temp-file litter; read-only files |
| 8 | Existing text | Tilted scans; scanner lid margins; case C almost never triggering; flat-fill patches; white-wall false positives; ghost lines |
| 9 | Hidden marker | "Remove marker" leaked the old caption; stale markers left after erase; wrong exact edges; visible grid; 5× too slow |
| 10 | Batch mode | Orphaned workers after a kill; crash before staging lost the batch; Cancel dropped photos; incomplete CSV |
| 11 | Security | AppleScript injection through file names; ExifTool path gave code execution; allow-list was advisory; port squatting; zlib bombs |
| 12 | Performance | Out-of-memory from parallel full decodes; each file decoded 3×; whole-canvas marker work |

### Review 13: full code review (1 Oct 2026)

Six reviewers (backend spec conformance, UI/UX, correctness, security, code quality, clean install and packaging), then one verifier on the merged result. Everything below is fixed with regression tests (`tests/test_review_*.py`, `ui/src/tests/review_*.test.ts`, `tests/e2e/test_ui_review.py`).

| Area | Worst findings (now fixed) |
|---|---|
| Packaging | Frozen app could not start (relative-import entry point); LZW TIFFs failed in the build; windowed Windows build crashed on launch (no stdout); universal2 macOS impossible; release could ship without ExifTool |
| Install | Fresh install pulled OpenCV 5; no venv step; wrong Node version; `pip install .` broken; captiontokens a bare PyPI name. Now `python3 scripts/bootstrap.py`, hashed lockfiles, single version source, CI |
| Data safety | "Overwrite" on-exists replaced *other* photos' outputs; batch Restore wrote back the pre-caption original instead of the pre-batch file; newer draft deleted after overwrite; case C overwrite trusted the client |
| Wrong output | Saving during the existing-caption check (and several draft/undo/reload paths) stacked a second band; erase draft saved as a new band; template-switch race saved the wrong text |
| Metadata | `%` in a file name lost all metadata (ExifTool format codes); XMP over 64 KB dropped when saving to JPEG; X/Y DPI not swapped for orientations 5–8 |
| Security | Batch `copyAs` and `subfolderName` wrote outside the allow-list; `exclude` + Restore could overwrite any file; settings type confusion bricked launch; token in the browser's argv; TIFF decode-budget bypass; 100k-IFD probe; ExifTool hang held the pool; CSV formula injection |
| UX | Tab hid both panels; Style/Layout tabs cut off below 1280 px; Overwrite lost its label and used a download icon; "After only" mode broke the always-side-by-side rule; toasts covered dialogs; colour-only filmstrip badges; jargon in warnings and batch |

## Open items for the owner

- **Text over the photo / bundle Tesseract? (deferred, to discuss).** Detecting text over the photo is not officially supported for now (a best-effort warning). Caption OCR uses the OS engine (Windows OCR, Apple Vision) and works without Tesseract. Text printed over the photo (case D: date-stamp verification, general printed text) is Tesseract-only, so without it a coloured stamp is flagged unverified and other printed text isn't looked for. Findings from the evaluation:
  - Windows: upstream 5.5.3 (`tesseract-ocr-w64-setup-5.5.3.20260724.exe`, GitHub digest sha256 `bee9e3434bd94fd65387d9be28cd467a41f61b1275383b55b0f59a1331270ae4`) extracts with 7-Zip; a stripped bundle (exe + 33 DLLs + tessdata_fast `eng.traineddata` + `tessdata/configs/tsv`) adds ~8 MB to the installer, ~26 MB installed, ~0.5 s per photo for the case D scan. No test regressions. Licences: Apache-2.0 plus GPL-2+ (libjbig) and LGPL DLLs, fine for a separate executable but they need notices and source pointers in THIRD_PARTY_NOTICES.md. The spec already picks up `vendor/tesseract`; fetch_vendor.py, vendor.lock.json and build.ps1 would need a Tesseract step.
  - macOS: bundling means Homebrew dylibs per architecture, rewritten load paths, codesigning and notarization. Porting `detect_text_over_photo` to Vision is likely cheaper.
- **Pin the Google Fonts commit** in `scripts/fetch_fonts.py` (`TBD-pin`). Only needed to refresh the committed fonts.
- **Spec wording to update** to match deliberate decisions:
  - "Unsaved edits prompt before navigating away" → edits autosave as drafts; leaving a photo shows a notice, and Close all asks first.
  - The marker aims for 3 copies across the band, not "at least 4".
- **Not done:** single saves and pre-flight run in server threads, not a worker process (spec: "long operations run in a worker process"). Batch saves do use worker processes.
- **Performance:** the hidden-marker search adds about 2–3 s to the background caption check on large unmarked photos. Preview isn't blocked, but Save waits for that check.
- **Network shares in the fallback browser:** UNC paths are browsable only inside a mapped drive, a dialog-picked folder or the last folder. Arbitrary typed `\\server\share` paths stay blocked (NTLM-leak guard).
- **Unequal X/Y DPI:** layout sizes are correct per axis, but glyphs are drawn on square pixels, so text is slightly stretched on such files.

## Not verified in this environment

- **Desktop shell:** the pywebview native window (WebView2/WKWebView), native file dialogs, and drag-and-drop with full paths. All flows were tested in browser mode (`photoband serve`).
- **OS OCR engines:** the Apple Vision and Windows.Media.Ocr adapters are written against the pyobjc and winrt APIs but were never run.
- **Packaging:** the PyInstaller spec, Inno Setup script and macOS sign/notarize script are written but were never built or run. There are no app icons yet (`packaging/icon.ico` and `packaging/icon.icns` are optional in the spec file).
- **Windows- and macOS-only code paths:** job objects, `SetFileTime`, read-only attributes, xattr copying on macOS, and the file-lock retries.
- **Real Lightroom and digiKam round trips:** the remapped regions are only mathematically verified.

## Known limitations and decisions

- **Hidden marker:**
  - JPEG q70 + 50% resize survives only for photos ≥1600 px wide; 25% + q85 needs about 3000 px.
  - Side crops of more than a few pixels lose the marker, and detection takes over.
  - A faint grain remains after an extreme levels stretch.
  - The on-disk format is v2; v1 files still read.
- **Pre-flight speed:** it runs the full-resolution caption check on every photo. Next step: proxy-first detection and OCR only on band candidates.
- **Lightroom settings:** crop is switched off (`HasCrop=False`) and local masks are removed so Lightroom doesn't crop off or recolour the band. Lens-profile and perspective corrections are kept; they would warp the band if applied.
- **Premultiplied alpha:** a TIFF with associated alpha saved as PNG keeps premultiplied values, with a note in the save log.
- **Fonts:**
  - Special Elite is Apache 2.0, not OFL as the spec's table says. It is still fine to bundle.
  - System `.ttc` collections are not listed.
- **Physical scale mode without a usable DPI:** sizes are applied relative to the photo width, as if printed 6 in wide, with a warning.
- **In-app file browser:** it stays available in desktop mode. Granting access uses one-time pick ids, but anyone who holds the per-launch token can still browse.

## Suggested next steps

1. Build the Windows and macOS installers and run the desktop smoke test: native dialogs, drop, WebView2 fallback, Vision OCR.
2. Open real archive scans from your library and tune case C detection and the erase settings on them.
3. Make pre-flight proxy-first to reach the 200-photos-in-60-s target.
4. Add app icons, and optionally a toggle between the "Front row, L–R:" and "Front:" row-label styles in the template editor.
5. Wire the shared `captiontokens` package into photokin.
