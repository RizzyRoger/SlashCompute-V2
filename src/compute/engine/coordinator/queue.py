"""The waitlist in front of the pool: a :class:`~compute.engine.accounting.Gate`.

A pipeline serves one request at a time, so each model gets a FIFO with one request in flight.
While a request waits it yields ``{"type": "queued", "queue_id", "position", "eta_s"}`` (again
whenever its position changes, and at least every ``tick_s``). A waiter can be cancelled; its
stream then ends with an ``error`` event ``"cancelled"`` (status 499).
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Optional

DEFAULT_TOK_S = 10.0
TICK_S = 2.0


@dataclass
class Ticket:
    queue_id: str
    model_id: str
    user_id: str
    max_tokens: int
    created: float = field(default_factory=time.time)
    started: Optional[float] = None
    cancelled: bool = False


def cancelled_event() -> dict:
    return {"type": "error", "error": "cancelled", "status": 499, "retryable": False, "job_id": None}


def recent_tok_s(conn: sqlite3.Connection, model_id: str, n: int = 10) -> Optional[float]:
    """Mean generation speed of the model's last ``n`` finished requests (None if none yet)."""
    rows = conn.execute("SELECT tok_s FROM jobs WHERE model_id=? AND state='done' AND tok_s > 0 "
                        "ORDER BY finished_at DESC LIMIT ?", (model_id, n)).fetchall()
    speeds = [float(r[0]) for r in rows]
    return sum(speeds) / len(speeds) if speeds else None


def eta_seconds(tokens_ahead: float, tok_s: Optional[float]) -> float:
    speed = tok_s if tok_s and tok_s > 0 else DEFAULT_TOK_S
    return round(max(tokens_ahead, 0.0) / speed, 1)


class Waitlist:
    def __init__(self, tok_s_fn: Optional[Callable[[str], Optional[float]]] = None, tick_s: float = TICK_S) -> None:
        self.tok_s_fn = tok_s_fn or (lambda _model_id: None)
        self.tick_s = tick_s
        self.waiting: dict[str, list[Ticket]] = {}
        self.running: dict[str, Ticket] = {}
        self._changed = asyncio.Event()

    # ------------------------------------------------------------ views

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    def find(self, queue_id: str) -> Optional[Ticket]:
        return next((t for q in self.waiting.values() for t in q if t.queue_id == queue_id), None)

    def position(self, t: Ticket) -> int:
        return self.waiting.get(t.model_id, []).index(t) + 1

    def _tok_s(self, model_id: str) -> Optional[float]:
        try:
            return self.tok_s_fn(model_id)
        except Exception:  # noqa: BLE001 - an ETA is only a hint
            return None

    def tokens_ahead(self, t: Ticket, tok_s: Optional[float]) -> float:
        """max_tokens of every request ahead, the one in flight minus what it has likely produced."""
        queue = self.waiting.get(t.model_id, [])
        ahead = sum(o.max_tokens for o in queue[:queue.index(t)])
        cur = self.running.get(t.model_id)
        if cur is not None:
            done = (time.time() - (cur.started or time.time())) * (tok_s or DEFAULT_TOK_S)
            ahead += max(cur.max_tokens - done, 0.0)
        return ahead

    def event(self, t: Ticket) -> dict:
        tok_s = self._tok_s(t.model_id)
        return {"type": "queued", "queue_id": t.queue_id, "position": self.position(t),
                "eta_s": eta_seconds(self.tokens_ahead(t, tok_s), tok_s)}

    def snapshot(self, user_id: Optional[str] = None) -> dict:
        models = {}
        for model_id, queue in self.waiting.items():
            if queue:
                models[model_id] = [{k: v for k, v in self.event(t).items() if k != "type"} | {"mine": t.user_id == user_id}
                                    for t in queue]
        return {"models": models}

    def length(self) -> int:
        return sum(len(q) for q in self.waiting.values())

    # ------------------------------------------------------------ control

    def cancel(self, queue_id: str) -> bool:
        t = self.find(queue_id)
        if t is None:
            return False
        t.cancelled = True
        self._notify()
        return True

    def _can_start(self, t: Ticket) -> bool:
        queue = self.waiting.get(t.model_id, [])
        return t.model_id not in self.running and bool(queue) and queue[0] is t

    def _leave(self, t: Ticket) -> None:
        queue = self.waiting.get(t.model_id, [])
        if t in queue:
            queue.remove(t)
        if not queue:
            self.waiting.pop(t.model_id, None)
        if self.running.get(t.model_id) is t:
            del self.running[t.model_id]
        self._notify()

    async def admit(self, model_id: str, user_id: str, max_tokens: int,
                    run: Callable[[], AsyncIterator[dict]]) -> AsyncIterator[dict]:
        t = Ticket("q-" + uuid.uuid4().hex[:10], model_id, user_id, max(int(max_tokens or 0), 1))
        self.waiting.setdefault(model_id, []).append(t)
        try:
            last_pos, last_emit = None, 0.0
            while not self._can_start(t):
                if t.cancelled:
                    yield cancelled_event()
                    return
                pos = self.position(t)
                if pos != last_pos or time.monotonic() - last_emit >= self.tick_s:
                    last_pos, last_emit = pos, time.monotonic()
                    yield self.event(t)
                changed = self._changed
                if t.cancelled or self._can_start(t):
                    continue
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(changed.wait(), self.tick_s)
            self.waiting[model_id].remove(t)
            t.started = time.time()
            self.running[model_id] = t
            self._notify()
            async for ev in run():
                yield ev
        finally:
            self._leave(t)
