"""The /api/code endpoints through the real shell, with the main server faked over MockTransport."""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app_fakes import Recorder, sse
from code_fakes import BUGGY_APP, FIX_ADD, tool
from compute.app.code import commands
from compute.app.controller import Controller
from compute.app.shell import create_shell
from conftest import SHELL_BASE


@pytest.fixture(autouse=True)
def plain_path(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(commands, "login_path", lambda: None)


class FakePool:
    """The main server: /sessions and a scripted /v1/chat/completions."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.chats: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"id": "srv-1", "model": "m", "max_flops": 1e15, "spent": 0})
        if request.url.path == "/v1/chat/completions":
            body = json.loads(request.content)
            body["_auth"] = request.headers.get("authorization")
            self.chats.append(body)
            text = (self.replies.pop(0) if self.replies else "Done.").split("</tool>")[0]
            return httpx.Response(200, content=sse({"choices": [{"delta": {"content": text}}]},
                                                   {"choices": [], "network": {"flops": 5e11}}, "[DONE]"),
                                  headers={"content-type": "text/event-stream"})
        return httpx.Response(404, json={"detail": "not found"})


@pytest.fixture
def project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("proj").resolve()
    (root / "app.py").write_text(BUGGY_APP)
    return root


def shell_for(tmp_path: Path, pool: FakePool) -> TestClient:
    ctl = Controller(home=tmp_path / "home", popen=Recorder(), run=Recorder(),
                     http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
                     discover_fn=lambda _t: None, lan_ip_fn=lambda: "10.0.0.5")
    ctl.update_settings({"server_url": "http://pool:8765", "session_token": "tok"})
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(pool))
    return TestClient(create_shell(controller=ctl, client=upstream, port=8766), base_url=SHELL_BASE)


def wait_for(client: TestClient, sid: str, check, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get(f"/api/code/sessions/{sid}").json()
        if check(snap):
            return snap
        time.sleep(0.02)
    raise AssertionError(f"timed out; last snapshot: {snap}")


def read_events(client: TestClient, sid: str, after: int = 0) -> list[dict]:
    with client.stream("GET", f"/api/code/sessions/{sid}/events?after={after}") as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        return [json.loads(line[5:]) for line in r.iter_lines() if line.startswith("data:")]


def test_manual_tab_end_to_end(tmp_path: Path, project: Path):
    pool = FakePool([FIX_ADD, "Fixed add()."])
    with shell_for(tmp_path, pool) as client:
        r = client.post("/api/code/sessions", json={"root": str(project), "model": "m", "mode": "manual",
                                                    "budget_pflops": 1})
        assert r.status_code == 200, r.text
        meta = r.json()
        sid = meta["id"]
        assert meta["mode"] == "manual" and meta["flop_session_id"] == "srv-1" and meta["title"] == project.name

        assert client.post(f"/api/code/sessions/{sid}/messages", json={"text": "fix add"}).status_code == 200
        busy = client.post(f"/api/code/sessions/{sid}/messages", json={"text": "again"})
        assert busy.status_code == 409

        snap = wait_for(client, sid, lambda s: s["pending"])
        pending = snap["pending"]
        assert snap["state"] == "waiting" and pending["tool"] == "edit_file" and "+    return a + b" in pending["diff"]
        assert client.post(f"/api/code/sessions/{sid}/approval", json={"id": "stale", "approve": True}).status_code == 409
        ok = client.post(f"/api/code/sessions/{sid}/approval", json={"id": pending["id"], "approve": True})
        assert ok.status_code == 200

        events = read_events(client, sid)
        assert events[-1] == {"done": True}
        kinds = [e["entry"]["kind"] for e in events if "entry" in e]
        assert kinds == ["notice", "user", "assistant", "tool", "assistant"]
        assert "a + b" in (project / "app.py").read_text()

        chat = pool.chats[0]
        assert chat["_auth"] == "Bearer tok" and chat["session_id"] == "srv-1" and chat["stop"] == ["</tool>"]
        final = client.get(f"/api/code/sessions/{sid}").json()
        assert final["state"] == "idle" and final["spent_flops"] == pytest.approx(1e12)

        # Catch-up reads only what's new.
        assert client.get(f"/api/code/sessions/{sid}?after=4").json()["entries"][0]["kind"] == "assistant"


def test_plan_mode_flow_and_mode_switches(tmp_path: Path, project: Path):
    pool = FakePool([tool("propose_plan", plan="1. Flip the sign"), FIX_ADD, "Implemented."])
    with shell_for(tmp_path, pool) as client:
        sid = client.post("/api/code/sessions", json={"root": str(project), "model": "m"}).json()["id"]
        client.post(f"/api/code/sessions/{sid}/messages", json={"text": "fix add"})
        snap = wait_for(client, sid, lambda s: s["state"] == "idle" and s["plan"])
        assert snap["mode"] == "plan" and snap["plan"] == "1. Flip the sign"

        assert client.post(f"/api/code/sessions/{sid}/plan", json={"action": "nope"}).status_code == 400
        r = client.post(f"/api/code/sessions/{sid}/plan", json={"action": "auto"})
        assert r.status_code == 200 and r.json()["mode"] == "auto"
        wait_for(client, sid, lambda s: s["state"] == "idle")
        assert "a + b" in (project / "app.py").read_text()
        assert client.post(f"/api/code/sessions/{sid}/plan", json={"action": "auto"}).status_code == 409

        r = client.patch(f"/api/code/sessions/{sid}", json={"mode": "manual", "title": "Calculator"})
        assert r.json()["mode"] == "manual" and r.json()["title"] == "Calculator"
        assert client.patch(f"/api/code/sessions/{sid}", json={"mode": "wild"}).status_code == 400
        entries = client.get(f"/api/code/sessions/{sid}").json()["entries"]
        assert [e["mode"] for e in entries if e["kind"] == "mode"] == ["auto", "manual"]


def test_tabs_list_stop_and_close(tmp_path: Path, project: Path):
    pool = FakePool([tool("run_command", command="sleep 30")])
    with shell_for(tmp_path, pool) as client:
        a = client.post("/api/code/sessions", json={"root": str(project), "model": "m", "mode": "auto"}).json()
        b = client.post("/api/code/sessions", json={"root": str(project), "model": "m", "title": "Second"}).json()
        listed = client.get("/api/code/sessions").json()["sessions"]
        assert [s["id"] for s in listed] == [a["id"], b["id"]] and listed[1]["title"] == "Second"

        client.post(f"/api/code/sessions/{a['id']}/messages", json={"text": "wait"})
        wait_for(client, a["id"], lambda s: s["activity"].startswith("running"))
        client.post(f"/api/code/sessions/{a['id']}/stop")
        snap = wait_for(client, a["id"], lambda s: s["state"] == "idle")
        assert snap["entries"][-1]["text"] == "Stopped."

        assert client.delete(f"/api/code/sessions/{a['id']}").json() == {"ok": True}
        assert [s["id"] for s in client.get("/api/code/sessions").json()["sessions"]] == [b["id"]]
        assert client.get(f"/api/code/sessions/{a['id']}").status_code == 404
        assert client.delete(f"/api/code/sessions/{a['id']}").status_code == 404


def test_open_tab_errors(tmp_path: Path, project: Path):
    with shell_for(tmp_path, FakePool([])) as client:
        for body, status in [
            ({"root": "", "model": "m"}, 400),
            ({"root": str(project / "missing"), "model": "m"}, 400),
            ({"root": str(tmp_path / "home"), "model": "m"}, 400),
            ({"root": str(project)}, 400),
        ]:
            r = client.post("/api/code/sessions", json=body)
            assert r.status_code == status and r.json()["detail"], body


def test_code_api_refuses_other_websites(tmp_path: Path, project: Path):
    with shell_for(tmp_path, FakePool([])) as client:
        r = client.post("/api/code/sessions", json={"root": str(project), "model": "m"},
                        headers={"origin": "https://evil.example"})
        assert r.status_code == 403
        assert client.get("/api/code/sessions", headers={"host": "evil.example:8766"}).status_code == 403


def test_run_commands_opt_in_is_saved_per_tab(tmp_path: Path, project: Path):
    with shell_for(tmp_path, FakePool([])) as client:
        meta = client.post("/api/code/sessions", json={"root": str(project), "model": "m", "mode": "auto"}).json()
        assert meta["run_commands"] is False
        sid = meta["id"]
        assert client.patch(f"/api/code/sessions/{sid}", json={"run_commands": True}).json()["run_commands"] is True
        snap = client.get(f"/api/code/sessions/{sid}").json()
        assert snap["run_commands"] is True
        assert snap["entries"][-1]["text"] == "Auto mode will run commands without asking in this tab."
        opted = client.post("/api/code/sessions", json={"root": str(project), "model": "m", "mode": "auto",
                                                         "run_commands": True}).json()
        assert opted["run_commands"] is True


def test_undo_endpoint(tmp_path: Path, project: Path):
    pool = FakePool([FIX_ADD, "Fixed."])
    with shell_for(tmp_path, pool) as client:
        sid = client.post("/api/code/sessions", json={"root": str(project), "model": "m", "mode": "auto"}).json()["id"]
        client.post(f"/api/code/sessions/{sid}/messages", json={"text": "fix add"})
        snap = wait_for(client, sid, lambda s: s["state"] == "idle" and s["checkpoints"])
        turn = snap["checkpoints"][0]["turn"]
        assert snap["checkpoints"][0]["files"] == ["app.py"] and "a + b" in (project / "app.py").read_text()

        assert client.post(f"/api/code/sessions/{sid}/revert", json={"turn": "x"}).status_code == 400
        r = client.post(f"/api/code/sessions/{sid}/revert", json={"turn": turn})
        assert r.status_code == 200 and r.json()["kind"] == "revert" and r.json()["files"] == ["app.py"]
        assert (project / "app.py").read_text() == BUGGY_APP
        assert client.post(f"/api/code/sessions/{sid}/revert", json={"turn": turn}).status_code == 409
        assert client.get(f"/api/code/sessions/{sid}").json()["checkpoints"][0]["reverted"] is True
