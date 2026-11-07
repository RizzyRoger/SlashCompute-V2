"""The main server: accounts, FLOP ledger, waitlist and admin API, with the inference engine mounted."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Callable, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from compute.common.config import EngineConfig
from compute.engine import PREFIX
from compute.engine.accounting import AccountingError
from compute.engine.config import InferenceSettings
from compute.engine.coordinator.queue import Waitlist, recent_tok_s
from compute.engine.coordinator.service import InferenceService, mount
from compute.server.accounting import ServerAccounting, node_owner_or_error
from compute.server.auth import Auth, AuthError, is_admin, request_token
from compute.server.db import Database, database_path
from compute.server.ledger import Ledger, LedgerError, starter_flops
from compute.server.routes import make_router

log = logging.getLogger(__name__)
VERSION = "0.2.0"
REGISTER_PATH = f"{PREFIX}/nodes/register"


@dataclass
class Services:
    cfg: EngineConfig
    db: Database
    auth: Auth
    ledger: Ledger
    accounting: ServerAccounting
    waitlist: Waitlist
    svc: InferenceService
    starter_flops: Callable[[], float] = starter_flops

    def node_name(self, node_id: str) -> str:
        row = self.svc.conn.execute("SELECT name FROM nodes WHERE id=?", (node_id,)).fetchone()
        return row["name"] if row else node_id

    def authorize(self, request: Request) -> bool:
        """Engine shared-token routes: the admin session passes; ``/v1/*`` also takes any signed-in account."""
        user = self.auth.session_user(request_token(request))
        if user is None or user.banned:
            return False
        return is_admin(user) or request.url.path.startswith("/v1/")


def default_settings(cfg: EngineConfig) -> InferenceSettings:
    return InferenceSettings.from_env(DB_PATH=str(cfg.coordinator_dir / "inference.sqlite3"),
                                      MODELS_DIR=str(cfg.coordinator_dir / "models"))


def build_services(cfg: EngineConfig, settings: Optional[InferenceSettings] = None) -> Services:
    db = Database(database_path(cfg.home))
    auth, ledger = Auth(db), Ledger(db)
    ledger.release_stale()
    accounting = ServerAccounting(auth, ledger)
    waitlist = Waitlist()
    svc = InferenceService(settings or default_settings(cfg), accounting, waitlist)
    waitlist.tok_s_fn = lambda model_id: recent_tok_s(svc.conn, model_id)
    sv = Services(cfg, db, auth, ledger, accounting, waitlist, svc)
    svc.authorize = sv.authorize
    return sv


class NodeRegistrationGuard:
    """ASGI middleware: a node registering with a session must belong to a signed-in, unbanned account that
    accepted the current terms (403 ``terms_required``). The engine itself only logs a failed bind."""

    def __init__(self, app, auth: Auth) -> None:
        self.app, self.auth = app, auth

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != REGISTER_PATH:
            await self.app(scope, receive, send)
            return
        body, more = b"", True
        while more:
            msg = await receive()
            body += msg.get("body", b"")
            more = msg.get("more_body", False)
        error = self.check(body)
        if error is not None:
            await JSONResponse({"detail": str(error)}, status_code=error.status)(scope, receive, send)
            return
        replayed = False

        async def replay():
            nonlocal replayed
            if replayed:
                return await receive()
            replayed = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)

    def check(self, body: bytes) -> Optional[AccountingError]:
        try:
            token = (json.loads(body or b"{}") or {}).get("session_token")
        except (ValueError, AttributeError):
            return None  # let the engine reject malformed bodies
        if not token:
            return None
        try:
            node_owner_or_error(self.auth, token)
        except AccountingError as e:
            return e
        return None


async def _advertise(port: int):
    try:
        from compute.common.discovery import Advertiser

        adv = Advertiser(port, version=VERSION)
        await adv.start()
        return adv
    except Exception as e:  # noqa: BLE001 - discovery is best-effort
        log.warning("mDNS advertising unavailable: %r", e)
        return None


async def _stop_advertising(adv) -> None:
    try:
        await adv.close()
    except Exception as e:  # noqa: BLE001 - shutting down anyway
        log.warning("mDNS goodbye failed: %r", e)


def create_app(cfg: EngineConfig, settings: Optional[InferenceSettings] = None, advertise: bool = False) -> FastAPI:
    sv = build_services(cfg, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sv.svc.start()
        adv = await _advertise(cfg.coordinator_port) if advertise else None
        try:
            yield
        finally:
            if adv is not None:
                await _stop_advertising(adv)
            await sv.svc.stop()
            sv.db.close()

    app = FastAPI(title="/compute main server", version=VERSION, lifespan=lifespan)
    app.state.services = sv
    app.state.inference = sv.svc
    app.add_middleware(NodeRegistrationGuard, auth=sv.auth)

    @app.exception_handler(AuthError)
    async def auth_error(_: Request, e: AuthError):
        return JSONResponse({"detail": str(e)}, status_code=e.status)

    @app.exception_handler(LedgerError)
    async def ledger_error(_: Request, e: LedgerError):
        return JSONResponse({"detail": str(e)}, status_code=e.status)

    @app.get("/health")
    async def health():
        return {"ok": True, "server": "compute", "version": VERSION, "inference_nodes": len(sv.svc.online)}

    app.include_router(make_router(sv))
    mount(app, sv.svc)
    return app
