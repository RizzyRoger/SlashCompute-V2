"""Shared fixtures for the main-server tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# uv's editable .pth can carry the macOS "hidden" flag, which Python 3.13 skips: keep ``src`` importable.
SRC = str(Path(__file__).resolve().parents[2] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from fastapi.testclient import TestClient  # noqa: E402

from compute.common.config import EngineConfig  # noqa: E402



@pytest.fixture(autouse=True)
def fast_env(monkeypatch):
    monkeypatch.setattr("compute.server.auth.ITERATIONS", 1)
    from srv_helpers import ADMIN

    monkeypatch.setenv("COMPUTE_ADMIN_EMAIL", ADMIN)
    monkeypatch.delenv("COMPUTE_STARTER_FLOPS", raising=False)


@pytest.fixture
def client(tmp_path):
    from inf_harness import fast_settings

    from compute.server.app import create_app

    app = create_app(EngineConfig(home=tmp_path / "home"), settings=fast_settings())
    with TestClient(app) as c:
        yield c

