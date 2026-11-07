from __future__ import annotations

import json

import httpx

from compute.common.discovery import LanServer
from app_fakes import sse


def capture(seen: list[httpx.Request], response: httpx.Response):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response
    return handler


# --------------------------------------------------------------------- proxy


def test_coord_proxy_adds_saved_token(make_client):
    seen: list[httpx.Request] = []
    client, ctl = make_client(capture(seen, httpx.Response(200, json={"balance": 5})))
    ctl.update_settings({"server_url": "http://10.0.0.2:8765", "session_token": "saved-tok"})
    r = client.get("/api/coord/wallet/transactions?limit=5")
    assert r.status_code == 200 and r.json() == {"balance": 5}
    assert str(seen[0].url) == "http://10.0.0.2:8765/wallet/transactions?limit=5"
    assert seen[0].headers["authorization"] == "Bearer saved-tok"


def test_coord_proxy_browser_auth_wins_and_body_passes(make_client):
    seen: list[httpx.Request] = []
    client, ctl = make_client(capture(seen, httpx.Response(201, json={"ok": True})))
    ctl.update_settings({"server_url": "http://10.0.0.2:8765", "session_token": "saved-tok"})
    r = client.patch("/api/coord/auth/me", json={"name": "Ada"}, headers={"Authorization": "Bearer mine"})
    assert r.status_code == 201
    assert seen[0].method == "PATCH"
    assert seen[0].headers["authorization"] == "Bearer mine"
    assert json.loads(seen[0].content) == {"name": "Ada"}


def test_coord_proxy_passes_set_cookie_and_errors(make_client):
    upstream = httpx.Response(403, json={"detail": "banned"},
                              headers=[("set-cookie", "compute_session=abc; HttpOnly")])
    client, ctl = make_client(lambda _r: upstream)
    ctl.update_settings({"server_url": "http://10.0.0.2:8765"})
    r = client.post("/api/coord/auth/login", json={"email": "a@b.c", "password": "x"})
    assert r.status_code == 403
    assert r.json() == {"detail": "banned"}
    assert "compute_session=abc" in r.headers["set-cookie"]


def test_coord_proxy_without_server(make_client):
    client, _ = make_client(lambda _r: httpx.Response(200))
    r = client.get("/api/coord/wallet")
    assert r.status_code == 503


def test_coord_proxy_rejects_traversal(make_client):
    client, ctl = make_client(lambda _r: httpx.Response(200))
    ctl.update_settings({"server_url": "http://10.0.0.2:8765"})
    assert client.get("/api/coord/a/../../etc").status_code == 404


def test_cross_origin_requests_refused(make_client):
    client, _ = make_client(lambda _r: httpx.Response(200))
    r = client.post("/api/server/stop", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.get("/api/settings", headers={"Host": "evil.example:8766"}).status_code == 403
    assert client.get("/api/settings", headers={"Origin": "http://127.0.0.1:8766"}).status_code == 200


# ---------------------------------------------------------------------- chat


def test_chat_sse_passthrough_with_queued(make_client):
    body = sse(
        {"queued": {"queue_id": "q1", "position": 2, "eta_s": 30}},
        {"queued": {"queue_id": "q1", "position": 1, "eta_s": 10}},
        {"object": "chat.completion.chunk", "choices": [{"delta": {"content": "Hi"}}]},
        {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1},
         "network": {"job_id": "j", "flops": 1.5e12, "members": []}},
        "[DONE]",
    )
    seen: list[httpx.Request] = []
    client, ctl = make_client(capture(seen, httpx.Response(
        200, content=body, headers={"content-type": "text/event-stream"})))
    ctl.update_settings({"server_url": "http://10.0.0.2:8765", "session_token": "tok"})
    r = client.post("/api/chat", json={"model": "m", "messages": [], "stream": False, "session_id": "s1"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.content == body
    sent = json.loads(seen[0].content)
    assert sent["stream"] is True and sent["session_id"] == "s1"
    assert seen[0].url.path == "/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer tok"


def test_chat_error_before_stream(make_client):
    client, ctl = make_client(lambda _r: httpx.Response(402, json={"detail": "insufficient FLOPs"}))
    ctl.update_settings({"server_url": "http://10.0.0.2:8765"})
    r = client.post("/api/chat", json={"model": "m", "messages": []})
    assert r.status_code == 402
    assert r.json()["detail"] == "insufficient FLOPs"


# ---------------------------------------------------------- settings/status


def test_settings_endpoints(make_client):
    client, _ = make_client(lambda _r: httpx.Response(200))
    r = client.post("/api/settings", json={"memory_percent": 150, "models_dirs": ["/x"]})
    assert r.json()["memory_percent"] == 100
    assert client.get("/api/settings").json()["models_dirs"] == ["/x"]
    status = client.get("/api/status").json()
    assert set(status) >= {"server", "node", "settings"}


def test_status_joins_this_networks_pool(make_client, make_controller):
    pool = LanServer("http://10.0.0.3:8765", "compute-pool", 100.0)
    client, _ = make_client(lambda _r: httpx.Response(200), make_controller(lan=[pool]))
    status = client.get("/api/status").json()
    assert status["settings"]["server_url"] == pool.url
    assert status["server"]["auto_join"] is True
    assert client.post("/api/discover").json() == {"url": pool.url}


def test_typed_address_stays_across_polls(make_client, make_controller):
    pool = LanServer("http://10.0.0.3:8765", "compute-pool", 100.0)
    client, _ = make_client(lambda _r: httpx.Response(200), make_controller(lan=[pool]))
    client.post("/api/settings", json={"server_url": "203.0.113.7:8765", "auto_join": False})
    for _ in range(2):
        assert client.get("/api/status").json()["settings"]["server_url"] == "http://203.0.113.7:8765"


def test_server_start_error_is_400(make_client):
    client, _ = make_client(lambda _r: httpx.Response(200))
    r = client.post("/api/server/start", json={"email": "", "password": ""})
    assert r.status_code == 400
    assert "admin email" in r.json()["detail"]


def test_node_start_without_server_is_400(make_client):
    client, _ = make_client(lambda _r: httpx.Response(200))
    r = client.post("/api/node/start")
    assert r.status_code == 400


def test_index_served(make_client):
    client, _ = make_client(lambda _r: httpx.Response(200))
    r = client.get("/")
    assert r.status_code == 200 and "/compute" in r.text
    assert client.get("/static/api.js").status_code == 200
