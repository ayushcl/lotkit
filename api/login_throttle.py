"""Persistent, IP-scoped throttling for owner login attempts."""

from __future__ import annotations

import hashlib
import ipaddress
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Request

FAILURE_THRESHOLD = 8
FAILURE_WINDOW = timedelta(minutes=15)
BLOCK_DURATION = timedelta(minutes=15)
STALE_BUCKET_AGE = timedelta(days=1)
THROTTLED_LOGIN_DETAIL = "Too many login attempts. Try again later."

_CLIENT_KEY_CONTEXT = b"lotkit-owner-login-ip\0"
_UNAVAILABLE_PEER = b"unavailable"


@dataclass(frozen=True, slots=True)
class ThrottleDecision:
    """The result of checking or recording one login attempt."""

    retry_after_seconds: int


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def canonicalize_client_ip(value: object) -> str | None:
    """Return a canonical IP literal, or ``None`` for an unsafe peer value."""

    if not isinstance(value, str) or not value or "%" in value:
        return None
    try:
        return ipaddress.ip_address(value).compressed
    except ValueError:
        return None


def client_key_for_peer(value: object) -> str:
    """Derive a deterministic non-reversible bucket key from a peer IP."""

    canonical = canonicalize_client_ip(value)
    material = (
        canonical.encode("ascii") if canonical is not None else _UNAVAILABLE_PEER
    )
    return hashlib.sha256(_CLIENT_KEY_CONTEXT + material).hexdigest()


def client_key_for_request(request: Request) -> str:
    """Use only the ASGI peer after the server's proxy-trust processing."""

    peer = request.client
    host = peer.host if peer is not None else None
    return client_key_for_peer(host)


def _parse_utc(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _retry_after(blocked_until: datetime, now: datetime) -> int:
    return max(1, math.ceil((blocked_until - now).total_seconds()))


def _delete_stale_buckets(
    connection: sqlite3.Connection,
    *,
    now: datetime,
) -> None:
    connection.execute(
        """
        DELETE FROM login_throttle_buckets
        WHERE updated_utc <= ?
          AND (blocked_until_utc IS NULL OR blocked_until_utc <= ?)
        """,
        (
            (now - STALE_BUCKET_AGE).isoformat(),
            now.isoformat(),
        ),
    )


def begin_login_attempt(
    connection: sqlite3.Connection,
    client_key: str,
    *,
    now: datetime | None = None,
) -> ThrottleDecision | None:
    """Begin a serialized attempt and reject an already-blocked client.

    The caller must commit or roll back the transaction. Keeping this
    ``BEGIN IMMEDIATE`` transaction open until the credential result is
    recorded prevents concurrent failures from bypassing the threshold.
    """

    checked_at = (now or utc_now()).astimezone(timezone.utc)
    connection.execute("BEGIN IMMEDIATE")
    _delete_stale_buckets(connection, now=checked_at)
    row = connection.execute(
        """
        SELECT failure_count, window_start_utc, blocked_until_utc
        FROM login_throttle_buckets
        WHERE client_key = ?
        """,
        (client_key,),
    ).fetchone()
    if row is None:
        return None

    blocked_until = _parse_utc(row["blocked_until_utc"])
    if blocked_until is not None and blocked_until > checked_at:
        return ThrottleDecision(
            retry_after_seconds=_retry_after(blocked_until, checked_at)
        )

    window_start = _parse_utc(row["window_start_utc"])
    window_is_current = (
        window_start is not None
        and window_start <= checked_at
        and checked_at - window_start < FAILURE_WINDOW
        and blocked_until is None
    )
    if not window_is_current:
        connection.execute(
            "DELETE FROM login_throttle_buckets WHERE client_key = ?",
            (client_key,),
        )
    return None


def record_login_failure(
    connection: sqlite3.Connection,
    client_key: str,
    *,
    now: datetime | None = None,
) -> ThrottleDecision | None:
    """Record a failed credential check in the current login transaction."""

    failed_at = (now or utc_now()).astimezone(timezone.utc)
    row = connection.execute(
        """
        SELECT failure_count, window_start_utc
        FROM login_throttle_buckets
        WHERE client_key = ?
        """,
        (client_key,),
    ).fetchone()
    if row is None:
        failure_count = 1
        window_start = failed_at
    else:
        stored_start = _parse_utc(row["window_start_utc"])
        if (
            stored_start is None
            or stored_start > failed_at
            or failed_at - stored_start >= FAILURE_WINDOW
        ):
            failure_count = 1
            window_start = failed_at
        else:
            failure_count = int(row["failure_count"]) + 1
            window_start = stored_start

    blocked_until: datetime | None = None
    if failure_count >= FAILURE_THRESHOLD:
        failure_count = FAILURE_THRESHOLD
        blocked_until = failed_at + BLOCK_DURATION

    connection.execute(
        """
        INSERT INTO login_throttle_buckets (
            client_key, failure_count, window_start_utc,
            blocked_until_utc, updated_utc
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(client_key) DO UPDATE SET
            failure_count = excluded.failure_count,
            window_start_utc = excluded.window_start_utc,
            blocked_until_utc = excluded.blocked_until_utc,
            updated_utc = excluded.updated_utc
        """,
        (
            client_key,
            failure_count,
            window_start.isoformat(),
            blocked_until.isoformat() if blocked_until is not None else None,
            failed_at.isoformat(),
        ),
    )
    if blocked_until is None:
        return None
    return ThrottleDecision(
        retry_after_seconds=_retry_after(blocked_until, failed_at)
    )


def clear_login_failures(
    connection: sqlite3.Connection,
    client_key: str,
) -> None:
    """Clear an IP bucket after a successful pre-threshold login."""

    connection.execute(
        "DELETE FROM login_throttle_buckets WHERE client_key = ?",
        (client_key,),
    )
