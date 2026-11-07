"""Windowed PyInstaller builds on Windows start with no stdout/stderr; the CLI must not crash on them."""

from __future__ import annotations

import sys

from compute import cli


def test_missing_streams_are_redirected_to_a_log_file(tmp_path, monkeypatch):
    monkeypatch.setattr("compute.common.config.default_home", lambda: tmp_path)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    cli.ensure_stdio("app")

    assert sys.stdout is not None and sys.stderr is not None
    assert sys.stdout.isatty() is False            # what uvicorn's formatter calls
    print("hello from a windowed exe")
    assert "hello from a windowed exe" in (tmp_path / "logs" / "app-console.log").read_text()


def test_real_streams_are_left_alone(monkeypatch):
    out, err = sys.stdout, sys.stderr
    cli.ensure_stdio("app")
    assert sys.stdout is out and sys.stderr is err
