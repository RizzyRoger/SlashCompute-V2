"""Public HTTP API of the main server: accounts, terms, wallet, chat sessions, waitlist, leaderboard, admin."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse

from compute.server.auth import COOKIE, SESSION_TTL_S, is_admin, request_token, user_view
from compute.server.db import User
from compute.server.terms import current_terms

if TYPE_CHECKING:
    from compute.server.app import Services

from compute.app.resources import admin_dir

ADMIN_DIR = admin_dir()   # source checkout or PyInstaller bundle


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=SESSION_TTL_S, httponly=True, samesite="lax")


def _float(value, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"{name} must be a number") from e
    if number != number or number in (float("inf"), float("-inf")):
        raise HTTPException(400, f"{name} must be a finite number")
    return number


def make_deps(sv: "Services"):
    def current_user(request: Request) -> Optional[User]:
        user = sv.auth.session_user(request_token(request))
        if user is not None and user.banned:
            raise HTTPException(403, "This account is banned.")
        return user

    def require_user(user: Optional[User] = Depends(current_user)) -> User:
        if user is None:
            raise HTTPException(401, "Sign in first.")
        return user

    def require_admin(user: User = Depends(require_user)) -> User:
        if not is_admin(user):
            raise HTTPException(403, "Admin only.")
        return user

    return current_user, require_user, require_admin


def make_router(sv: "Services") -> APIRouter:
    r = APIRouter()
    current_user, require_user, require_admin = make_deps(sv)

    # ------------------------------------------------------------ accounts

    @r.post("/auth/register")
    def register(body: dict, response: Response):
        user = sv.auth.register(body.get("email", ""), body.get("password", ""), body.get("name"))
        sv.ledger.post(user.id, "starter", sv.starter_flops(), note="welcome to /compute")
        token = sv.auth.issue(user)
        _set_cookie(response, token)
        return {"user": user_view(user), "token": token}

    @r.post("/auth/login")
    def login(body: dict, response: Response):
        user, token = sv.auth.login(body.get("email", ""), body.get("password", ""))
        _set_cookie(response, token)
        return {"user": user_view(user), "token": token}

    @r.post("/auth/logout")
    def logout(request: Request, response: Response):
        sv.auth.logout(request_token(request))
        response.delete_cookie(COOKIE)
        return {"ok": True}

    @r.get("/auth/me")
    def me(user: User = Depends(require_user)):
        return user_view(user)

    @r.patch("/auth/me")
    def patch_me(body: dict, user: User = Depends(require_user)):
        return user_view(sv.auth.rename(user, body.get("name")))

    @r.get("/terms")
    def terms():
        return current_terms()

    @r.post("/auth/terms")
    def accept_terms(body: dict, user: User = Depends(require_user)):
        return user_view(sv.auth.accept_terms(user, body.get("version", "")))

    # ------------------------------------------------------------ wallet

    @r.get("/wallet")
    def wallet(user: User = Depends(require_user)):
        return sv.ledger.wallet(user.id, unlimited=is_admin(user))

    @r.get("/wallet/transactions")
    def transactions(limit: int = 50, user: User = Depends(require_user)):
        return sv.ledger.transactions(user.id, limit)

    @r.get("/wallet/live")
    def live(user: User = Depends(require_user)):
        nodes = [{"node_id": nid, "name": sv.node_name(nid), "online": nid in sv.svc.online,
                  "flops_total": sv.ledger.node_earned(nid)} for nid in sv.ledger.nodes_of(user.id)]
        return {"earning_flops_per_s": sv.ledger.earning_rate(user.id),
                "earned_total": sv.ledger.kind_total(user.id, "earn"), "nodes": nodes}

    @r.post("/wallet/checkout")
    def checkout(body: dict, user: User = Depends(require_user)):
        receipt = sv.ledger.checkout(user.id, body.get("pflops"))
        return {"receipt": receipt, "balance": None if is_admin(user) else sv.ledger.balance(user.id)}

    # ------------------------------------------------------------ chat sessions

    @r.post("/sessions")
    def create_session(body: dict, user: User = Depends(require_user)):
        return sv.ledger.create_session(user.id, body.get("model", ""), _float(body.get("max_flops"), "max_flops"))

    @r.get("/sessions/{session_id}")
    def get_session(session_id: str, user: User = Depends(require_user)):
        return sv.ledger.get_session(session_id, user.id, admin=is_admin(user))

    # ------------------------------------------------------------ waitlist + leaderboard

    @r.get("/queue")
    def queue(user: Optional[User] = Depends(current_user)):
        return sv.waitlist.snapshot(user.id if user else None)

    @r.delete("/queue/{queue_id}")
    def cancel_queued(queue_id: str, user: User = Depends(require_user)):
        ticket = sv.waitlist.find(queue_id)
        if ticket is None:
            running = any(t.queue_id == queue_id for t in sv.waitlist.running.values())
            raise HTTPException(409 if running else 404,
                                "that request is already running" if running else "no such queued request")
        if ticket.user_id != user.id and not is_admin(user):
            raise HTTPException(403, "not your request")
        sv.waitlist.cancel(queue_id)
        return {"ok": True}

    @r.get("/leaderboard")
    def leaderboard(window: str = "all", _: Optional[User] = Depends(current_user)):
        if window not in ("all", "week"):
            raise HTTPException(400, "window must be all or week")
        return sv.ledger.leaderboard(window)

    # ------------------------------------------------------------ admin

    def admin_user_row(u: User) -> dict:
        return {"id": u.id, "email": u.email, "name": u.name,
                "balance": None if is_admin(u) else sv.ledger.balance(u.id),
                "earned_total": sv.ledger.kind_total(u.id, "earn"),
                "spent_total": abs(sv.ledger.kind_total(u.id, "spend")),
                "flagged": u.flagged, "flag_reason": u.flag_reason, "banned": u.banned, "created": u.created_at}

    def target(user_id: str) -> User:
        u = sv.auth.get(user_id)
        if u is None:
            raise HTTPException(404, "no such user")
        return u

    @r.get("/admin/api/users")
    def admin_users(_: User = Depends(require_admin)):
        return [admin_user_row(u) for u in sv.auth.all_users()]

    @r.post("/admin/api/users/{user_id}/credit")
    def admin_credit(user_id: str, body: dict, admin: User = Depends(require_admin)):
        u = target(user_id)
        flops = _float(body.get("flops"), "flops")
        if flops <= 0:
            raise HTTPException(400, "flops must be greater than zero")
        sv.ledger.post(u.id, "admin_credit", flops, note=(body.get("note") or "").strip()[:200] or "admin credit")
        return {"ok": True, "balance": None if is_admin(u) else sv.ledger.balance(u.id)}

    @r.post("/admin/api/users/{user_id}/ban")
    def admin_ban(user_id: str, body: dict, admin: User = Depends(require_admin)):
        u = target(user_id)
        if is_admin(u):
            raise HTTPException(400, "the admin cannot be banned")
        sv.auth.set_banned(u, bool(body.get("banned", True)))
        return {"ok": True}

    @r.post("/admin/api/users/{user_id}/flag")
    def admin_flag(user_id: str, body: dict, admin: User = Depends(require_admin)):
        sv.auth.set_flagged(target(user_id), bool(body.get("flagged", True)), body.get("reason"))
        return {"ok": True}

    @r.get("/admin/api/overview")
    def admin_overview(_: User = Depends(require_admin)):
        return {"users": len(sv.auth.all_users()), "nodes_online": len(sv.svc.online),
                "flops_earned_total": sv.ledger.kind_total(None, "earn"),
                "flops_purchased_total": sv.ledger.kind_total(None, "purchase"),
                "queue_length": sv.waitlist.length()}

    @r.get("/admin")
    def admin_page():
        return _admin_file("admin.html")

    @r.get("/admin/static/{path:path}")
    def admin_static(path: str):
        return _admin_file(path)

    return r


def _admin_file(path: str) -> FileResponse:
    """A file from ``server/admin/`` (the page itself is public; its data calls need the admin session)."""
    root = ADMIN_DIR.resolve()
    target = (root / path).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(404, f"admin file {path!r} is not installed (expected in {ADMIN_DIR})")
    return FileResponse(target)

