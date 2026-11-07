"""The DRAFT terms (``docs/TERMS.md``). Their version is the sha256 of the text, so any edit needs re-acceptance."""

from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from pathlib import Path

FALLBACK = "# /compute terms — DRAFT\n\nThe terms file is missing from this install. Ask the server admin.\n"


def terms_path() -> Path:
    env = os.environ.get("COMPUTE_TERMS_PATH")
    if env:
        return Path(env)
    from compute.app.resources import terms_file   # works from source and inside PyInstaller builds
    return terms_file()


@lru_cache(maxsize=1)
def terms_text() -> str:
    path = terms_path()
    return path.read_text(encoding="utf-8") if path.is_file() else FALLBACK


def terms_version() -> str:
    return hashlib.sha256(terms_text().encode("utf-8")).hexdigest()


def current_terms() -> dict:
    return {"version": terms_version(), "text": terms_text()}
