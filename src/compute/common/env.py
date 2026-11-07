"""Load ``.env`` files into ``os.environ`` so secrets and per-install settings stay out of git.

Searched in order (the first file to set a key wins, and real environment variables always win):

1. ``$COMPUTE_ENV_FILE`` if set
2. ``.env`` in the current working directory (running from a source checkout)
3. ``.env`` in the repo root (source checkout started from elsewhere)
4. ``.env`` in the app's data folder (``~/.compute`` / ``%LOCALAPPDATA%\\compute``), for the packaged app

Format: ``KEY=value`` lines; blank lines and ``#`` comments are ignored; an optional ``export``
prefix and matching single/double quotes around the value are stripped.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

_REPO = Path(__file__).resolve().parents[3]


def parse_line(line: str) -> Optional[tuple[str, str]]:
    text = line.strip()
    if not text or text.startswith("#") or "=" not in text:
        return None
    if text.startswith("export "):
        text = text[len("export "):].lstrip()
    key, value = text.split("=", 1)
    key, value = key.strip(), value.strip()
    if not key:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return key, value[1:-1]
    return key, value.split(" #", 1)[0].rstrip()


def parse(text: str) -> dict[str, str]:
    pairs = (parse_line(line) for line in text.splitlines())
    return dict(p for p in pairs if p is not None)


def candidate_files() -> list[Path]:
    from compute.common.config import default_home

    explicit = os.environ.get("COMPUTE_ENV_FILE")
    paths = [Path(explicit).expanduser()] if explicit else []
    paths += [Path.cwd() / ".env", _REPO / ".env", default_home() / ".env"]
    return list(dict.fromkeys(p.resolve() for p in paths))


def load_env(files: Optional[Iterable[Path]] = None) -> list[Path]:
    """Apply every existing file without overriding variables already set. Returns the files loaded."""
    loaded = []
    for path in candidate_files() if files is None else files:
        if not path.is_file():
            continue
        for key, value in parse(path.read_text(encoding="utf-8")).items():
            os.environ.setdefault(key, value)
        loaded.append(path)
    return loaded
