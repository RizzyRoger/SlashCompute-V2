"""Helpers shared by the main-server tests (accounts over HTTP)."""

from __future__ import annotations

ADMIN = "admin@example.com"
ADMIN_PW = "admin-password"


def services(c):
    return c.app.state.services


def register(c, email: str, name: str = "", terms: bool = False) -> str:
    r = c.post("/auth/register", json={"email": email, "password": "password1", "name": name or email.split("@")[0]})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    if terms:
        version = c.get("/terms").json()["version"]
        assert c.post("/auth/terms", json={"version": version}, headers=bearer(token)).status_code == 200
    return token


def admin_token(c) -> str:
    services(c).auth.ensure_admin(ADMIN, ADMIN_PW)
    r = c.post("/auth/login", json={"email": ADMIN, "password": ADMIN_PW})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}
