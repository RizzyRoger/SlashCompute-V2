"""The engine's :class:`~compute.engine.accounting.Accounting`, backed by the server's accounts and FLOP ledger."""

from __future__ import annotations

from typing import Optional

from fastapi import Request

from compute.engine.accounting import AccountingError
from compute.server.auth import Auth, accepted_terms, is_admin, request_token
from compute.server.db import User
from compute.server.ledger import Ledger, LedgerError


def node_owner_or_error(auth: Auth, session_token: Optional[str]) -> User:
    """The account a registering node belongs to; refuses unknown, banned or terms-pending accounts."""
    user = auth.session_user(session_token)
    if user is None:
        raise AccountingError("invalid or expired session; sign in again", 401)
    if user.banned:
        raise AccountingError("This account is banned.", 403)
    if not accepted_terms(user):
        raise AccountingError("terms_required", 403)
    return user


class ServerAccounting:
    def __init__(self, auth: Auth, ledger: Ledger) -> None:
        self.auth, self.ledger = auth, ledger

    def requester(self, request: Request) -> Optional[str]:
        user = self.auth.session_user(request_token(request))
        if user is None:
            raise AccountingError("Sign in first.", 401)
        if user.banned:
            raise AccountingError("This account is banned.", 403)
        return user.id

    def bind_node(self, node_id: str, session_token: Optional[str]) -> None:
        self.ledger.bind_node(node_id, node_owner_or_error(self.auth, session_token).id)

    def reserve(self, user_id: str, account_id: str, flops: float, session_id: Optional[str] = None) -> None:
        user = self.auth.get(user_id)
        if user is None:
            raise AccountingError("Sign in first.", 401)
        try:
            self.ledger.reserve(user_id, account_id, flops, unlimited=is_admin(user), session_id=session_id)
        except LedgerError as e:
            raise AccountingError(str(e), e.status) from e

    def record(self, account_id: str, per_node: dict[str, float], tokens: int, wall_s: float) -> None:
        self.ledger.record(account_id, per_node)

    def settle(self, account_id: str) -> None:
        self.ledger.settle(account_id)
