from __future__ import annotations

from pathlib import Path

import pytest

from compute.app.code import workspace as wsmod
from compute.app.code.workspace import Workspace, WorkspaceError


@pytest.fixture
def ws(tmp_path: Path) -> Workspace:
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def add(a, b):\n    return a - b\n")
    (root / "README.md").write_text("# Demo\n")
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("secret = 1\n")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n")
    return Workspace(root.resolve())


# ------------------------------------------------------------------ root


def test_validate_root_accepts_a_project_folder(ws: Workspace):
    assert wsmod.validate_root(str(ws.root)) == ws.root


@pytest.mark.parametrize("raw, message", [
    ("", "Choose a project folder"),
    ("relative/path", "full path"),
    ("/", "whole disk"),
    ("~", "home folder"),
])
def test_validate_root_refusals(raw: str, message: str):
    with pytest.raises(WorkspaceError, match=message):
        wsmod.validate_root(raw)


def test_validate_root_refuses_missing_and_file(tmp_path: Path):
    with pytest.raises(WorkspaceError, match="not a folder"):
        wsmod.validate_root(str(tmp_path / "nope"))
    (tmp_path / "f.txt").write_text("x")
    with pytest.raises(WorkspaceError, match="not a folder"):
        wsmod.validate_root(str(tmp_path / "f.txt"))


def test_validate_root_keeps_away_from_compute_home(tmp_path: Path):
    home = tmp_path / "home" / ".compute"
    (home / "code").mkdir(parents=True)
    for folder in (home, home / "code", tmp_path / "home"):
        with pytest.raises(WorkspaceError, match="own settings"):
            wsmod.validate_root(str(folder), forbidden=[home])


# ----------------------------------------------------------------- paths


@pytest.mark.parametrize("rel", ["../outside.txt", "/etc/passwd", "src/../../x", "~/.ssh/id_rsa"])
def test_paths_outside_the_folder_are_refused(ws: Workspace, rel: str):
    with pytest.raises(WorkspaceError, match="outside the project folder"):
        ws.resolve(rel)


def test_symlink_escape_is_refused(ws: Workspace, tmp_path: Path):
    secret = tmp_path / "secret.txt"
    secret.write_text("token")
    (ws.root / "link.txt").symlink_to(secret)
    with pytest.raises(WorkspaceError, match="outside"):
        wsmod.read_file(ws, "link.txt")


def test_absolute_path_inside_is_fine(ws: Workspace):
    assert ws.resolve(str(ws.root / "src" / "app.py")) == ws.root / "src" / "app.py"


# ------------------------------------------------------------------ read


def test_list_files_skips_heavy_folders(ws: Workspace):
    out = wsmod.list_files(ws)
    assert "src/" in out and "app.py" in out and "README.md" in out
    assert "node_modules/ (skipped)" in out and "index.js" not in out
    assert ".git/ (skipped)" in out and "config" not in out


def test_read_file_numbers_lines_and_windows(ws: Workspace):
    (ws.root / "long.txt").write_text("\n".join(f"line {i}" for i in range(1, 1001)))
    out = wsmod.read_file(ws, "long.txt")
    assert out.startswith("long.txt (lines 1-400 of 1000)")
    assert "    1  line 1" in out and "<start>401</start>" in out
    part = wsmod.read_file(ws, "long.txt", "990", "2000")
    assert "(lines 990-1000 of 1000)" in part and "line 1000" in part


def test_read_file_errors(ws: Workspace):
    with pytest.raises(WorkspaceError, match="does not exist"):
        wsmod.read_file(ws, "missing.py")
    with pytest.raises(WorkspaceError, match="is a folder"):
        wsmod.read_file(ws, "src")
    (ws.root / "img.bin").write_bytes(b"\x89PNG\0\0data")
    with pytest.raises(WorkspaceError, match="binary"):
        wsmod.read_file(ws, "img.bin")


def test_search_finds_lines_and_skips_secrets(ws: Workspace):
    (ws.root / ".env").write_text("API_KEY=return a\n")
    out = wsmod.search(ws, r"return a")
    assert "src/app.py:2: return a - b" in out
    assert ".env" not in out and "node_modules" not in out
    assert wsmod.search(ws, "nothing-here") == "No matches for 'nothing-here'."
    assert "src/app.py:1: def add(a, b):" in wsmod.search(ws, "add(a")   # invalid regex: searched literally


def test_sensitive_files():
    assert wsmod.is_sensitive(Path(".env")) and wsmod.is_sensitive(Path(".env.local"))
    assert wsmod.is_sensitive(Path("server.pem")) and wsmod.is_sensitive(Path("id_ed25519"))
    assert not wsmod.is_sensitive(Path(".env.example")) and not wsmod.is_sensitive(Path("id_utils.py"))


# ----------------------------------------------------------------- edits


def test_replace_previews_then_applies(ws: Workspace):
    edit = wsmod.plan_replace(ws, "src/app.py", "return a - b", "return a + b")
    assert "-    return a - b" in edit.diff and "+    return a + b" in edit.diff
    assert edit.summary == "Edited src/app.py (+1 -1)"
    assert "a - b" in (ws.root / "src" / "app.py").read_text()   # preview doesn't write
    assert wsmod.apply(edit) == "Edited src/app.py (+1 -1)."
    assert (ws.root / "src" / "app.py").read_text() == "def add(a, b):\n    return a + b\n"


def test_replace_needs_a_unique_exact_match(ws: Workspace):
    with pytest.raises(WorkspaceError, match="not found"):
        wsmod.plan_replace(ws, "src/app.py", "return a * b", "x")
    (ws.root / "dup.py").write_text("x = 1\nx = 1\n")
    with pytest.raises(WorkspaceError, match="appears 2 times"):
        wsmod.plan_replace(ws, "dup.py", "x = 1", "x = 2")
    with pytest.raises(WorkspaceError, match="use write_file"):
        wsmod.plan_replace(ws, "new.py", "a", "b")


def test_write_creates_folders_and_rewrites(ws: Workspace):
    edit = wsmod.plan_write(ws, "pkg/util/helpers.py", "VALUE = 1\n")
    assert edit.summary.startswith("Created pkg/util/helpers.py")
    wsmod.apply(edit)
    assert (ws.root / "pkg" / "util" / "helpers.py").read_text() == "VALUE = 1\n"
    again = wsmod.plan_write(ws, "pkg/util/helpers.py", "VALUE = 2\n")
    assert again.summary.startswith("Rewrote")


def test_apply_refuses_when_file_changed_after_preview(ws: Workspace):
    edit = wsmod.plan_replace(ws, "src/app.py", "return a - b", "return a + b")
    (ws.root / "src" / "app.py").write_text("def add(a, b):\n    return a - b  # touched\n")
    with pytest.raises(WorkspaceError, match="changed since"):
        wsmod.apply(edit)
    created = wsmod.plan_write(ws, "fresh.txt", "a")
    (ws.root / "fresh.txt").write_text("someone else")
    with pytest.raises(WorkspaceError, match="changed since"):
        wsmod.apply(created)


def test_delete_and_git_guard(ws: Workspace):
    edit = wsmod.plan_delete(ws, "README.md")
    assert edit.new_text is None and "-# Demo" in edit.diff
    wsmod.apply(edit)
    assert not (ws.root / "README.md").exists()
    with pytest.raises(WorkspaceError, match=".git"):
        wsmod.plan_write(ws, ".git/config", "evil")
    with pytest.raises(WorkspaceError, match="folder, not a file"):
        wsmod.plan_delete(ws, "src")


def test_delete_binary_file(ws: Workspace):
    (ws.root / "logo.png").write_bytes(b"\x89PNG\0\0\0")
    wsmod.apply(wsmod.plan_delete(ws, "logo.png"))
    assert not (ws.root / "logo.png").exists()
