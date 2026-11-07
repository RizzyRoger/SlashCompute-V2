"""The real main server with a fake inference cluster: FLOPs move 1:1, admin is free, budgets, SSE."""

from __future__ import annotations

import json

import httpx
import pytest

from inf_harness import FakeNode, chat, fast_settings, start_harness
from compute.common.config import EngineConfig
from compute.engine.coordinator.layers import synthetic_layout
from compute.server.app import create_app
from compute.server.terms import terms_version
from srv_helpers import ADMIN, ADMIN_PW

GB = 10 ** 9
QWEN = "Qwen3.8-27B-UD-Q4_K_XL.gguf"


@pytest.fixture
async def pool(tmp_path):
    cfg = EngineConfig(home=tmp_path / "home")
    h = await start_harness(fast_settings(PIPELINE_IDLE_SECONDS=30),
                            app_factory=lambda: create_app(cfg, settings=fast_settings(PIPELINE_IDLE_SECONDS=30)),
                            tmp=str(tmp_path / "nodes"))
    h.add_model(synthetic_layout(QWEN, n_layers=64, total_bytes=int(17.56 * GB), head_bytes=1 * GB))
    yield h
    await h.stop()


def sv(h):
    return h.app.state.services


def account(h, email: str, credit: float = 1e17) -> tuple[str, str]:
    s = sv(h)
    user = s.auth.register(email, "password1", email.split("@")[0])
    s.auth.accept_terms(user, terms_version())
    if credit:
        s.ledger.post(user.id, "admin_credit", credit)
    return user.id, s.auth.issue(user)


def admin(h) -> tuple[str, str]:
    user = sv(h).auth.ensure_admin(ADMIN, ADMIN_PW)
    return user.id, sv(h).auth.issue(user)


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def two_nodes(h, head_token: str, worker_token: str) -> None:
    await h.add_node(FakeNode("head", 16, may_be_head=True, files=(QWEN,)), 0, session_token=head_token)
    await h.add_node(FakeNode("worker", 16, gen_score=1.2), 1, session_token=worker_token)


def earned(h, uid: str) -> float:
    return sv(h).ledger.kind_total(uid, "earn")


async def test_earning_is_one_to_one_with_spending_across_two_nodes(pool):
    h = pool
    host1, t1 = account(h, "host1@lan.test", credit=0)
    host2, t2 = account(h, "host2@lan.test", credit=0)
    chatter, tc = account(h, "chatter@lan.test")
    await two_nodes(h, t1, t2)
    r = await chat(h, QWEN, max_tokens=8, headers=auth(tc))
    assert r.status_code == 200, r.text
    flops = r.json()["network"]["flops"]
    assert flops > 0
    assert earned(h, host1) > 0 and earned(h, host2) > 0
    assert earned(h, host1) + earned(h, host2) == pytest.approx(flops)
    ledger = sv(h).ledger
    assert -ledger.kind_total(chatter, "spend") == pytest.approx(flops)
    assert ledger.balance(chatter) == pytest.approx(1e17 - flops)
    assert ledger.held(chatter) == 0
    async with httpx.AsyncClient(base_url=h.url) as c:
        live = (await c.get("/wallet/live", headers=auth(t1))).json()
        assert live["earned_total"] == pytest.approx(earned(h, host1)) and live["earning_flops_per_s"] > 0
        assert [n["name"] for n in live["nodes"]] == ["head"] and live["nodes"][0]["online"]
        board = (await c.get("/leaderboard")).json()
        assert {row["name"] for row in board} == {"host1", "host2"}
        assert (await c.get("/health")).json()["inference_nodes"] == 2


async def test_admin_is_never_charged_but_contributors_earn(pool):
    h = pool
    host, th = account(h, "host@lan.test", credit=0)
    admin_id, ta = admin(h)
    await two_nodes(h, th, th)
    r = await chat(h, QWEN, max_tokens=8, headers=auth(ta))
    assert r.status_code == 200, r.text
    assert earned(h, host) == pytest.approx(r.json()["network"]["flops"])
    assert sv(h).ledger.transactions(admin_id) == []
    async with httpx.AsyncClient(base_url=h.url) as c:
        wallet = (await c.get("/wallet", headers=auth(ta))).json()
    assert wallet["balance"] is None and wallet["unlimited"] is True


async def test_session_budget_caps_chat(pool):
    h = pool
    host, th = account(h, "host@lan.test", credit=0)
    _, tc = account(h, "chatter@lan.test")
    await two_nodes(h, th, th)
    async with httpx.AsyncClient(base_url=h.url, timeout=30) as c:
        tiny = (await c.post("/sessions", json={"model": QWEN, "max_flops": 1.0}, headers=auth(tc))).json()
        body = {"model": QWEN, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}
        r = await c.post("/v1/chat/completions", json={**body, "session_id": tiny["id"]}, headers=auth(tc))
        assert r.status_code == 402 and r.json()["detail"] == "budget_exhausted"
        big = (await c.post("/sessions", json={"model": QWEN, "max_flops": 1e17}, headers=auth(tc))).json()
        r = await c.post("/v1/chat/completions", json={**body, "session_id": big["id"]}, headers=auth(tc))
        assert r.status_code == 200, r.text
        spent = (await c.get(f"/sessions/{big['id']}", headers=auth(tc))).json()["spent"]
        assert spent == pytest.approx(r.json()["network"]["flops"])


async def test_broke_user_gets_402(pool):
    h = pool
    _, th = account(h, "host@lan.test", credit=0)
    _, tb = account(h, "broke@lan.test", credit=0)              # no starter grant outside /auth/register
    await two_nodes(h, th, th)
    r = await chat(h, QWEN, max_tokens=8, headers=auth(tb))
    assert r.status_code == 402 and "insufficient FLOPs" in r.text


async def test_streaming_chat_through_the_main_server(pool):
    h = pool
    _, th = account(h, "host@lan.test", credit=0)
    _, tc = account(h, "chatter@lan.test")
    await two_nodes(h, th, th)
    r = await chat(h, QWEN, max_tokens=6, stream=True, headers=auth(tc))
    assert r.status_code == 200, r.text
    lines = [line for line in r.text.splitlines() if line.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    final = json.loads(lines[-2][6:])
    assert final["choices"] == [] and final["network"]["flops"] > 0


async def test_banned_owner_stops_earning_and_cannot_chat(pool):
    h = pool
    host, th = account(h, "host@lan.test", credit=0)
    _, tc = account(h, "chatter@lan.test")
    await two_nodes(h, th, th)
    s = sv(h)
    s.auth.set_banned(s.auth.get(host), True)
    r = await chat(h, QWEN, max_tokens=4, headers=auth(th))
    assert r.status_code == 403
    r = await chat(h, QWEN, max_tokens=4, headers=auth(tc))
    assert r.status_code == 200
    assert earned(h, host) == 0
