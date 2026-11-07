from __future__ import annotations

import sys
from pathlib import Path

# uv's editable .pth can be skipped on macOS + Python 3.13 (hidden file flag): import from src/ directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from typing import Callable, Optional

import httpx
import pytest
from fastapi.testclient import TestClient

from compute.app.controller import Controller
from compute.app.shell import create_shell
from app_fakes import Recorder

SHELL_BASE = "http://127.0.0.1:8766"


def json_handler(routes: dict[str, Callable[[httpx.Request], httpx.Response]]):
    def handler(request: httpx.Request) -> httpx.Response:
        fn = routes.get(request.url.path)
        if fn is None:
            return httpx.Response(404, json={"detail": "not found"})
        return fn(request)
    return handler


@pytest.fixture
def make_controller(tmp_path: Path):
    def build(popen=None, run=None, routes=None, **kwargs) -> Controller:
        http = httpx.Client(transport=httpx.MockTransport(json_handler(routes or {})))
        return Controller(home=tmp_path, popen=popen or Recorder(), run=run or Recorder(), http=http,
                          discover_fn=lambda _t: None, lan_ip_fn=lambda: "10.0.0.5",
                          server_wait_s=0.3, node_wait_s=0.2, **kwargs)
    return build


@pytest.fixture
def make_client(make_controller):
    def build(handler, controller: Optional[Controller] = None) -> tuple[TestClient, Controller]:
        ctl = controller or make_controller()
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = TestClient(create_shell(controller=ctl, client=upstream, port=8766), base_url=SHELL_BASE)
        return client, ctl
    return build
