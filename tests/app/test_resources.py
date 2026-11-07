from __future__ import annotations

import sys

from compute.app import resources


def test_source_paths_exist():
    assert (resources.static_dir() / "index.html").is_file()
    assert (resources.admin_dir() / "admin.html").is_file()


def test_frozen_paths_use_meipass(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert resources.static_dir() == tmp_path / "compute" / "app" / "static"
    assert resources.admin_dir() == tmp_path / "compute" / "server" / "admin"
    assert resources.terms_file() == tmp_path / "docs" / "TERMS.md"
