"""The folder a Code tab works in: safe paths, the file tools, and edits previewed before they land.

Every path the model names is resolved inside the root (symlinks included) or refused.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

READ_MAX_BYTES = 512 * 1024
READ_WINDOW_LINES = 400
LIST_DEPTH = 2
LIST_MAX_ENTRIES = 300
SEARCH_MAX_HITS = 80
SEARCH_LINE_CHARS = 200
WRITE_MAX_BYTES = 1024 * 1024
DIFF_MAX_LINES = 400
BINARY_SNIFF_BYTES = 8192

SKIP_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".tox", ".idea",
})
SKIP_FILES = frozenset({".DS_Store", "Thumbs.db"})
SENSITIVE_NAMES = frozenset({".netrc", ".npmrc", ".pypirc", ".git-credentials"})
SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks")
SENSITIVE_PREFIXES = ("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")
ENV_EXAMPLES = frozenset({".env.example", ".env.sample", ".env.template", ".env.dist"})


class WorkspaceError(Exception):
    """A tool call that can't run; the message goes back to the model (and the transcript)."""


# ------------------------------------------------------------------ the root


def validate_root(raw: str, forbidden: Iterable[Path] = ()) -> Path:
    """The folder a tab may work in: an existing directory, not the disk root, not the home folder,
    and not inside (or around) a forbidden folder such as the /compute home with its session token."""
    text = (raw or "").strip()
    if not text:
        raise WorkspaceError("Choose a project folder.")
    path = Path(os.path.expanduser(text))
    if not path.is_absolute():
        raise WorkspaceError("Enter the folder's full path, for example /Users/me/project.")
    root = path.resolve()
    if not root.is_dir():
        raise WorkspaceError(f"{root} is not a folder.")
    if root == Path(root.anchor):
        raise WorkspaceError("Pick a project folder, not the whole disk.")
    if root == Path.home().resolve():
        raise WorkspaceError("Pick a project folder, not your whole home folder.")
    for item in forbidden:
        other = Path(item).resolve()
        if root == other or other in root.parents or root in other.parents:
            raise WorkspaceError(f"{root} holds /compute's own settings; pick a different folder.")
    return root


@dataclass(frozen=True)
class Workspace:
    root: Path

    def resolve(self, rel: Optional[str]) -> Path:
        text = (rel or "").strip() or "."
        candidate = Path(os.path.expanduser(text))
        path = (candidate if candidate.is_absolute() else self.root / candidate).resolve()
        if path != self.root and self.root not in path.parents:
            raise WorkspaceError(f"{text} is outside the project folder.")
        return path

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() or "."


def in_git_dir(ws: Workspace, path: Path) -> bool:
    return ".git" in path.relative_to(ws.root).parts


def is_sensitive(path: Path) -> bool:
    """Files that usually hold secrets: reading or changing them always asks first."""
    name = path.name.lower()
    if name in ENV_EXAMPLES:
        return False
    return (name == ".env" or name.startswith(".env.") or name in SENSITIVE_NAMES
            or name.endswith(SENSITIVE_SUFFIXES) or name.startswith(SENSITIVE_PREFIXES))


def is_binary(data: bytes) -> bool:
    return b"\0" in data[:BINARY_SNIFF_BYTES]


def read_text(path: Path, limit: int = READ_MAX_BYTES) -> str:
    """UTF-8 text of a file, or a WorkspaceError the model can act on."""
    try:
        size = path.stat().st_size
    except OSError as e:
        raise WorkspaceError(f"Can't read {path.name}: {e.strerror or e}") from e
    if size > limit:
        raise WorkspaceError(f"{path.name} is too large to read ({size // 1024} KB; the limit is {limit // 1024} KB).")
    data = path.read_bytes()
    if is_binary(data):
        raise WorkspaceError(f"{path.name} is a binary file.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise WorkspaceError(f"{path.name} is not UTF-8 text.") from e


def file_hash(path: Path) -> Optional[str]:
    """Fingerprint of a file's bytes (None when it doesn't exist), to catch changes after a preview."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


# ------------------------------------------------------------- read tools


def walk(path: Path, depth: int) -> Iterator[tuple[Path, int]]:
    """(entry, level) pairs, folders first, sorted, without descending into SKIP_DIRS."""
    try:
        children = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError:
        return
    for child in children:
        if child.name in SKIP_FILES:
            continue
        yield child, 0
        if child.is_dir() and not child.is_symlink() and child.name not in SKIP_DIRS and depth > 1:
            yield from ((p, level + 1) for p, level in walk(child, depth - 1))


def list_files(ws: Workspace, rel: Optional[str] = ".", depth: int = LIST_DEPTH) -> str:
    base = ws.resolve(rel)
    if not base.is_dir():
        raise WorkspaceError(f"{ws.rel(base)} is not a folder.")
    lines = []
    for path, level in walk(base, depth):
        if len(lines) >= LIST_MAX_ENTRIES:
            lines.append("... (more entries; list a subfolder to see them)")
            break
        suffix = "/" if path.is_dir() else ""
        note = " (skipped)" if path.is_dir() and path.name in SKIP_DIRS else ""
        lines.append(f"{'  ' * level}{path.name}{suffix}{note}")
    header = f"{ws.rel(base)}/"
    return header + "\n" + ("\n".join(lines) if lines else "(empty folder)")


def to_int(value: object, default: Optional[int] = None) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def read_file(ws: Workspace, rel: Optional[str], start: object = None, end: object = None) -> str:
    path = ws.resolve(rel)
    if path.is_dir():
        raise WorkspaceError(f"{ws.rel(path)} is a folder; use list_files.")
    if not path.is_file():
        raise WorkspaceError(f"{ws.rel(path)} does not exist.")
    lines = read_text(path).splitlines()
    total = len(lines)
    first = max(1, to_int(start, 1) or 1)
    last = min(total, to_int(end, first + READ_WINDOW_LINES - 1) or total)
    last = min(last, first + READ_WINDOW_LINES - 1)
    if total == 0:
        return f"{ws.rel(path)} (empty file)"
    if first > total:
        raise WorkspaceError(f"{ws.rel(path)} has only {total} lines.")
    body = "\n".join(f"{n:>5}  {lines[n - 1]}" for n in range(first, last + 1))
    more = f"\n... (lines {last + 1}-{total} not shown; read again with <start>{last + 1}</start>)" if last < total else ""
    return f"{ws.rel(path)} (lines {first}-{last} of {total})\n{body}{more}"


def searchable_files(base: Path) -> Iterator[Path]:
    if base.is_file():
        yield base
        return
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            yield Path(dirpath) / name


def compile_pattern(pattern: str) -> re.Pattern:
    try:
        return re.compile(pattern)
    except re.error:
        return re.compile(re.escape(pattern))


def search(ws: Workspace, pattern: Optional[str], rel: Optional[str] = ".") -> str:
    if not (pattern or "").strip():
        raise WorkspaceError("search needs a <pattern>.")
    base = ws.resolve(rel)
    if not base.exists():
        raise WorkspaceError(f"{ws.rel(base)} does not exist.")
    regex = compile_pattern(pattern)
    hits = []
    for path in searchable_files(base):
        if is_sensitive(path) or path.is_symlink():
            continue
        try:
            text = read_text(path)
        except (WorkspaceError, OSError):
            continue
        for n, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                hits.append(f"{ws.rel(path)}:{n}: {line.strip()[:SEARCH_LINE_CHARS]}")
                if len(hits) >= SEARCH_MAX_HITS:
                    return "\n".join(hits) + f"\n... (stopped at {SEARCH_MAX_HITS} matches; narrow the search)"
    return "\n".join(hits) if hits else f"No matches for {pattern!r}."


# ------------------------------------------------------------ write tools


@dataclass(frozen=True)
class Edit:
    """A previewed change. ``new_text`` None means delete; ``old_hash`` None means the file is new."""

    path: Path
    rel: str
    old_hash: Optional[str]
    new_text: Optional[str]
    diff: str
    summary: str


def writable(ws: Workspace, rel: Optional[str]) -> Path:
    if not (rel or "").strip():
        raise WorkspaceError("This tool needs a <path>.")
    path = ws.resolve(rel)
    if path == ws.root or path.is_dir():
        raise WorkspaceError(f"{ws.rel(path)} is a folder, not a file.")
    if in_git_dir(ws, path):
        raise WorkspaceError("Files inside .git are never edited.")
    return path


def unified_diff(rel: str, old: str, new: str) -> str:
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(),
                                      fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm=""))
    if len(lines) > DIFF_MAX_LINES:
        lines = lines[:DIFF_MAX_LINES] + [f"... ({len(lines) - DIFF_MAX_LINES} more diff lines)"]
    return "\n".join(lines)


def count_changes(diff: str) -> tuple[int, int]:
    body = [l for l in diff.splitlines() if not l.startswith(("+++", "---"))]
    return sum(l.startswith("+") for l in body), sum(l.startswith("-") for l in body)


def make_edit(ws: Workspace, path: Path, old: Optional[str], new: Optional[str], verb: str) -> Edit:
    rel = ws.rel(path)
    diff = unified_diff(rel, old or "", new or "")
    added, removed = count_changes(diff)
    return Edit(path, rel, file_hash(path), new, diff, f"{verb} {rel} (+{added} -{removed})")


def plan_write(ws: Workspace, rel: Optional[str], content: Optional[str]) -> Edit:
    path = writable(ws, rel)
    text = content or ""
    if len(text.encode("utf-8")) > WRITE_MAX_BYTES:
        raise WorkspaceError(f"That file is too large to write (limit {WRITE_MAX_BYTES // 1024} KB).")
    old = read_text(path) if path.exists() else None
    return make_edit(ws, path, old, text, "Created" if old is None else "Rewrote")


def plan_replace(ws: Workspace, rel: Optional[str], old_text: Optional[str], new_text: Optional[str]) -> Edit:
    path = writable(ws, rel)
    if not path.is_file():
        raise WorkspaceError(f"{ws.rel(path)} does not exist; use write_file to create it.")
    if not old_text:
        raise WorkspaceError("edit_file needs the exact <old> text to replace.")
    current = read_text(path)
    count = current.count(old_text)
    if count == 0:
        raise WorkspaceError(f"The <old> text was not found in {ws.rel(path)}. Read the file again and copy "
                             "the exact lines, including indentation.")
    if count > 1:
        raise WorkspaceError(f"The <old> text appears {count} times in {ws.rel(path)}. Include more "
                             "surrounding lines so it matches exactly once.")
    return make_edit(ws, path, current, current.replace(old_text, new_text or "", 1), "Edited")


def plan_delete(ws: Workspace, rel: Optional[str]) -> Edit:
    path = writable(ws, rel)
    if not path.is_file():
        raise WorkspaceError(f"{ws.rel(path)} does not exist.")
    try:
        current = read_text(path)
    except WorkspaceError:
        current = ""   # binary or large: delete it all the same, without a line diff
    return make_edit(ws, path, current, None, "Deleted")


def apply(edit: Edit) -> str:
    """Write a previewed edit, unless the file changed since the preview."""
    if file_hash(edit.path) != edit.old_hash:
        raise WorkspaceError(f"{edit.rel} changed since this edit was prepared. Read it again and redo the edit.")
    if edit.new_text is None:
        edit.path.unlink()
    else:
        edit.path.parent.mkdir(parents=True, exist_ok=True)
        edit.path.write_text(edit.new_text, encoding="utf-8", newline="")
    return edit.summary + "."
