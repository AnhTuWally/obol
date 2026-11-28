#!/usr/bin/env python3
"""Launch the Obol GUI, creating/updating the local virtual environment if needed.

Usage:
    python launch_gui.py [extra args passed to `obol gui`]

Options:
    --reinstall   Force reinstalling dependencies into the venv.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import venv
from pathlib import Path

MIN_PYTHON = (3, 11)
ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
PYPROJECT = ROOT / "pyproject.toml"
STAMP = VENV_DIR / ".obol_install_stamp"


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def pyproject_hash() -> str:
    return hashlib.sha256(PYPROJECT.read_bytes()).hexdigest()


def ensure_venv(force_reinstall: bool) -> Path:
    py = venv_python()
    if not py.exists():
        print(f"[launch_gui] Creating virtual environment in {VENV_DIR} ...")
        venv.EnvBuilder(with_pip=True, clear=VENV_DIR.exists()).create(VENV_DIR)

    current = pyproject_hash()
    installed = STAMP.read_text().strip() if STAMP.exists() else ""
    if force_reinstall or installed != current:
        print("[launch_gui] Installing dependencies (pip install -e .) ...")
        subprocess.check_call([str(py), "-m", "pip", "install", "--upgrade", "pip"])
        subprocess.check_call([str(py), "-m", "pip", "install", "-e", str(ROOT)])
        STAMP.write_text(current)
    return py


def main() -> int:
    if sys.version_info < MIN_PYTHON:
        print(
            f"[launch_gui] Python {'.'.join(map(str, MIN_PYTHON))}+ required, "
            f"found {sys.version.split()[0]}."
        )
        return 1

    args = sys.argv[1:]
    force = "--reinstall" in args
    args = [a for a in args if a != "--reinstall"]

    try:
        py = ensure_venv(force)
    except subprocess.CalledProcessError as exc:
        print(f"[launch_gui] Environment setup failed: {exc}")
        return exc.returncode or 1

    return subprocess.call([str(py), "-m", "obol.cli.main", "gui", *args], cwd=ROOT)


if __name__ == "__main__":
    sys.exit(main())
