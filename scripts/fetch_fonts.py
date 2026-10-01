#!/usr/bin/env python3
"""MAINTAINER TOOL. fonts/ is committed, so developers and builds never need to run this.

Download the bundled font families from the google/fonts repository and verify
that each family's METADATA.pb lists the script subsets the spec promises.

    python scripts/fetch_fonts.py                 # the pinned google/fonts commit
    python scripts/fetch_fonts.py --ref <sha>     # another commit (then update GOOGLE_FONTS_COMMIT)

Fails (exit 1) if a listed subset is missing, so the font table cannot drift
from what ships. Writes fonts/<id>/*.ttf, fonts/<id>/METADATA.pb, the license
file, and fonts/manifest.json.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "fonts")
# Pin google/fonts to a commit so re-running this reproduces the committed fonts/.
# TODO(maintainer): set this to the commit the committed fonts/ came from (or a newer one you
# have checked). Until then the script refuses to run without an explicit --ref.
GOOGLE_FONTS_COMMIT = "TBD-pin"
RAW_TEMPLATE = "https://raw.githubusercontent.com/google/fonts/{ref}"
RAW = RAW_TEMPLATE.format(ref=GOOGLE_FONTS_COMMIT)

# id, family, google dir, role, required subsets, fallback-only
FAMILIES = [
    ("eb-garamond", "EB Garamond", "ebgaramond", "Classic serif", ["latin-ext", "greek", "cyrillic"], False),
    ("source-serif-4", "Source Serif 4", "sourceserif4", "Readable serif", ["latin-ext", "greek", "cyrillic"], False),
    ("literata", "Literata", "literata", "Book serif", ["latin-ext", "greek", "cyrillic"], False),
    ("playfair-display", "Playfair Display", "playfairdisplay", "Display serif", ["latin-ext", "cyrillic"], False),
    ("inter", "Inter", "inter", "Clean sans", ["latin-ext", "greek", "cyrillic"], False),
    ("source-sans-3", "Source Sans 3", "sourcesans3", "Humanist sans", ["latin-ext", "greek", "cyrillic"], False),
    ("special-elite", "Special Elite", "specialelite", "Typewriter", ["latin"], False),
    ("courier-prime", "Courier Prime", "courierprime", "Typewriter, clean", ["latin-ext"], False),
    ("caveat", "Caveat", "caveat", "Handwriting, casual", ["latin-ext", "cyrillic"], False),
    ("patrick-hand", "Patrick Hand", "patrickhand", "Handwriting, neat print", ["latin-ext"], False),
    ("kalam", "Kalam", "kalam", "Handwriting, pen", ["latin-ext"], False),
    ("great-vibes", "Great Vibes", "greatvibes", "Formal script", ["latin-ext", "cyrillic"], False),
    ("noto-serif", "Noto Serif", "notoserif", "Fallback serif", ["latin-ext", "greek", "cyrillic"], True),
    ("noto-sans", "Noto Sans", "notosans", "Fallback sans", ["latin-ext", "greek", "cyrillic"], True),
]
LICENSE_DIRS = ["ofl", "apache", "ufl"]


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()


def main(argv=None) -> int:
    global RAW
    ap = argparse.ArgumentParser(description="Re-download fonts/ from google/fonts (maintainers only).")
    ap.add_argument("--ref", default=GOOGLE_FONTS_COMMIT,
                    help="google/fonts commit SHA (default: the pinned GOOGLE_FONTS_COMMIT)")
    args = ap.parse_args(argv)
    if args.ref.startswith("TBD"):
        print("REFUSING: GOOGLE_FONTS_COMMIT is not pinned yet. Pass --ref <commit sha> (or --ref main\n"
              "if you really want whatever is current) and then pin that SHA in this file.", file=sys.stderr)
        return 2
    RAW = RAW_TEMPLATE.format(ref=args.ref)
    print(f"google/fonts @ {args.ref}")
    os.makedirs(OUT, exist_ok=True)
    manifest = []
    errors = []
    for fid, family, gdir, role, need, fallback in FAMILIES:
        meta = None
        for lic in LICENSE_DIRS:
            try:
                meta = fetch(f"{RAW}/{lic}/{gdir}/METADATA.pb").decode("utf-8")
                break
            except Exception:
                continue
        if meta is None:
            errors.append(f"{family}: METADATA.pb not found")
            continue
        subsets = re.findall(r'^subsets:\s*"([^"]+)"', meta, re.M)
        missing = [s for s in need if s not in subsets]
        if missing:
            errors.append(f"{family}: missing subsets {missing} (has {subsets})")
        license_name = re.search(r'^license:\s*"([^"]+)"', meta, re.M).group(1)
        files = re.findall(r'filename:\s*"([^"]+\.ttf)"', meta)
        d = os.path.join(OUT, fid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "METADATA.pb"), "w", encoding="utf-8") as fh:
            fh.write(meta)
        for fn in files:
            p = os.path.join(d, fn)
            if not os.path.exists(p):
                print("download", family, fn, flush=True)
                with open(p, "wb") as fh:
                    fh.write(fetch(f"{RAW}/{lic}/{gdir}/{urllib.request.quote(fn)}"))
        for lf in ("OFL.txt", "LICENSE.txt"):
            try:
                data = fetch(f"{RAW}/{lic}/{gdir}/{lf}")
                with open(os.path.join(d, lf), "wb") as fh:
                    fh.write(data)
                break
            except Exception:
                continue
        manifest.append({"id": fid, "family": family, "role": role, "license": license_name,
                         "subsets": subsets, "required": need, "fallback": fallback, "files": files})
    with open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    if errors:
        print("FONT CHECK FAILED:\n  " + "\n  ".join(errors), file=sys.stderr)
        return 1
    print(f"OK: {len(manifest)} families")
    return 0


if __name__ == "__main__":
    sys.exit(main())
