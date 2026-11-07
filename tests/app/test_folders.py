from __future__ import annotations

import sys
from pathlib import Path

import pytest

from compute.app import folders
from compute.app.folders import FolderError, browse


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    for name in ("beta", "Alpha", "gamma/inner", ".git", "Desktop"):
        (tmp_path / name).mkdir(parents=True)
    (tmp_path / "notes.txt").write_text("not a folder")
    return tmp_path


def names(data: dict) -> list[str]:
    return [f["name"] for f in data["folders"]]


def test_lists_sub_folders_sorted_without_files_or_hidden(tree: Path):
    data = browse(str(tree), home=tree)
    assert names(data) == ["Alpha", "beta", "Desktop", "gamma"]
    assert data["folders"][0] == {"name": "Alpha", "path": str(tree.resolve() / "Alpha")}
    assert data["path"] == str(tree.resolve())
    assert data["truncated"] is False


def test_hidden_folders_on_request(tree: Path):
    assert ".git" in names(browse(str(tree), hidden=True, home=tree))


def test_empty_path_opens_home(tree: Path):
    assert browse("", home=tree)["path"] == str(tree.resolve())


def test_crumbs_and_parent_walk_up_to_the_root(tree: Path):
    inner = (tree / "gamma" / "inner").resolve()
    data = browse(str(inner), home=tree)
    assert data["parent"] == str(inner.parent)
    assert data["crumbs"][-1] == {"name": "inner", "path": str(inner)}
    assert data["crumbs"][0]["path"] == inner.anchor
    assert [c["path"] for c in data["crumbs"]] == [str(p) for p in reversed([inner, *inner.parents])]


def test_disk_root_has_no_parent(tree: Path):
    root = Path(tree.resolve().anchor)
    data = browse(str(root), home=tree)
    assert data["parent"] is None and data["name"] == root.anchor


def test_places_show_home_and_existing_usual_folders(tree: Path):
    places = browse(str(tree), home=tree)["places"]
    assert places[0] == {"name": "Home", "path": str(tree)}
    assert {"name": "Desktop", "path": str(tree / "Desktop")} in places
    assert all(p["name"] != "Documents" for p in places)  # only folders that exist


def test_relative_path_refused(tree: Path):
    with pytest.raises(FolderError) as e:
        browse("some/where", home=tree)
    assert e.value.status == 400


def test_missing_folder_and_file_are_404(tree: Path):
    for raw in (tree / "nope", tree / "notes.txt"):
        with pytest.raises(FolderError) as e:
            browse(str(raw), home=tree)
        assert e.value.status == 404


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_unreadable_folder_is_403(tree: Path):
    locked = tree / "locked"
    locked.mkdir(mode=0o000)
    try:
        with pytest.raises(FolderError) as e:
            browse(str(locked), home=tree)
        assert e.value.status == 403
    finally:
        locked.chmod(0o755)


def test_long_listings_are_truncated(tree: Path, monkeypatch):
    monkeypatch.setattr(folders, "MAX_FOLDERS", 2)
    data = browse(str(tree), home=tree)
    assert names(data) == ["Alpha", "beta"] and data["truncated"] is True


# --------------------------------------------------------------------- route


def test_folders_route(make_client, tree: Path):
    client, _ = make_client(lambda _r: None)
    r = client.get("/api/folders", params={"path": str(tree)})
    assert r.status_code == 200 and "Alpha" in names(r.json())
    r = client.get("/api/folders", params={"path": str(tree / "nope")})
    assert r.status_code == 404 and "not a folder" in r.json()["detail"]


def test_folders_route_is_local_only(make_client, tree: Path):
    client, _ = make_client(lambda _r: None)
    r = client.get("/api/folders", params={"path": str(tree)}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


# ------------------------------------------------------- system dialog (app window)


class FakeWindow:
    def __init__(self, picked):
        self.picked = picked
        self.calls: list[tuple] = []

    def create_file_dialog(self, kind, directory=""):
        self.calls.append((kind, directory))
        return self.picked


def test_system_chooser_returns_the_chosen_folder(tree: Path):
    import webview

    from compute.app.main import folder_chooser

    window = FakeWindow((str(tree / "Alpha"),))
    choose = folder_chooser(window)
    assert choose.__name__ == "choose_folder"  # exposed to the page as pywebview.api.choose_folder
    assert choose(str(tree)) == str(tree / "Alpha")
    folder = webview.FileDialog.FOLDER if hasattr(webview, "FileDialog") else webview.FOLDER_DIALOG
    assert window.calls == [(folder, str(tree))]


def test_system_chooser_cancel_and_missing_start(tree: Path):
    from compute.app.main import folder_chooser

    window = FakeWindow(None)
    assert folder_chooser(window)(str(tree / "nope")) is None
    assert window.calls[0][1] == ""  # a start folder that's gone: let the OS pick where to open
