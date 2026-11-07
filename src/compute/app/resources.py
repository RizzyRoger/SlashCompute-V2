"""Locate bundled data files from source checkouts and PyInstaller builds alike.

PyInstaller unpacks data files under ``sys._MEIPASS`` with the same relative layout the spec gives
them (``compute/app/static``, ``compute/server/admin``, ``docs/TERMS.md``). From a source checkout
the same relative paths resolve against ``src/`` (package files) or the repo root (``docs/``).
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2]      # .../src
_REPO = _SRC.parent                              # repo root (docs/ lives here)


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def data_path(relative: str) -> Path:
    """Absolute path of a bundled data file, e.g. ``data_path("compute/app/static")``."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS")) / relative
    in_src = _SRC / relative
    return in_src if in_src.exists() else _REPO / relative


def static_dir() -> Path:
    return data_path("compute/app/static")


def admin_dir() -> Path:
    return data_path("compute/server/admin")


def terms_file() -> Path:
    return data_path("docs/TERMS.md")
