"""The FLOP ledger: one signed row per movement, plus per-request holds and per-session budgets.

Kinds: ``starter, earn, spend, purchase, admin_credit, refund``. A chat request holds its estimate
(``reserve``), is charged what it actually used (``record``: ``spend`` for the requester, ``earn``
1:1 for each node's owner), and its hold is released when it ends (``settle``). The admin is never
charged; contributors still earn from the admin's requests.
"""

from __future__ import annotations

import math
import os
import secrets
import time
from typing import Optional

from sqlalchemy import func
from sqlmodel import Session, select

from compute.server.db import ChatSession, Database, NodeOwner, Reservation, Txn, User

PFLOP = 1e15
# ~20 short replies on Qwen3.8-27B: 20 x (2*27e9*500 prompt + 2*27e9*300 generated * gen weight 10)
STARTER_FLOPS_DEFAULT = 4e15
PRICE_USD_PER_PFLOP = 0.50
MAX_CHECKOUT_PFLOPS = 10_000.0
WEEK_S = 7 * 24 * 3600
LIVE_WINDOW_S = 60.0
KINDS = ("starter", "earn", "spend", "purchase", "admin_credit", "refund")


class LedgerError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def starter_flops() -> float:
    raw = os.environ.get("COMPUTE_STARTER_FLOPS")
    if not raw:
        return STARTER_FLOPS_DEFAULT
    try:
        value = float(raw)
    except ValueError:
        return STARTER_FLOPS_DEFAULT
    if not math.isfinite(value) or value < 0:
        return STARTER_FLOPS_DEFAULT
    return value


def price_usd(pflops: float) -> float:
    return round(pflops * PRICE_USD_PER_PFLOP, 2)


def txn_view(t: Txn) -> dict:
    return {"id": t.id, "kind": t.kind, "flops": t.flops, "ts": t.ts, "note": t.note}


def session_view(cs: ChatSession) -> dict:
    return {"id": cs.id, "model": cs.model, "max_flops": cs.max_flops, "spent": cs.spent, "created": cs.created}


def _total(s: Session, *where) -> float:
    return float(s.exec(select(func.coalesce(func.sum(Txn.flops), 0.0)).where(*where)).one())


def _holds(s: Session, *where) -> float:
    """Unspent part of open reservations matching ``where``."""
    rows = s.exec(select(Reservation).where(Reservation.open == True, *where)).all()  # noqa: E712
    return sum(max(r.held - r.spent, 0.0) for r in rows)


class Ledger:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------ rows

    def post(self, user_id: str, kind: str, flops: float, note: Optional[str] = None,
             node_id: Optional[str] = None, account_id: Optional[str] = None) -> Txn:
        if kind not in KINDS:
            raise ValueError(f"unknown ledger kind {kind!r}")
        row = Txn(user_id=user_id, kind=kind, flops=float(flops), note=note, node_id=node_id, account_id=account_id)
        self.db.add(row)
        return row

    def transactions(self, user_id: str, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        with self.db.session() as s:
            rows = s.exec(select(Txn).where(Txn.user_id == user_id)
                          .order_by(Txn.ts.desc(), Txn.id.desc()).limit(limit)).all()
        return [txn_view(t) for t in rows]

    # ------------------------------------------------------------ totals

    def balance(self, user_id: str) -> float:
        with self.db.session() as s:
            return _total(s, Txn.user_id == user_id)

    def kind_total(self, user_id: Optional[str], kind: str, since: float = 0.0) -> float:
        with self.db.session() as s:
            where = [Txn.kind == kind, Txn.ts >= since] + ([Txn.user_id == user_id] if user_id else [])
            return _total(s, *where)

    def held(self, user_id: str) -> float:
        with self.db.session() as s:
            return _holds(s, Reservation.user_id == user_id)

    def wallet(self, user_id: str, unlimited: bool) -> dict:
        return {
            "balance": None if unlimited else self.balance(user_id),
            "unlimited": unlimited,
            "reserved": self.held(user_id),
            "earned_total": self.kind_total(user_id, "earn"),
            "earned_week": self.kind_total(user_id, "earn", since=time.time() - WEEK_S),
            "spent_total": abs(self.kind_total(user_id, "spend")),
            "purchased_total": self.kind_total(user_id, "purchase"),
        }

    # ------------------------------------------------------------ nodes

    def bind_node(self, node_id: str, user_id: str) -> None:
        self.db.save(NodeOwner(node_id=node_id, user_id=user_id))

    def owner_of(self, node_id: str) -> Optional[str]:
        row = self.db.get(NodeOwner, node_id)
        return row.user_id if row else None

    def nodes_of(self, user_id: str) -> list[str]:
        with self.db.session() as s:
            return list(s.exec(select(NodeOwner.node_id).where(NodeOwner.user_id == user_id)).all())

    def node_earned(self, node_id: str) -> float:
        with self.db.session() as s:
            return _total(s, Txn.kind == "earn", Txn.node_id == node_id)

    def earning_rate(self, user_id: str, window_s: float = LIVE_WINDOW_S) -> float:
        return self.kind_total(user_id, "earn", since=time.time() - window_s) / window_s

    # ------------------------------------------------------------ chat requests

    def reserve(self, user_id: str, account_id: str, flops: float, *, unlimited: bool,
                session_id: Optional[str] = None) -> None:
        if not math.isfinite(flops) or flops <= 0:
            raise LedgerError("FLOP estimate must be a positive number", 400)
        with self.db.session() as s:
            if session_id:
                cs = s.get(ChatSession, session_id)
                if cs is None or cs.user_id != user_id:
                    raise LedgerError("unknown chat session", 404)
                if cs.max_flops - cs.spent - _holds(s, Reservation.session_id == session_id) < flops:
                    raise LedgerError("budget_exhausted", 402)
            if not unlimited:
                available = _total(s, Txn.user_id == user_id) - _holds(s, Reservation.user_id == user_id)
                if available < flops:
                    raise LedgerError("insufficient FLOPs", 402)
            s.add(Reservation(account_id=account_id, user_id=user_id, session_id=session_id, held=float(flops),
                              unlimited=unlimited))
            s.commit()

    def record(self, account_id: str, per_node: dict[str, float]) -> None:
        """Each node's owner earns its FLOPs; the requester (unless unlimited) spends the same total."""
        with self.db.session() as s:
            res = s.get(Reservation, account_id)
            if res is None:
                return
            raw = sum(f for f in per_node.values() if math.isfinite(f) and f > 0)
            room = max(0.0, float(res.held) - float(res.spent))
            bill = min(raw, room)
            scale = bill / raw if raw else 0.0
            for node_id, f in per_node.items():
                owner = s.get(NodeOwner, node_id)
                owner_user = s.get(User, owner.user_id) if owner else None
                share = f * scale
                if share > 0 and owner_user is not None and not owner_user.banned:
                    s.add(Txn(user_id=owner_user.id, kind="earn", flops=share, node_id=node_id,
                              account_id=account_id, note="served a chat request"))
            if bill > 0 and not res.unlimited:
                s.add(Txn(user_id=res.user_id, kind="spend", flops=-bill, account_id=account_id,
                          note="chat request"))
            res.spent += bill
            s.add(res)
            if res.session_id and (cs := s.get(ChatSession, res.session_id)) is not None:
                cs.spent += bill
                s.add(cs)
            s.commit()

    def settle(self, account_id: str) -> None:
        """Release whatever the request held but did not use."""
        with self.db.session() as s:
            res = s.get(Reservation, account_id)
            if res is None or not res.open:
                return
            res.open = False
            s.add(res)
            s.commit()

    def release_stale(self) -> None:
        """At startup: no request survives a restart, so every open hold is released."""
        with self.db.session() as s:
            for r in s.exec(select(Reservation).where(Reservation.open == True)).all():  # noqa: E712
                r.open = False
                s.add(r)
            s.commit()

    # ------------------------------------------------------------ chat sessions

    def create_session(self, user_id: str, model: str, max_flops: float) -> dict:
        if not model:
            raise LedgerError("model is required")
        if not math.isfinite(max_flops) or max_flops <= 0:
            raise LedgerError("max_flops must be a positive number")
        cs = ChatSession(id="s-" + secrets.token_hex(8), user_id=user_id, model=model, max_flops=float(max_flops))
        self.db.add(cs)
        return session_view(cs)

    def get_session(self, session_id: str, user_id: str, admin: bool = False) -> dict:
        cs = self.db.get(ChatSession, session_id)
        if cs is None or (cs.user_id != user_id and not admin):
            raise LedgerError("unknown chat session", 404)
        return session_view(cs)

    # ------------------------------------------------------------ leaderboard + purchases

    def leaderboard(self, window: str = "all", limit: int = 50) -> list[dict]:
        since = time.time() - WEEK_S if window == "week" else 0.0
        total = func.sum(Txn.flops)
        stmt = (select(User.name, total).join(User, User.id == Txn.user_id)
                .where(Txn.kind == "earn", Txn.ts >= since).group_by(Txn.user_id)
                .order_by(total.desc()).limit(limit))
        with self.db.session() as s:
            rows = s.exec(stmt).all()
        return [{"rank": i + 1, "name": name, "flops": float(flops)} for i, (name, flops) in enumerate(rows) if flops]

    def checkout(self, user_id: str, pflops: float) -> dict:
        """TEST MODE purchase: no payment service is contacted and no money moves."""
        try:
            pflops = float(pflops)
        except (TypeError, ValueError) as e:
            raise LedgerError("pflops must be a number") from e
        if not 0 < pflops <= MAX_CHECKOUT_PFLOPS:
            raise LedgerError(f"pflops must be between 0 and {MAX_CHECKOUT_PFLOPS:g}")
        receipt = {"id": "test_" + secrets.token_hex(8), "pflops": pflops, "flops": pflops * PFLOP,
                   "price_usd": price_usd(pflops), "test_mode": True}
        self.post(user_id, "purchase", receipt["flops"],
                  note=f"TEST MODE purchase {receipt['id']}: {pflops:g} PFLOP for ${receipt['price_usd']:.2f}")
        return receipt
