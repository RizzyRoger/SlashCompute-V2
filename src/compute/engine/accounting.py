"""How inference reports credits. The main coordinator plugs in its FLOP credits; tests use :class:`NullAccounting`."""

from __future__ import annotations

from typing import AsyncIterator, Callable, Optional, Protocol

from fastapi import Request


class AccountingError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class Accounting(Protocol):
    def requester(self, request: Request) -> Optional[str]:
        """The signed-in user making a chat request (None = anonymous LAN use, free)."""
        ...

    def bind_node(self, node_id: str, session_token: Optional[str]) -> None:
        """An inference node registered with its owner's session: its earnings go to that user."""
        ...

    def reserve(self, user_id: str, account_id: str, flops: float, session_id: Optional[str] = None) -> None:
        """Hold ``flops`` of the user's credits for one chat request (raises AccountingError 402).

        ``session_id`` is the chat session whose FLOP budget also caps this request
        (402 with message ``budget_exhausted`` when the session cannot cover it)."""
        ...

    def record(self, account_id: str, per_node: dict[str, float], tokens: int, wall_s: float) -> None:
        """A request finished: credit each hosting node, log usage, consume from the reservation."""
        ...

    def settle(self, account_id: str) -> None:
        """Refund whatever the request did not use."""
        ...


class NullAccounting:
    """No credits: records totals in memory (standalone app and tests)."""

    def __init__(self) -> None:
        self.earned: dict[str, float] = {}
        self.records: list[dict] = []

    def requester(self, request: Request) -> Optional[str]:
        return None

    def bind_node(self, node_id: str, session_token: Optional[str]) -> None:
        return None

    def reserve(self, user_id: str, account_id: str, flops: float, session_id: Optional[str] = None) -> None:
        return None

    def record(self, account_id: str, per_node: dict[str, float], tokens: int, wall_s: float) -> None:
        for node_id, flops in per_node.items():
            self.earned[node_id] = self.earned.get(node_id, 0.0) + flops
        self.records.append({"account_id": account_id, "per_node": dict(per_node), "tokens": tokens,
                             "wall_s": wall_s})

    def settle(self, account_id: str) -> None:
        return None


class Gate(Protocol):
    """The waitlist in front of the pool. ``admit`` yields ``{"type": "queued", ...}`` events while the
    request waits, then every event of ``run()`` (the real request), releasing its place at the end."""

    def admit(self, model_id: str, user_id: str, max_tokens: int,
              run: "Callable[[], AsyncIterator[dict]]") -> "AsyncIterator[dict]":
        ...


class OpenGate:
    """No waitlist: run straight away (standalone app and tests)."""

    def admit(self, model_id: str, user_id: str, max_tokens: int,
              run: "Callable[[], AsyncIterator[dict]]") -> "AsyncIterator[dict]":
        return run()
