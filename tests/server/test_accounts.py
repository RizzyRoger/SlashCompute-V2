"""Admin-only start, accounts, terms, wallet, checkout, admin API, leaderboard, sessions (no cluster)."""

from __future__ import annotations

import time

import pytest
from typer.testing import CliRunner

from compute.engine.accounting import AccountingError
from compute.server.db import Txn
from compute.server.ledger import PFLOP, STARTER_FLOPS_DEFAULT, WEEK_S
from compute.server.main import app as server_cli
from srv_helpers import ADMIN, ADMIN_PW, admin_token, bearer, register, services


# ------------------------------------------------------------ start (admin only)

@pytest.fixture
def no_bind(monkeypatch):
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: calls.append((a, kw)))
    return calls


def serve(home, email, password):
    return CliRunner().invoke(server_cli, ["serve", "--admin-email", email, "--admin-password", password,
                                           "--home", str(home), "--no-mdns"])


def test_non_admin_email_cannot_start_the_server(tmp_path, no_bind):
    res = serve(tmp_path, "someone@else.test", "password1")
    assert res.exit_code == 2
    assert not no_bind                        # refused before binding


def test_admin_is_created_on_first_start_and_checked_afterwards(tmp_path, no_bind):
    assert serve(tmp_path, ADMIN.upper(), ADMIN_PW).exit_code == 0
    assert len(no_bind) == 1
    assert serve(tmp_path, ADMIN, "wrong-password").exit_code == 2
    assert len(no_bind) == 1
    assert serve(tmp_path, ADMIN, ADMIN_PW).exit_code == 0


def test_admin_email_env_override(tmp_path, no_bind, monkeypatch):
    monkeypatch.setenv("COMPUTE_ADMIN_EMAIL", "boss@lan.test")
    assert serve(tmp_path, ADMIN, ADMIN_PW).exit_code == 2
    assert serve(tmp_path, "boss@lan.test", ADMIN_PW).exit_code == 0


# ------------------------------------------------------------ accounts

def test_health(client):
    body = client.get("/health").json()
    assert body == {"ok": True, "server": "compute", "version": "0.2.0", "inference_nodes": 0, "admin_email": ADMIN}


def test_first_user_is_not_admin_and_gets_the_starter_grant(client):
    token = register(client, "first@lan.test")
    me = client.get("/auth/me", headers=bearer(token)).json()
    assert me["is_admin"] is False and me["accepted_terms"] is False and me["terms_version"] is None
    assert set(me) == {"id", "email", "name", "is_admin", "banned", "flagged", "accepted_terms", "terms_version"}
    wallet = client.get("/wallet", headers=bearer(token)).json()
    assert wallet["balance"] == pytest.approx(STARTER_FLOPS_DEFAULT) and wallet["unlimited"] is False
    txns = client.get("/wallet/transactions", headers=bearer(token)).json()
    assert [t["kind"] for t in txns] == ["starter"]


def test_starter_flops_env(client, monkeypatch):
    monkeypatch.setenv("COMPUTE_STARTER_FLOPS", "1e12")
    token = register(client, "a@lan.test")
    assert client.get("/wallet", headers=bearer(token)).json()["balance"] == pytest.approx(1e12)


def test_admin_email_cannot_be_registered(client):
    r = client.post("/auth/register", json={"email": ADMIN, "password": "password1", "name": "x"})
    assert r.status_code == 403


def test_login_cookie_logout_and_rename(client):
    register(client, "a@lan.test")
    client.cookies.clear()
    assert client.post("/auth/login", json={"email": "a@lan.test", "password": "nope-nope"}).status_code == 401
    r = client.post("/auth/login", json={"email": "A@lan.test", "password": "password1"})
    assert r.status_code == 200 and "compute_session" in r.cookies
    assert client.get("/auth/me").json()["email"] == "a@lan.test"            # cookie auth
    assert client.patch("/auth/me", json={"name": "Alice"}).json()["name"] == "Alice"
    assert client.post("/auth/logout").json() == {"ok": True}
    client.cookies.clear()
    assert client.get("/auth/me", headers=bearer(r.json()["token"])).status_code == 401


def test_terms_must_match_the_current_version(client):
    token = register(client, "a@lan.test")
    terms = client.get("/terms").json()
    assert "DRAFT" in terms["text"] and len(terms["version"]) == 64
    assert client.post("/auth/terms", json={"version": "old"}, headers=bearer(token)).status_code == 409
    me = client.post("/auth/terms", json={"version": terms["version"]}, headers=bearer(token)).json()
    assert me["accepted_terms"] is True and me["terms_version"] == terms["version"]


def test_banned_user_is_refused_everywhere(client):
    token = register(client, "bad@lan.test")
    admin = admin_token(client)
    uid = client.get("/auth/me", headers=bearer(token)).json()["id"]
    assert client.post(f"/admin/api/users/{uid}/ban", json={"banned": True}, headers=bearer(admin)).json() == {"ok": True}
    assert client.get("/wallet", headers=bearer(token)).status_code == 403
    assert client.post("/auth/login", json={"email": "bad@lan.test", "password": "password1"}).status_code == 403
    r = client.post("/v1/chat/completions", headers=bearer(token), json={"model": "m", "messages": []})
    assert r.status_code == 403 and "banned" in r.text


def test_chat_requires_sign_in(client):
    r = client.post("/v1/chat/completions", json={"model": "m", "messages": []})
    assert r.status_code == 401


# ------------------------------------------------------------ wallet + admin

def test_test_mode_checkout(client):
    token = register(client, "a@lan.test")
    r = client.post("/wallet/checkout", json={"pflops": 10}, headers=bearer(token))
    assert r.status_code == 200, r.text
    receipt = r.json()["receipt"]
    assert receipt["test_mode"] is True and receipt["price_usd"] == 5.0 and receipt["flops"] == 10 * PFLOP
    assert r.json()["balance"] == pytest.approx(STARTER_FLOPS_DEFAULT + 10 * PFLOP)
    assert client.get("/wallet", headers=bearer(token)).json()["purchased_total"] == pytest.approx(10 * PFLOP)
    assert client.post("/wallet/checkout", json={"pflops": -1}, headers=bearer(token)).status_code == 400


def test_admin_wallet_is_unlimited_and_admin_api(client):
    admin = admin_token(client)
    wallet = client.get("/wallet", headers=bearer(admin)).json()
    assert wallet["balance"] is None and wallet["unlimited"] is True
    user = register(client, "a@lan.test")
    assert client.get("/admin/api/users", headers=bearer(user)).status_code == 403
    users = client.get("/admin/api/users", headers=bearer(admin)).json()
    row = next(u for u in users if u["email"] == "a@lan.test")
    assert set(row) == {"id", "email", "name", "balance", "earned_total", "spent_total", "flagged", "flag_reason",
                        "banned", "created"}
    r = client.post(f"/admin/api/users/{row['id']}/credit", json={"flops": 1e15, "note": "thanks"},
                    headers=bearer(admin))
    assert r.json() == {"ok": True, "balance": pytest.approx(STARTER_FLOPS_DEFAULT + 1e15)}
    kinds = [t["kind"] for t in client.get("/wallet/transactions", headers=bearer(user)).json()]
    assert kinds[0] == "admin_credit"
    client.post(f"/admin/api/users/{row['id']}/flag", json={"flagged": True, "reason": "odd"}, headers=bearer(admin))
    row = next(u for u in client.get("/admin/api/users", headers=bearer(admin)).json() if u["id"] == row["id"])
    assert row["flagged"] is True and row["flag_reason"] == "odd"
    overview = client.get("/admin/api/overview", headers=bearer(admin)).json()
    assert overview == {"users": 2, "nodes_online": 0, "flops_earned_total": 0.0, "flops_purchased_total": 0.0,
                        "queue_length": 0}


def test_admin_page_404s_until_installed(client, tmp_path, monkeypatch):
    monkeypatch.setattr("compute.server.routes.ADMIN_DIR", tmp_path / "admin")
    r = client.get("/admin")
    assert r.status_code == 404 and "admin.html" in r.json()["detail"]
    (tmp_path / "admin").mkdir()
    (tmp_path / "admin" / "admin.html").write_text("<h1>admin</h1>")
    (tmp_path / "admin" / "admin.js").write_text("1")
    assert client.get("/admin").text == "<h1>admin</h1>"
    assert client.get("/admin/static/admin.js").text == "1"
    assert client.get("/admin/static/../../etc/passwd").status_code == 404


def test_leaderboard_all_vs_week(client):
    sv = services(client)
    old_id = client.get("/auth/me", headers=bearer(register(client, "old@lan.test", "Oldtimer"))).json()["id"]
    new_id = client.get("/auth/me", headers=bearer(register(client, "new@lan.test", "Newbie"))).json()["id"]
    sv.db.add(Txn(user_id=old_id, kind="earn", flops=5e15, ts=time.time() - WEEK_S - 60))
    sv.db.add(Txn(user_id=new_id, kind="earn", flops=1e15))
    everything = client.get("/leaderboard?window=all").json()
    assert everything == [{"rank": 1, "name": "Oldtimer", "flops": 5e15}, {"rank": 2, "name": "Newbie", "flops": 1e15}]
    assert client.get("/leaderboard?window=week").json() == [{"rank": 1, "name": "Newbie", "flops": 1e15}]
    assert "@" not in str(everything)


# ------------------------------------------------------------ chat sessions + ledger rules

def test_session_budget(client):
    token = register(client, "a@lan.test")
    s = client.post("/sessions", json={"model": "m.gguf", "max_flops": 1e14}, headers=bearer(token)).json()
    assert s["max_flops"] == 1e14 and s["spent"] == 0 and s["model"] == "m.gguf"
    assert client.get(f"/sessions/{s['id']}", headers=bearer(token)).json() == s
    other = register(client, "b@lan.test")
    assert client.get(f"/sessions/{s['id']}", headers=bearer(other)).status_code == 404
    sv = services(client)
    uid = client.get("/auth/me", headers=bearer(token)).json()["id"]
    with pytest.raises(AccountingError) as e:
        sv.accounting.reserve(uid, "inf-1", 2e14, session_id=s["id"])
    assert e.value.status == 402 and str(e.value) == "budget_exhausted"
    sv.accounting.reserve(uid, "inf-2", 6e13, session_id=s["id"])
    sv.accounting.record("inf-2", {"n-x": 5e13}, 10, 1.0)
    sv.accounting.settle("inf-2")
    assert client.get(f"/sessions/{s['id']}", headers=bearer(token)).json()["spent"] == pytest.approx(5e13)
    with pytest.raises(AccountingError) as e:
        sv.accounting.reserve(uid, "inf-3", 6e13, session_id=s["id"])
    assert e.value.status == 402


def test_reservation_holds_then_charges_actual(client):
    token = register(client, "a@lan.test")
    sv = services(client)
    uid = client.get("/auth/me", headers=bearer(token)).json()["id"]
    sv.accounting.reserve(uid, "inf-1", 3e15)
    assert client.get("/wallet", headers=bearer(token)).json()["reserved"] == pytest.approx(3e15)
    with pytest.raises(AccountingError) as e:          # only 1e15 left unheld
        sv.accounting.reserve(uid, "inf-2", 2e15)
    assert e.value.status == 402 and "insufficient" in str(e.value)
    sv.accounting.record("inf-1", {"n-a": 1e15}, 10, 1.0)
    sv.accounting.settle("inf-1")
    wallet = client.get("/wallet", headers=bearer(token)).json()
    assert wallet["reserved"] == 0 and wallet["spent_total"] == pytest.approx(1e15)
    assert wallet["balance"] == pytest.approx(STARTER_FLOPS_DEFAULT - 1e15)


def test_terms_required_and_ban_for_node_registration(client):
    body = {"name": "mac", "llama_build": "b1"}
    no_terms = register(client, "a@lan.test")
    r = client.post("/inference/nodes/register", json={**body, "session_token": no_terms})
    assert r.status_code == 403 and r.json()["detail"] == "terms_required"
    ok = register(client, "b@lan.test", terms=True)
    r = client.post("/inference/nodes/register", json={**body, "session_token": ok})
    assert r.status_code == 200, r.text
    node_id = r.json()["node_id"]
    assert services(client).ledger.owner_of(node_id) is not None
    live = client.get("/wallet/live", headers=bearer(ok)).json()
    assert live["nodes"] == [{"node_id": node_id, "name": "mac", "online": True, "flops_total": 0.0}]
    admin = admin_token(client)
    uid = client.get("/auth/me", headers=bearer(ok)).json()["id"]
    client.post(f"/admin/api/users/{uid}/ban", json={"banned": True}, headers=bearer(admin))
    r = client.post("/inference/nodes/register", json={**body, "session_token": ok})
    assert r.status_code == 403 and "banned" in r.json()["detail"]
    assert client.post("/inference/nodes/register", json={**body, "session_token": "nope"}).status_code == 401


def test_engine_admin_routes_accept_the_admin_session(client):
    from dataclasses import replace

    sv = services(client)
    sv.svc.s = replace(sv.svc.s, TOKEN="pool-secret")
    stop = "/inference/pipelines/p-none/stop"
    assert client.post(stop).status_code == 401
    user = register(client, "a@lan.test")
    assert client.post(stop, headers=bearer(user)).status_code == 401
    assert client.post(stop, headers=bearer(admin_token(client))).status_code == 404   # past auth: unknown pipeline
