# Changelog

All notable changes to Photoband. The version lives in one place: `photoband/__init__.py`.

## Unreleased

### Estimated dates
- The date editor has **Day · Month · Year** and an **Estimated** switch: a best-guess day (around
  Thanksgiving: November 23 → "c. November 23, 1944"), month (the summer of 1944: July →
  "c. July 1944") or year ("c. 1925"). Written with photokin's patterns (`Y!M!D~`, `Y!M~`, `Y~`).
  An estimated day never keeps a camera's time of day.
- Every date pattern in photokin's spec (`Y[!?~@](M[!?~@])?(D[!?~@])?`) is read: a guessed
  month or day is printed with "c." instead of being dropped (a year-only format still prints the
  certain year without it); a guessed year keeps a known month and day ("c. June 14, 1944"); an
  unknown year with a known month and day prints "June 14" (a birthday). The date editor shows the
  dates it can't hold (an unknown year) as words.
- `{keywords}` leaves out every keyword photokin takes for its date marker: any keyword starting
  "DATE:", in any case, including a note such as "Date: ask Ann" (kept in the file, shown as a
  marker chip, `markers=show` prints it). Editing the date replaces only well-formed markers
  ("DATE: Y!M~"), never such a note; a guessed year with a known month and day ("Y~M!D!") keeps
  its pattern when only the values are edited.

## 1.1.0 - 2026-10-05

### Edit the photo's details and faces
- The Metadata tab is now a form: title, description, notes, date, photographer, place (location,
  city, state, country) and keywords (as chips) can be edited. Captions follow the edits as you type.
  Edits are saved into the photo with the next save (a copy or an overwrite), or into the original
  on their own with **Save to original** (only the metadata is rewritten: the image data is checked
  to be byte-for-byte the same, the file is backed up first when Backup is on and it has no backup
  yet, and every old value is written to the save log). A saved copy keeps the edits waiting for the
  original. Undo covers every edit.
- The date editor knows partial dates: a day, a month, a year, or "about" a year. It writes them as
  photokin does (a filled-in DateTimeOriginal at midnight, XMP DateCreated with the known part, and a
  "DATE: Y!M!" keyword), and reads photokin's dates back the same way.
- Values are kept in step across the standards a file already uses (XMP, IPTC, EXIF, Windows tags);
  clearing a field removes it everywhere captions read it from. IPTC is never created, and text IPTC
  can't hold is kept in XMP only.
- Face tags can be edited on the photo, like in a photo organiser: **Faces** below the photo shows
  them; click a face to select it, drag it or its handles to move or resize, click its label (or
  press Enter) to name it with suggestions, Delete to remove it. **Add face** (N) draws a new one.
  Lightroom's region frames on rotated photos are respected; when a file lists its people as
  keywords (Lightroom does), renames follow there too. The Faces setting is remembered.
- People in the Metadata tab are listed in rows as `{names:rows}` prints them, with a per-photo
  row count (Auto, 1-4), saved with the caption.

### Rows of people
- `{names:rows}` no longer splits one row of standing people of different heights into several rows:
  rows break at gaps that are large for that photo (a crouching person in front still gets their own row).

### Captions from photokin
- New `{notes}` field: the photo's notes from EXIF UserComment (where photokin writes its
  analysis) or the IPTC/XMP Instructions field. Shown in the Details panel and the Insert menu.
- `{keywords}` leaves out photokin's marker keywords ("DATE: Y!M~", "<model> Analyzed", "back",
  "negative"); `{keywords|markers=show}` keeps them.
- `{date}` follows photokin's "DATE: Y!M~" certainty keyword: guessed parts of the date are left
  out ("Y!M~" prints only the year) and a guessed year prints as "c. 1925" (`circa=` changes the
  prefix, `certainty=ignore` prints the stored date). It applies only to the DateTimeOriginal
  photokin wrote: a date with a clock time (a camera's own) is never cut down.
- `{notes}` prints at most 200 characters (cut at a word) unless `max=` says otherwise; `max=0`: no limit.

## 1.0.1 - 2026-10-03

### Fixes
- Face tags from Lightroom on rotated photos (EXIF orientation 5-8) are placed on the faces again: the overlay, the left-to-right name order, `{names:rows}` and the regions in saved copies were all using a swapped frame.
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

### Interface
- Right-click a photo (or press the Menu key / Shift+F10) to show it in File Explorer / Finder or copy its path.
- The existing-caption banner says in plain words what was found and what saving will do.

## 1.0.0 - 2026-10-02

First release (see docs/STATUS.md for the acceptance checklist and known limitations).

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

