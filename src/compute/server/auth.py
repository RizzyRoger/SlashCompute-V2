"""Email + password accounts and bearer/cookie sessions. Only ``config.admin_email()`` is ever admin."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Optional

from fastapi import Request
from sqlmodel import select

from compute.common.config import admin_email
from compute.server.db import Database, SessionRow, User
from compute.server.terms import terms_version

ITERATIONS = 210_000
SESSION_TTL_S = 30 * 24 * 3600
COOKIE = "compute_session"


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"{ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        iters_s, salt_hex, dk_hex = stored.split("$", 2)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters_s))
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def is_admin(user: Optional[User]) -> bool:
    return user is not None and normalize_email(user.email) == admin_email()


def accepted_terms(user: User) -> bool:
    return user.terms_version is not None and user.terms_version == terms_version()


def request_token(request: Request) -> Optional[str]:
    """Bearer token (desktop app, nodes) or the ``compute_session`` cookie (admin page)."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return request.cookies.get(COOKIE) or None


def user_view(user: User) -> dict:
    return {
        "id": user.id, "email": user.email, "name": user.name, "is_admin": is_admin(user),
        "banned": user.banned, "flagged": user.flagged, "accepted_terms": accepted_terms(user),
        "terms_version": user.terms_version,
    }


def _check_email(email: str) -> None:
    if "@" not in email or "." not in email.split("@")[-1]:
        raise AuthError("Enter a real email address.")


def _check_password(password: str) -> None:
    if len(password or "") < 8:
        raise AuthError("Password must be at least 8 characters.")


def _clean_name(name: Optional[str], email: str) -> str:
    return ((name or "").strip() or email.split("@")[0])[:80] or "member"


class Auth:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, user_id: str) -> Optional[User]:
        return self.db.get(User, user_id)

    def get_by_email(self, email: str) -> Optional[User]:
        with self.db.session() as s:
            return s.exec(select(User).where(User.email == normalize_email(email))).first()

    def all_users(self) -> list[User]:
        with self.db.session() as s:
            return list(s.exec(select(User).order_by(User.created_at)).all())

    def _create(self, email: str, password: str, name: Optional[str]) -> User:
        user = User(id=secrets.token_hex(8), email=email, password_hash=hash_password(password),
                    name=_clean_name(name, email))
        self.db.add(user)
        return user

    def register(self, email: str, password: str, name: Optional[str]) -> User:
        email = normalize_email(email)
        _check_email(email)
        _check_password(password)
        if self.get_by_email(email) is not None:
            raise AuthError("That email is already registered.", 409)
        if email == admin_email():
            raise AuthError("That email is reserved for the server admin.", 403)
        return self._create(email, password, name)

    def ensure_admin(self, email: str, password: str) -> User:
        """First start creates the admin with ``password``; later starts must match it."""
        email = normalize_email(email)
        if email != admin_email():
            raise AuthError(f"Only {admin_email()} can run the main server.", 403)
        _check_password(password)
        user = self.get_by_email(email)
        if user is None:
            return self._create(email, password, "admin")
        if not verify_password(password, user.password_hash):
            raise AuthError("Wrong admin password.", 401)
        return user

    def login(self, email: str, password: str) -> tuple[User, str]:
        user = self.get_by_email(email)
        if user is None or not verify_password(password or "", user.password_hash):
            raise AuthError("Email or password is wrong.", 401)
        if user.banned:
            raise AuthError("This account is banned.", 403)
        return user, self.issue(user)

    def issue(self, user: User) -> str:
        token = secrets.token_urlsafe(32)
        self.db.add(SessionRow(token_hash=token_hash(token), user_id=user.id, expires_at=time.time() + SESSION_TTL_S))
        return token

    def logout(self, token: Optional[str]) -> None:
        if not token:
            return
        with self.db.session() as s:
            row = s.get(SessionRow, token_hash(token))
            if row is not None:
                s.delete(row)
                s.commit()

    def session_user(self, token: Optional[str]) -> Optional[User]:
        """User for this token, banned or not. Expired or unknown -> None."""
        if not token:
            return None
        with self.db.session() as s:
            row = s.get(SessionRow, token_hash(token))
            if row is None or row.expires_at < time.time():
                return None
            return s.get(User, row.user_id)

    def accept_terms(self, user: User, version: str) -> User:
        if version != terms_version():
            raise AuthError("Those are not the current terms; reload them.", 409)
        user.terms_version, user.terms_accepted_at = version, time.time()
        self.db.save(user)
        return user

    def rename(self, user: User, name: Optional[str]) -> User:
        clean = (name or "").strip()[:80]
        if not clean:
            raise AuthError("Name cannot be empty.")
        user.name = clean
        self.db.save(user)
        return user

    def set_banned(self, user: User, banned: bool) -> User:
        user.banned = bool(banned)
        self.db.save(user)
        return user

    def set_flagged(self, user: User, flagged: bool, reason: Optional[str]) -> User:
        user.flagged = bool(flagged)
        user.flag_reason = ((reason or "").strip()[:500] or None) if flagged else None
        self.db.save(user)
        return user
