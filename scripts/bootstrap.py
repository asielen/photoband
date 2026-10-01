#!/usr/bin/env python3
"""One-command developer setup for Photoband.

    python3 scripts/bootstrap.py          # macOS / Linux
    py scripts\\bootstrap.py               # Windows

What it does (safe to re-run; finished steps are skipped):
  1. checks Python >= 3.11 and Node >= 22.12
  2. creates .venv and installs the locked dependencies (requirements/dev.lock) plus
     photoband and captiontokens in editable mode
  3. builds the UI (npm ci + npm run build in ui/) unless ui/dist is already up to date
  4. finds ExifTool (PATH, $PHOTOBAND_EXIFTOOL or vendor/exiftool) or fetches it
  5. with --e2e, installs Playwright's Chromium for the end-to-end tests
  6. prints how to run the app

Only the standard library is used, so this runs on a bare Python.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys

MIN_PY = (3, 11)
MIN_NODE = (22, 12)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "ui")
IS_WIN = sys.platform == "win32"


def say(msg: str) -> None:
    print(f"==> {msg}", flush=True)


def die(msg: str) -> None:
    print(f"\nERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def run(cmd: list[str], cwd: str = ROOT, check: bool = True) -> int:
    print("    $ " + " ".join(cmd), flush=True)
    try:
        r = subprocess.run(cmd, cwd=cwd)
    except FileNotFoundError as e:
        die(f"Could not run {cmd[0]}: {e}")
    if check and r.returncode:
        die(f"Command failed (exit {r.returncode}): {' '.join(cmd)}")
    return r.returncode


# ----------------------------------------------------------------------------- checks
def check_python() -> None:
    if sys.version_info < MIN_PY:
        hint = "py -3.12 scripts\\bootstrap.py" if IS_WIN else "python3.12 scripts/bootstrap.py"
        die(f"Photoband needs Python {MIN_PY[0]}.{MIN_PY[1]} or newer; this is {sys.version.split()[0]}.\n"
            f"Install a newer Python (https://www.python.org/downloads/) and run e.g.: {hint}")
    say(f"Python {sys.version.split()[0]} OK")


def parse_version(text: str) -> tuple[int, ...]:
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    return tuple(int(x or 0) for x in m.groups()) if m else ()


def node_status() -> tuple[str | None, str | None, str]:
    """(node, npm, problem) - problem is "" when Node is new enough."""
    node, npm = shutil.which("node"), shutil.which("npm")
    want = f"{MIN_NODE[0]}.{MIN_NODE[1]}"
    how = ("Install Node.js LTS from https://nodejs.org (or `nvm install`, which reads .nvmrc"
           + (", or `winget install OpenJS.NodeJS.LTS`" if IS_WIN else ", or `brew install node@22`") + ").")
    if not node or not npm:
        return node, npm, f"Node.js {want}+ with npm is needed to build the UI, but it was not found.\n{how}"
    out = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip()
    if parse_version(out)[:2] < MIN_NODE:
        return node, npm, f"Node.js {want}+ is needed to build the UI; found {out or 'unknown'}.\n{how}"
    return node, npm, ""


# ----------------------------------------------------------------------------- venv
def venv_python(venv: str) -> str:
    return os.path.join(venv, "Scripts", "python.exe") if IS_WIN else os.path.join(venv, "bin", "python")


def make_venv(venv: str) -> str:
    py = venv_python(venv)
    if os.path.exists(py):
        r = subprocess.run([py, "-c", "import sys; print(sys.version_info >= (3, 11))"],
                           capture_output=True, text=True)
        if r.stdout.strip() == "True":
            say(f"Using existing virtual environment {os.path.relpath(venv, ROOT)}")
            return py
        die(f"{venv} exists but its Python is broken or too old. Delete the folder and re-run.")
    say(f"Creating virtual environment {os.path.relpath(venv, ROOT)}")
    run([sys.executable, "-m", "venv", venv])
    return py


def install_python_deps(py: str, lock: str) -> None:
    say(f"Installing pinned dependencies from {os.path.relpath(lock, ROOT)}")
    run([py, "-m", "pip", "install", "--disable-pip-version-check", "-q", "--upgrade", "pip"])
    run([py, "-m", "pip", "install", "--disable-pip-version-check", "-r", lock])
    say("Installing photoband + captiontokens (editable)")
    run([py, "-m", "pip", "install", "--disable-pip-version-check", "--no-deps", "-e", ROOT])


# ----------------------------------------------------------------------------- UI
UI_INPUTS = ["src", "public", "index.html", "package.json", "package-lock.json", "vite.config.ts",
             "svelte.config.js", "tsconfig.json", "tsconfig.app.json", "tsconfig.node.json"]


def newest_mtime(paths: list[str]) -> float:
    newest = 0.0
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                for f in files:
                    newest = max(newest, os.path.getmtime(os.path.join(root, f)))
        elif os.path.exists(p):
            newest = max(newest, os.path.getmtime(p))
    return newest


def ui_up_to_date() -> bool:
    index = os.path.join(UI, "dist", "index.html")
    return os.path.exists(index) and os.path.getmtime(index) >= newest_mtime([os.path.join(UI, p) for p in UI_INPUTS])


def build_ui(npm: str, force: bool) -> None:
    marker = os.path.join(UI, "node_modules", ".package-lock.json")
    lock = os.path.join(UI, "package-lock.json")
    if force or not os.path.exists(marker) or os.path.getmtime(marker) < os.path.getmtime(lock):
        say("Installing UI dependencies (npm ci)")
        run([npm, "ci", "--no-audit", "--no-fund"], cwd=UI)
    if not force and ui_up_to_date():
        say("UI build is up to date (ui/dist)")
        return
    say("Building the UI (npm run build)")
    run([npm, "run", "build"], cwd=UI)


# ----------------------------------------------------------------------------- ExifTool
def find_exiftool() -> str | None:
    env = os.environ.get("PHOTOBAND_EXIFTOOL")
    if env and os.path.exists(env):
        return env
    vendored = os.path.join(ROOT, "vendor", "exiftool", "exiftool.exe" if IS_WIN else "exiftool")
    if os.path.exists(vendored):
        return vendored
    return shutil.which("exiftool")


def ensure_exiftool(py: str) -> bool:
    found = find_exiftool()
    if found:
        say(f"ExifTool found: {found}")
        return True
    say("ExifTool not found; fetching the pinned copy into vendor/exiftool")
    rc = run([py, os.path.join(ROOT, "scripts", "fetch_vendor.py")], check=False)
    if rc == 0 and find_exiftool():
        return True
    print("\nWARNING: ExifTool is still missing. Photoband runs, but cannot read or write metadata\n"
          "until you install it (see the messages above), e.g.:\n"
          "  macOS: brew install exiftool   Windows: winget install OliverBetz.ExifTool\n"
          "  Linux: sudo apt install libimage-exiftool-perl\n", flush=True)
    return False


# ----------------------------------------------------------------------------- main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Set up a Photoband development environment.")
    ap.add_argument("--venv", default=os.path.join(ROOT, ".venv"), help="virtual environment folder (.venv)")
    ap.add_argument("--no-dev", action="store_true", help="runtime dependencies only (requirements/app.lock)")
    ap.add_argument("--e2e", action="store_true", help="also install Playwright's Chromium for tests/e2e")
    ap.add_argument("--skip-ui", action="store_true", help="don't build the UI")
    ap.add_argument("--rebuild-ui", action="store_true", help="npm ci + build even if ui/dist looks current")
    ap.add_argument("--skip-exiftool", action="store_true", help="don't look for or fetch ExifTool")
    args = ap.parse_args(argv)

    check_python()
    node, npm, node_problem = (None, None, "") if args.skip_ui else node_status()
    if node_problem:
        if ui_up_to_date() and not args.rebuild_ui:
            print(f"\nWARNING: {node_problem}\nui/dist is already built, so continuing without rebuilding it.\n")
        else:
            die(node_problem)
    elif node:
        say(f"Node {subprocess.run([node, '--version'], capture_output=True, text=True).stdout.strip()} OK")

    venv = os.path.abspath(args.venv)
    py = make_venv(venv)
    lock = os.path.join(ROOT, "requirements", "app.lock" if args.no_dev else "dev.lock")
    install_python_deps(py, lock)

    if not args.skip_ui and npm and not node_problem:
        build_ui(npm, args.rebuild_ui)

    exiftool_ok = True if args.skip_exiftool else ensure_exiftool(py)

    if args.e2e:
        say("Installing Playwright Chromium (for tests/e2e)")
        run([py, "-m", "playwright", "install", "chromium"])

    rel = os.path.relpath(venv, ROOT)
    if IS_WIN:
        vpy, activate = f"{rel}\\Scripts\\python", f"{rel}\\Scripts\\activate"
    else:
        vpy, activate = f"{rel}/bin/python", f"source {rel}/bin/activate"
    print(f"""
Photoband is ready{'' if exiftool_ok else ' (ExifTool still missing, see above)'}.

Run it from the repository folder:
  {vpy} -m photoband                    # desktop window
  {vpy} -m photoband serve              # in your browser (prints the URL)

or activate the environment first ({activate}) and then just run `photoband`.
Tests: {vpy} -m pytest -q --ignore=tests/e2e
""", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
