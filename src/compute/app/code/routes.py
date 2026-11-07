"""``/api/code/*``: the app shell's endpoints for Code tabs (see docs/API.md, "Code harness").

Every handler is ``async``: turns are asyncio tasks and must be started on the server's loop.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Callable

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from compute.app.code.harness import CodeError, CodeManager, session_events


def checked(fn: Callable[..., Any], *args: Any) -> Any:
    try:
        return fn(*args)
    except CodeError as e:
        raise HTTPException(e.status, e.message) from e


async def sse(events: AsyncIterator[dict]) -> AsyncIterator[str]:
    async for event in events:
        yield f"data: {json.dumps(event)}\n\n"


def code_router(manager: CodeManager) -> APIRouter:
    r = APIRouter(prefix="/api/code")

    @r.get("/sessions")
    async def list_sessions():
        return {"sessions": manager.list()}

    @r.post("/sessions")
    async def open_session(body: dict):
        try:
            session = await manager.create(body)
        except CodeError as e:
            raise HTTPException(e.status, e.message) from e
        return session.meta()

    @r.get("/sessions/{sid}")
    async def snapshot(sid: str, after: int = 0):
        return checked(manager.get, sid).view(after)

    @r.patch("/sessions/{sid}")
    async def update_session(sid: str, body: dict):
        try:
            session = await manager.update(sid, body)
        except CodeError as e:
            raise HTTPException(e.status, e.message) from e
        return session.meta()

    @r.delete("/sessions/{sid}")
    async def close_session(sid: str):
        checked(manager.close, sid)
        return {"ok": True}

    @r.post("/sessions/{sid}/messages")
    async def send_message(sid: str, body: dict):
        session = checked(manager.get, sid)
        checked(session.start, str(body.get("text") or ""))
        return session.meta()

    @r.post("/sessions/{sid}/approval")
    async def approval(sid: str, body: dict):
        session = checked(manager.get, sid)
        checked(session.resolve_approval, str(body.get("id") or ""), bool(body.get("approve")),
                str(body.get("note") or ""))
        return {"ok": True}

    @r.post("/sessions/{sid}/plan")
    async def plan(sid: str, body: dict):
        session = checked(manager.get, sid)
        checked(session.answer_plan, str(body.get("action") or ""), str(body.get("note") or ""))
        return session.meta()

    @r.post("/sessions/{sid}/revert")
    async def revert(sid: str, body: dict):
        session = checked(manager.get, sid)
        try:
            turn = int(body.get("turn"))
        except (TypeError, ValueError) as e:
            raise HTTPException(400, "turn must be the number of a message in this tab.") from e
        return checked(session.revert, turn)

    @r.post("/sessions/{sid}/stop")
    async def stop(sid: str):
        session = checked(manager.get, sid)
        session.stop()
        return session.meta()

    @r.get("/sessions/{sid}/events")
    async def events(sid: str, after: int = 0):
        session = checked(manager.get, sid)
        return StreamingResponse(sse(session_events(session, after)), media_type="text/event-stream",
                                 headers={"cache-control": "no-cache"})

    return r
