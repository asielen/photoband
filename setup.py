"""Build glue that pyproject.toml alone can't express.

* captiontokens (packages/captiontokens/captiontokens) ships inside the photoband distribution
  as a second top-level package. One ``pip install -e .`` or ``pip install .`` installs both;
  packages/captiontokens keeps its own pyproject.toml so photokin can still install it alone.
* A regular (non-editable) build copies the built UI (ui/dist) and the bundled fonts (fonts/)
  into the wheel as ``photoband/_data/ui/dist`` and ``photoband/_data/fonts``, so
  ``pip install .`` and ``pipx install .`` work outside the repo. Editable installs read them
  from the repo instead (see photoband/paths.py), so nothing is copied there.
"""
import os
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = os.path.dirname(os.path.abspath(__file__))


class BuildPyWithData(build_py):
    def run(self):
        super().run()
        if getattr(self, "editable_mode", False):
            return
        ui = os.path.join(ROOT, "ui", "dist")
        fonts = os.path.join(ROOT, "fonts")
        if not os.path.isfile(os.path.join(ui, "index.html")):
            raise SystemExit(
                "\nERROR: ui/dist is missing, so this install would have no user interface.\n"
                "Build the UI first:   cd ui && npm ci && npm run build\n"
                "(or run `python scripts/bootstrap.py`, which does it for you)\n")
        if not os.path.isfile(os.path.join(fonts, "manifest.json")):
            raise SystemExit("\nERROR: fonts/manifest.json is missing (fonts/ is part of the repository).\n")
        dst = os.path.join(self.build_lib, "photoband", "_data")
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(ui, os.path.join(dst, "ui", "dist"))
        shutil.copytree(fonts, os.path.join(dst, "fonts"))


setup(
    packages=["photoband", "captiontokens"],
    package_dir={"photoband": "photoband", "captiontokens": "packages/captiontokens/captiontokens"},
    cmdclass={"build_py": BuildPyWithData},
)
