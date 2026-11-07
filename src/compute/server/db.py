"""Main-server state: accounts, sessions, the FLOP ledger, reservations and chat sessions (SQLite via SQLModel)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from sqlmodel import Field, Session, SQLModel, create_engine


def now() -> float:
    return time.time()


class User(SQLModel, table=True):
    __tablename__ = "users"
    id: str = Field(primary_key=True)
    email: str = Field(index=True, unique=True)
    password_hash: str
    name: str
    banned: bool = False
    flagged: bool = False
    flag_reason: Optional[str] = None
    terms_version: Optional[str] = None
    terms_accepted_at: Optional[float] = None
    created_at: float = Field(default_factory=now)


class SessionRow(SQLModel, table=True):
    __tablename__ = "sessions"
    token_hash: str = Field(primary_key=True)
    user_id: str = Field(index=True)
    created_at: float = Field(default_factory=now)
    expires_at: float


class Txn(SQLModel, table=True):
    """One ledger row. ``flops`` is signed: earn/starter/purchase/admin_credit/refund > 0, spend < 0."""
    __tablename__ = "txns"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: str = Field(index=True)
    kind: str = Field(index=True)
    flops: float
    ts: float = Field(default_factory=now, index=True)
    note: Optional[str] = None
    node_id: Optional[str] = Field(default=None, index=True)
    account_id: Optional[str] = None


class NodeOwner(SQLModel, table=True):
    __tablename__ = "node_owners"
    node_id: str = Field(primary_key=True)
    user_id: str = Field(index=True)


class Reservation(SQLModel, table=True):
    """FLOPs held for one chat request (``account_id`` from the engine) until it settles."""
    __tablename__ = "reservations"
    account_id: str = Field(primary_key=True)
    user_id: str = Field(index=True)
    session_id: Optional[str] = Field(default=None, index=True)
    held: float
    spent: float = 0.0
    unlimited: bool = False
    open: bool = True
    created_at: float = Field(default_factory=now)


class ChatSession(SQLModel, table=True):
    __tablename__ = "chat_sessions"
    id: str = Field(primary_key=True)
    user_id: str = Field(index=True)
    model: str
    max_flops: float
    spent: float = 0.0
    created: float = Field(default_factory=now)


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 30})
        SQLModel.metadata.create_all(self.engine)

    def session(self) -> Session:
        return Session(self.engine, expire_on_commit=False)

    def add(self, *rows) -> None:
        with self.session() as s:
            for r in rows:
                s.add(r)
            s.commit()

    def get(self, model, key):
        with self.session() as s:
            return s.get(model, key)

    def save(self, row) -> None:
        with self.session() as s:
            s.merge(row)
            s.commit()

    def update(self, model, key, **fields):
        """Change only these columns on the current row, so a stale object cannot wipe a ban."""
        with self.session() as s:
            row = s.get(model, key)
            if row is None:
                raise KeyError(key)
            for name, value in fields.items():
                setattr(row, name, value)
            s.commit()
            s.refresh(row)
            return row

    def close(self) -> None:
        self.engine.dispose()


def database_path(home: Path) -> Path:
    return Path(home) / "server" / "compute.sqlite3"
