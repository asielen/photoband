# Third-party notices

Photoband's installers and app bundles include the third-party software below. The license
texts ship with each component: the fonts carry theirs in `fonts/<family>/`, the Python
packages in their `*.dist-info` folders inside the app bundle, and ExifTool in its own folder.

Photoband itself is released under the MIT License (see `LICENSE`). The components below keep their own licenses.

## Bundled programs

| Component | Used for | License |
|---|---|---|
| [ExifTool](https://exiftool.org) by Phil Harvey (`vendor/exiftool`) | reading and writing photo metadata | Same terms as Perl: the Artistic License or the GNU GPL, at your choice |
| Perl, Windows builds only (`exiftool_files/` in the stand-alone ExifTool package) | runs ExifTool | Artistic License or GNU GPL |
| [Tesseract OCR](https://github.com/tesseract-ocr/tesseract), only when `vendor/tesseract` is present at build time (not bundled by default) | OCR fallback | Apache License 2.0 |
| Microsoft Edge WebView2 Runtime bootstrapper (Windows installer only; downloads and installs the runtime when it is missing) | the app window | Microsoft Software License Terms |

## Fonts (`fonts/`)

From the [google/fonts](https://github.com/google/fonts) repository.

| Family | License |
|---|---|
| EB Garamond, Source Serif 4, Literata, Playfair Display, Inter, Source Sans 3, Courier Prime, Caveat, Patrick Hand, Kalam, Great Vibes, Noto Serif, Noto Sans | SIL Open Font License 1.1 |
| Special Elite | Apache License 2.0 |

## Python packages (in the app bundle)

| Package | License |
|---|---|
| CPython (the interpreter, via PyInstaller) | PSF License |
| PyInstaller bootloader | GPL 2.0 with the bootloader exception (allows distribution of the bundled app) |
| FastAPI, Starlette, Pydantic, pydantic-core, uvicorn, h11, anyio, python-multipart, click | MIT / BSD |
| NumPy | BSD-3-Clause (the bundled OpenBLAS: BSD-3-Clause) |
| Pillow | MIT-CMU (HPND) |
| tifffile, imagecodecs | BSD-3-Clause (imagecodecs bundles zlib, zstd, liblzma, libjpeg-turbo and other codec libraries under their own permissive licenses, listed in its `licenses/` folder) |
| opencv-python-headless | Apache License 2.0 (OpenCV), MIT (packaging); bundles third-party libraries under their own licenses (see its `LICENSE-3RD-PARTY.txt`) |
| reedsolo | Unlicense (public domain) |
| fontTools | MIT |
| psutil | BSD-3-Clause |
| pywebview | BSD-3-Clause |
| pythonnet, clr-loader (Windows) | MIT |
| pyobjc (macOS) | MIT |
| winrt-* (Windows) | MIT |
| captiontokens | part of this project |

## JavaScript (compiled into `ui/dist`)

| Package | License |
|---|---|
| Svelte (runtime) | MIT |

Build-only tools (Vite, TypeScript, svelte-check, Vitest) are not shipped.

To regenerate the Python list for an exact build, run inside the build environment:
`python -m pip install pip-licenses && pip-licenses --from=mixed --with-urls`.
