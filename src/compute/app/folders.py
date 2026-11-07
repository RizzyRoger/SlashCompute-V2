"""Folder browsing for the UI's folder picker (``GET /api/folders``).

Lists the sub-folders of one folder on this computer: names and paths only, never file contents.
The shell only answers its own UI on 127.0.0.1 (see ``shell.local_only``), so nothing leaves this machine.
"""

from __future__ import annotations

import os
import stat
import string
import sys
from pathlib import Path
from typing import Any, Iterator, Optional

MAX_FOLDERS = 2000
PLACE_FOLDERS = ("Desktop", "Documents", "Downloads")


class FolderError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def entry(path: Path, name: Optional[str] = None) -> dict[str, str]:
    return {"name": name or path.name or path.anchor or str(path), "path": str(path)}


def drives() -> list[Path]:
    """Windows drive roots (``C:\\``...); none elsewhere."""
    if sys.platform != "win32":
        return []
    listdrives = getattr(os, "listdrives", None)  # Python 3.12+
    if listdrives:
        return [Path(d) for d in listdrives()]
    return [Path(f"{letter}:\\") for letter in string.ascii_uppercase[2:] if Path(f"{letter}:\\").exists()]


def places(home: Optional[Path] = None) -> list[dict[str, str]]:
    """Shortcuts for the picker's sidebar: home, its usual sub-folders, then the disk roots."""
    home = home or Path.home()
    found = [entry(home, "Home")]
    found += [entry(home / name, name) for name in PLACE_FOLDERS if (home / name).is_dir()]
    roots = drives() or [Path(home.anchor or "/")]
    return found + [entry(root) for root in roots]


def crumbs(path: Path) -> list[dict[str, str]]:
    """``/``, ``/Users``, ``/Users/me``... so each part of the path can be clicked."""
    return [entry(p) for p in reversed([path, *path.parents])]


def is_hidden(item: os.DirEntry) -> bool:
    """Dot-folders, plus folders the OS hides (macOS ``~/Library``, Windows ``AppData``)."""
    if item.name.startswith("."):
        return True
    try:
        info = item.stat()
    except OSError:
        return True
    flags = getattr(info, "st_flags", 0) & getattr(stat, "UF_HIDDEN", 0)
    attrs = getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0)
    return bool(flags or attrs)


def is_folder(item: os.DirEntry) -> bool:
    try:
        return item.is_dir()
    except OSError:
        return False


def sub_folders(path: Path, hidden: bool) -> Iterator[os.DirEntry]:
    with os.scandir(path) as items:
        for item in items:
            if is_folder(item) and (hidden or not is_hidden(item)):
                yield item


def denied_message(path: Path) -> str:
    """macOS asks once before an app opens Desktop, Documents, Downloads...; a "Don't Allow" lands here."""
    if sys.platform == "darwin":
        return (f"macOS didn't allow opening {path}. To allow it, turn on /compute in System Settings > "
                "Privacy & Security > Files and Folders.")
    return f"This computer doesn't allow opening {path}."


def resolve(raw: str, home: Path) -> Path:
    if not raw.strip():
        return home.resolve()
    path = Path(raw.strip()).expanduser()
    if not path.is_absolute():
        raise FolderError(400, f"{raw} is not a full path.")
    return path.resolve()


def browse(raw: str = "", hidden: bool = False, home: Optional[Path] = None) -> dict[str, Any]:
    """One folder's sub-folders, sorted by name, plus breadcrumbs and shortcuts for the picker."""
    home = home or Path.home()
    path = resolve(raw, home)
    if not path.is_dir():
        raise FolderError(404, f"{path} is not a folder (or no longer exists).")
    try:
        found = sorted(sub_folders(path, hidden), key=lambda item: item.name.casefold())
    except PermissionError as e:
        raise FolderError(403, denied_message(path)) from e
    except OSError as e:
        raise FolderError(400, f"Couldn't open {path}: {e.strerror or e}") from e
    parent = path.parent if path.parent != path else None
    return {
        "path": str(path),
        "name": entry(path)["name"],
        "parent": str(parent) if parent else None,
        "crumbs": crumbs(path),
        "folders": [entry(path / item.name) for item in found[:MAX_FOLDERS]],
        "truncated": len(found) > MAX_FOLDERS,
        "places": places(home),
    }
