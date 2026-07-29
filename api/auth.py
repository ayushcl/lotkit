"""Invite-only owner authentication and server-side photographer sessions."""

from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request
from pwdlib import PasswordHash

from api.db import connect_db

DEFAULT_OWNER_EMAIL = "owner@local"
DEFAULT_OWNER_DISPLAY_NAME = "Owner"
OWNER_SESSION_COOKIE = "lotkit_owner_session"
OWNER_CSRF_COOKIE = "lotkit_owner_csrf"
SESSION_LIFETIME_SECONDS = 43_200
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256
GENERIC_AUTHENTICATION_DETAIL = "Authentication required."
GENERIC_LOGIN_DETAIL = "Invalid email or password."
GENERIC_REQUEST_DETAIL = "Request could not be verified."
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
PASSWORD_HASH = PasswordHash.recommended()
_DUMMY_PASSWORD = "lotkit-dummy-password-never-used"
_DUMMY_PASSWORD_HASH = PASSWORD_HASH.hash(_DUMMY_PASSWORD)
LOGGER = logging.getLogger("uvicorn.error")


class PasswordValidationError(ValueError):
    """Raised when an administrative password does not meet policy."""


@dataclass(frozen=True, slots=True)
class CreatedSession:
    id: int
    user_id: int
    session_credential: str
    csrf_credential: str
    created_utc: str
    expires_utc: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordValidationError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordValidationError(
            f"Password must be at most {MAX_PASSWORD_LENGTH} characters."
        )


def hash_password(password: str) -> str:
    validate_password(password)
    return PASSWORD_HASH.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bool(PASSWORD_HASH.verify(password, password_hash))
    except Exception:
        return False


def hash_credential(credential: str) -> str:
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def _generic_unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=GENERIC_AUTHENTICATION_DETAIL,
        headers={"Cache-Control": "no-store"},
    )


def _generic_forbidden() -> HTTPException:
    return HTTPException(
        status_code=403,
        detail=GENERIC_REQUEST_DETAIL,
        headers={"Cache-Control": "no-store"},
    )


def require_exact_origin(request: Request) -> None:
    supplied = request.headers.get("origin")
    expected = request.app.state.settings.public_origin
    if (
        supplied is None
        or supplied == "null"
        or not secrets.compare_digest(supplied, expected)
    ):
        raise _generic_forbidden()


def authenticate_credentials(
    connection: sqlite3.Connection,
    email: str,
    password: str,
) -> sqlite3.Row | None:
    normalized = normalize_email(email)
    user = connection.execute(
        """
        SELECT id, email, display_name, password_hash, is_active
        FROM users
        WHERE email = ? COLLATE NOCASE
        """,
        (normalized,),
    ).fetchone()

    candidate_hash = (
        str(user["password_hash"])
        if user is not None and user["password_hash"]
        else _DUMMY_PASSWORD_HASH
    )
    password_matches = verify_password(password, candidate_hash)
    if (
        user is None
        or not user["password_hash"]
        or int(user["is_active"]) != 1
        or not password_matches
    ):
        return None
    return user


def create_session(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    now: datetime | None = None,
) -> CreatedSession:
    created = now or utc_now()
    expires = created + timedelta(seconds=SESSION_LIFETIME_SECONDS)
    session_credential = secrets.token_urlsafe(32)
    csrf_credential = secrets.token_urlsafe(32)
    created_utc = created.isoformat()
    expires_utc = expires.isoformat()
    cursor = connection.execute(
        """
        INSERT INTO user_sessions (
            user_id, session_hash, csrf_hash, created_utc, expires_utc,
            last_seen_utc
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            hash_credential(session_credential),
            hash_credential(csrf_credential),
            created_utc,
            expires_utc,
            created_utc,
        ),
    )
    connection.commit()
    return CreatedSession(
        id=int(cursor.lastrowid),
        user_id=user_id,
        session_credential=session_credential,
        csrf_credential=csrf_credential,
        created_utc=created_utc,
        expires_utc=expires_utc,
    )


def revoke_session(
    connection: sqlite3.Connection,
    session_id: int,
    *,
    now: datetime | None = None,
) -> None:
    connection.execute(
        """
        UPDATE user_sessions
        SET revoked_utc = COALESCE(revoked_utc, ?)
        WHERE id = ?
        """,
        ((now or utc_now()).isoformat(), session_id),
    )
    connection.commit()


def revoke_all_user_sessions(
    connection: sqlite3.Connection,
    user_id: int,
    *,
    now: datetime | None = None,
) -> None:
    connection.execute(
        """
        UPDATE user_sessions
        SET revoked_utc = ?
        WHERE user_id = ? AND revoked_utc IS NULL
        """,
        ((now or utc_now()).isoformat(), user_id),
    )


def _resolve_session(
    connection: sqlite3.Connection,
    credential: str,
    *,
    now: datetime,
) -> sqlite3.Row | None:
    supplied_hash = hash_credential(credential)
    row = connection.execute(
        """
        SELECT
            user_sessions.id AS session_id,
            user_sessions.user_id,
            user_sessions.session_hash,
            user_sessions.csrf_hash,
            user_sessions.expires_utc,
            users.email,
            users.display_name
        FROM user_sessions
        JOIN users ON users.id = user_sessions.user_id
        WHERE user_sessions.session_hash = ?
          AND user_sessions.revoked_utc IS NULL
          AND user_sessions.expires_utc > ?
          AND users.is_active = 1
        """,
        (supplied_hash, now.isoformat()),
    ).fetchone()
    if row is None or not secrets.compare_digest(
        str(row["session_hash"]),
        supplied_hash,
    ):
        return None
    return row


def _require_csrf(request: Request, session: sqlite3.Row) -> None:
    require_exact_origin(request)
    supplied = request.headers.get("x-csrf-token")
    if not supplied:
        raise _generic_forbidden()
    supplied_hash = hash_credential(supplied)
    if not secrets.compare_digest(
        supplied_hash,
        str(session["csrf_hash"]),
    ):
        raise _generic_forbidden()


def get_or_create_default_owner(connection: sqlite3.Connection) -> int:
    """Legacy test-data helper; never used by runtime request/startup paths."""

    owner = connection.execute(
        "SELECT id FROM users WHERE email = ? COLLATE NOCASE",
        (DEFAULT_OWNER_EMAIL,),
    ).fetchone()
    if owner is None:
        timestamp = utc_now().isoformat()
        connection.execute(
            """
            INSERT OR IGNORE INTO users (
                email, display_name, created_utc, updated_utc
            ) VALUES (?, ?, ?, ?)
            """,
            (
                DEFAULT_OWNER_EMAIL,
                DEFAULT_OWNER_DISPLAY_NAME,
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
        owner = connection.execute(
            "SELECT id FROM users WHERE email = ? COLLATE NOCASE",
            (DEFAULT_OWNER_EMAIL,),
        ).fetchone()

    if owner is None:
        raise RuntimeError("Could not create the default LotKit owner.")
    return int(owner[0])


def current_owner_id(request: Request) -> int:
    """Resolve the authenticated photographer while preserving the CRUD seam."""

    credential = request.cookies.get(OWNER_SESSION_COOKIE)
    if not credential or len(credential) != 43:
        raise _generic_unauthorized()

    now = utc_now()
    connection = connect_db()
    try:
        session = _resolve_session(connection, credential, now=now)
        if session is None:
            raise _generic_unauthorized()
        if request.method.upper() not in SAFE_METHODS:
            _require_csrf(request, session)

        connection.execute(
            """
            UPDATE user_sessions
            SET last_seen_utc = ?
            WHERE id = ?
              AND revoked_utc IS NULL
              AND expires_utc > ?
            """,
            (now.isoformat(), session["session_id"], now.isoformat()),
        )
        connection.commit()
        request.state.owner_session_id = int(session["session_id"])
        request.state.owner_user = {
            "id": int(session["user_id"]),
            "email": str(session["email"]),
            "display_name": session["display_name"],
        }
        return int(session["user_id"])
    finally:
        connection.close()
