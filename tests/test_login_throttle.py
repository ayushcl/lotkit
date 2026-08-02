import asyncio
import concurrent.futures
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
import uvicorn
from fastapi import Request
from fastapi.testclient import TestClient

import api.auth_routes
from api.auth import (
    GENERIC_LOGIN_DETAIL,
    OWNER_CSRF_COOKIE,
    OWNER_SESSION_COOKIE,
    hash_password,
)
from api.db import connect_db
from api.login_throttle import (
    BLOCK_DURATION,
    FAILURE_THRESHOLD,
    FAILURE_WINDOW,
    THROTTLED_LOGIN_DETAIL,
    begin_login_attempt,
    canonicalize_client_ip,
    client_key_for_peer,
    client_key_for_request,
    record_login_failure,
)
from api.main import app, create_app

PASSWORD = "correct horse battery staple"
WRONG_PASSWORD = "wrong password value"
CLIENT_A = ("192.0.2.10", 51000)
CLIENT_B = ("198.51.100.20", 52000)
BASE_TIME = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


def _set_owner_password(owner_id: int) -> None:
    connection = connect_db()
    try:
        timestamp = BASE_TIME.isoformat()
        connection.execute(
            """
            UPDATE users
            SET email = ?, display_name = ?, password_hash = ?,
                is_active = 1, updated_utc = ?, password_changed_utc = ?
            WHERE id = ?
            """,
            (
                "photographer@example.com",
                "Test Photographer",
                hash_password(PASSWORD),
                timestamp,
                timestamp,
                owner_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _login(
    client: TestClient,
    isolated_persistence,
    *,
    email: str = "photographer@example.com",
    password: str = WRONG_PASSWORD,
    headers: dict[str, str] | None = None,
):
    return client.post(
        "/api/auth/login",
        headers={
            "Origin": isolated_persistence["settings"].public_origin,
            **(headers or {}),
        },
        json={"email": email, "password": password},
    )


def _throttle_rows():
    connection = connect_db()
    try:
        return connection.execute(
            "SELECT * FROM login_throttle_buckets ORDER BY client_key"
        ).fetchall()
    finally:
        connection.close()


def _use_fast_credentials(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    checked_passwords: list[str] = []

    def authenticate(connection, email: str, password: str):
        checked_passwords.append(password)
        if (
            email.strip().casefold() != "photographer@example.com"
            or password != PASSWORD
        ):
            return None
        return connection.execute(
            """
            SELECT id, email, display_name, password_hash, is_active
            FROM users
            WHERE email = ? COLLATE NOCASE
            """,
            (email.strip(),),
        ).fetchone()

    monkeypatch.setattr(
        api.auth_routes,
        "authenticate_credentials",
        authenticate,
    )
    return checked_passwords


@pytest.fixture
def throttle_clock(monkeypatch: pytest.MonkeyPatch):
    clock = {"now": BASE_TIME}
    monkeypatch.setattr(
        api.auth_routes,
        "throttle_utc_now",
        lambda: clock["now"],
    )
    return clock


def test_failures_one_through_seven_are_generic_and_eighth_blocks_before_more_checks(
    isolated_persistence,
    monkeypatch: pytest.MonkeyPatch,
    throttle_clock,
) -> None:
    _set_owner_password(isolated_persistence["owner_id"])
    checked_passwords = _use_fast_credentials(monkeypatch)
    expected_401 = f'{{"detail":"{GENERIC_LOGIN_DETAIL}"}}'.encode()

    with TestClient(app, client=CLIENT_A) as client:
        first_seven = [
            _login(client, isolated_persistence)
            for _ in range(FAILURE_THRESHOLD - 1)
        ]
        eighth = _login(client, isolated_persistence)
        blocked = _login(client, isolated_persistence, password=PASSWORD)

    assert all(response.status_code == 401 for response in first_seven)
    assert all(response.content == expected_401 for response in first_seven)
    assert eighth.status_code == blocked.status_code == 429
    assert eighth.content == blocked.content == (
        f'{{"detail":"{THROTTLED_LOGIN_DETAIL}"}}'.encode()
    )
    assert eighth.headers["cache-control"] == "no-store"
    assert eighth.headers["retry-after"].isdigit()
    assert int(eighth.headers["retry-after"]) >= 1
    assert not eighth.headers.get_list("set-cookie")
    assert not blocked.headers.get_list("set-cookie")
    assert len(checked_passwords) == FAILURE_THRESHOLD

    rows = _throttle_rows()
    assert len(rows) == 1
    assert rows[0]["failure_count"] == FAILURE_THRESHOLD
    assert rows[0]["client_key"] == client_key_for_peer(CLIENT_A[0])
    assert CLIENT_A[0] not in rows[0]["client_key"]


def test_block_persists_across_application_and_testclient_instances(
    isolated_persistence,
    monkeypatch: pytest.MonkeyPatch,
    throttle_clock,
) -> None:
    _set_owner_password(isolated_persistence["owner_id"])
    checked_passwords = _use_fast_credentials(monkeypatch)
    with TestClient(app, client=CLIENT_A) as first_client:
        responses = [
            _login(first_client, isolated_persistence)
            for _ in range(FAILURE_THRESHOLD)
        ]
    assert responses[-1].status_code == 429

    replacement_app = create_app(isolated_persistence["settings"])
    with TestClient(replacement_app, client=CLIENT_A) as second_client:
        persisted = _login(second_client, isolated_persistence)

    assert persisted.status_code == 429
    assert len(checked_passwords) == FAILURE_THRESHOLD


def test_block_expiry_allows_a_clean_success(
    isolated_persistence,
    monkeypatch: pytest.MonkeyPatch,
    throttle_clock,
) -> None:
    _set_owner_password(isolated_persistence["owner_id"])
    _use_fast_credentials(monkeypatch)
    with TestClient(app, client=CLIENT_A) as client:
        for _ in range(FAILURE_THRESHOLD):
            response = _login(client, isolated_persistence)
        assert response.status_code == 429

        throttle_clock["now"] += BLOCK_DURATION
        allowed = _login(
            client,
            isolated_persistence,
            password=PASSWORD,
        )

    assert allowed.status_code == 200
    assert _throttle_rows() == []


def test_expired_partial_window_restarts_at_one_failure(
    isolated_persistence,
    monkeypatch: pytest.MonkeyPatch,
    throttle_clock,
) -> None:
    _set_owner_password(isolated_persistence["owner_id"])
    _use_fast_credentials(monkeypatch)
    with TestClient(app, client=CLIENT_A) as client:
        for _ in range(FAILURE_THRESHOLD - 1):
            assert _login(client, isolated_persistence).status_code == 401
        throttle_clock["now"] += FAILURE_WINDOW
        restarted = _login(client, isolated_persistence)

    assert restarted.status_code == 401
    row = _throttle_rows()[0]
    assert row["failure_count"] == 1
    assert row["window_start_utc"] == throttle_clock["now"].isoformat()


def test_success_before_threshold_clears_failures(
    isolated_persistence,
    monkeypatch: pytest.MonkeyPatch,
    throttle_clock,
) -> None:
    _set_owner_password(isolated_persistence["owner_id"])
    _use_fast_credentials(monkeypatch)
    with TestClient(app, client=CLIENT_A) as client:
        for _ in range(FAILURE_THRESHOLD - 1):
            assert _login(client, isolated_persistence).status_code == 401
        succeeded = _login(
            client,
            isolated_persistence,
            password=PASSWORD,
        )
        assert succeeded.status_code == 200
        assert _throttle_rows() == []
        after_success = [
            _login(client, isolated_persistence)
            for _ in range(FAILURE_THRESHOLD - 1)
        ]

    assert all(response.status_code == 401 for response in after_success)
    assert _throttle_rows()[0]["failure_count"] == FAILURE_THRESHOLD - 1


def test_ips_are_independent_and_throttling_is_not_an_account_lockout(
    isolated_persistence,
    monkeypatch: pytest.MonkeyPatch,
    throttle_clock,
) -> None:
    _set_owner_password(isolated_persistence["owner_id"])
    _use_fast_credentials(monkeypatch)
    with (
        TestClient(app, client=CLIENT_A) as first_client,
        TestClient(app, client=CLIENT_B) as second_client,
    ):
        for _ in range(FAILURE_THRESHOLD):
            blocked = _login(first_client, isolated_persistence)
        other_ip = _login(
            second_client,
            isolated_persistence,
            password=PASSWORD,
        )
        still_blocked = _login(
            first_client,
            isolated_persistence,
            password=PASSWORD,
        )

    assert blocked.status_code == 429
    assert other_ip.status_code == 200
    assert still_blocked.status_code == 429
    assert len(_throttle_rows()) == 1


def test_unknown_email_and_wrong_password_remain_byte_equivalent_below_threshold(
    isolated_persistence,
) -> None:
    _set_owner_password(isolated_persistence["owner_id"])
    with TestClient(app, client=CLIENT_A) as client:
        unknown = _login(
            client,
            isolated_persistence,
            email="unknown@example.com",
            password=PASSWORD,
        )
        wrong = _login(client, isolated_persistence)

    assert unknown.status_code == wrong.status_code == 401
    assert unknown.content == wrong.content
    assert not unknown.headers.get_list("set-cookie")
    assert not wrong.headers.get_list("set-cookie")


def test_peer_canonicalization_and_unavailable_bucket_are_deterministic() -> None:
    assert canonicalize_client_ip("192.0.2.10") == "192.0.2.10"
    assert canonicalize_client_ip("2001:0db8:0:0:0:0:0:1") == "2001:db8::1"
    assert client_key_for_peer("2001:0db8:0:0:0:0:0:1") == (
        client_key_for_peer("2001:db8::1")
    )
    assert client_key_for_peer("192.0.2.10") == client_key_for_peer(
        "192.0.2.10"
    )
    assert client_key_for_peer(None) == client_key_for_peer("not-an-ip")
    assert client_key_for_peer(None) != client_key_for_peer("192.0.2.10")
    assert len(client_key_for_peer("192.0.2.10")) == hashlib.sha256().digest_size * 2

    missing_scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/login",
        "headers": [],
        "client": None,
        "server": ("testserver", 80),
        "scheme": "http",
        "query_string": b"",
        "http_version": "1.1",
        "root_path": "",
    }
    malformed_scope = {**missing_scope, "client": ("not-an-ip", 1234)}
    assert client_key_for_request(Request(missing_scope)) == (
        client_key_for_request(Request(malformed_scope))
    )


def test_uvicorn_no_proxy_headers_preserves_the_direct_transport_peer() -> None:
    async def probe(scope, receive, send) -> None:
        body = json.dumps(
            {"host": scope["client"][0], "scheme": scope["scheme"]}
        ).encode()
        await send(
            {"type": "http.response.start", "status": 200, "headers": []}
        )
        await send({"type": "http.response.body", "body": body})

    config = uvicorn.Config(
        probe,
        proxy_headers=False,
        lifespan="off",
        access_log=False,
        log_level="warning",
    )
    config.load()
    assert config.proxy_headers is False
    sent_messages: list[dict] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/peer",
        "raw_path": b"/peer",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"x-forwarded-for", b"1.2.3.4, 5.6.7.8"),
            (b"x-forwarded-for", b"9.10.11.12"),
            (b"x-forwarded-proto", b"https"),
            (b"forwarded", b"for=13.14.15.16;proto=https"),
            (b"x-real-ip", b"17.18.19.20"),
            (b"true-client-ip", b"21.22.23.24"),
        ],
        "client": CLIENT_A,
        "server": ("127.0.0.1", 8000),
    }

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent_messages.append(message)

    assert config.loaded_app is not None
    asyncio.run(config.loaded_app(scope, receive, send))

    assert sent_messages[0]["status"] == 200
    assert json.loads(sent_messages[1]["body"]) == {
        "host": CLIENT_A[0],
        "scheme": "http",
    }


def test_different_forged_xff_values_share_one_direct_peer_bucket(
    isolated_persistence,
    monkeypatch: pytest.MonkeyPatch,
    throttle_clock,
) -> None:
    _set_owner_password(isolated_persistence["owner_id"])
    _use_fast_credentials(monkeypatch)
    with TestClient(app, client=CLIENT_A) as client:
        first = _login(
            client,
            isolated_persistence,
            headers={"X-Forwarded-For": "1.2.3.4"},
        )
        second = _login(
            client,
            isolated_persistence,
            headers={"X-Forwarded-For": "5.6.7.8"},
        )

    assert first.status_code == second.status_code == 401
    rows = _throttle_rows()
    assert len(rows) == 1
    assert rows[0]["failure_count"] == 2
    assert rows[0]["client_key"] == client_key_for_peer(CLIENT_A[0])


def test_repeated_and_multi_entry_forwarding_headers_cannot_create_buckets(
    isolated_persistence,
    monkeypatch: pytest.MonkeyPatch,
    throttle_clock,
) -> None:
    _set_owner_password(isolated_persistence["owner_id"])
    _use_fast_credentials(monkeypatch)
    origin = isolated_persistence["settings"].public_origin

    with TestClient(app, client=CLIENT_A) as client:
        responses = []
        for suffix in range(2):
            responses.append(
                client.post(
                    "/api/auth/login",
                    headers=[
                        ("Origin", origin),
                        (
                            "X-Forwarded-For",
                            f"203.0.113.{suffix}, 198.51.100.{suffix}",
                        ),
                        ("X-Forwarded-For", f"192.0.2.{suffix}"),
                        ("Forwarded", f"for=198.18.0.{suffix}"),
                        ("X-Real-IP", f"198.19.0.{suffix}"),
                        ("True-Client-IP", f"198.20.0.{suffix}"),
                    ],
                    json={
                        "email": "photographer@example.com",
                        "password": WRONG_PASSWORD,
                    },
                )
            )

    assert [response.status_code for response in responses] == [401, 401]
    rows = _throttle_rows()
    assert len(rows) == 1
    assert rows[0]["failure_count"] == 2
    assert rows[0]["client_key"] == client_key_for_peer(CLIENT_A[0])


def test_distinct_direct_transport_peers_create_distinct_buckets(
    isolated_persistence,
    monkeypatch: pytest.MonkeyPatch,
    throttle_clock,
) -> None:
    _set_owner_password(isolated_persistence["owner_id"])
    _use_fast_credentials(monkeypatch)

    with (
        TestClient(app, client=CLIENT_A) as first_client,
        TestClient(app, client=CLIENT_B) as second_client,
    ):
        first = _login(
            first_client,
            isolated_persistence,
            headers={"X-Forwarded-For": "203.0.113.10"},
        )
        second = _login(
            second_client,
            isolated_persistence,
            headers={"X-Forwarded-For": "203.0.113.10"},
        )

    assert first.status_code == second.status_code == 401
    assert {row["client_key"] for row in _throttle_rows()} == {
        client_key_for_peer(CLIENT_A[0]),
        client_key_for_peer(CLIENT_B[0]),
    }


def test_stale_unblocked_buckets_are_deleted_opportunistically(
    isolated_persistence,
    monkeypatch: pytest.MonkeyPatch,
    throttle_clock,
) -> None:
    _set_owner_password(isolated_persistence["owner_id"])
    _use_fast_credentials(monkeypatch)
    stale_time = BASE_TIME - timedelta(days=2)
    connection = connect_db()
    try:
        connection.execute(
            """
            INSERT INTO login_throttle_buckets (
                client_key, failure_count, window_start_utc,
                blocked_until_utc, updated_utc
            ) VALUES (?, 1, ?, NULL, ?)
            """,
            (
                client_key_for_peer(CLIENT_B[0]),
                stale_time.isoformat(),
                stale_time.isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with TestClient(app, client=CLIENT_A) as client:
        assert _login(client, isolated_persistence).status_code == 401

    rows = _throttle_rows()
    assert len(rows) == 1
    assert rows[0]["client_key"] == client_key_for_peer(CLIENT_A[0])


def test_concurrent_failure_updates_reach_threshold_without_lost_counts(
    isolated_persistence,
) -> None:
    client_key = client_key_for_peer(CLIENT_A[0])

    def fail_once() -> bool:
        connection = connect_db()
        try:
            decision = begin_login_attempt(
                connection,
                client_key,
                now=BASE_TIME,
            )
            assert decision is None
            decision = record_login_failure(
                connection,
                client_key,
                now=BASE_TIME,
            )
            connection.commit()
            return decision is not None
        finally:
            connection.close()

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=FAILURE_THRESHOLD
    ) as executor:
        results = list(
            executor.map(lambda _: fail_once(), range(FAILURE_THRESHOLD))
        )

    assert results.count(True) == 1
    row = _throttle_rows()[0]
    assert row["failure_count"] == FAILURE_THRESHOLD


def test_failed_and_throttled_logs_exclude_sensitive_request_values(
    isolated_persistence,
    monkeypatch: pytest.MonkeyPatch,
    throttle_clock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _set_owner_password(isolated_persistence["owner_id"])
    _use_fast_credentials(monkeypatch)
    email = "secret-person@example.com"
    password = "secret-password-value"
    forwarded = "203.0.113.177"
    cookie = "sensitive-cookie-value"
    csrf = "sensitive-csrf-value"
    caplog.set_level("INFO", logger="uvicorn.error")

    with TestClient(app, client=CLIENT_A) as client:
        for _ in range(FAILURE_THRESHOLD):
            response = _login(
                client,
                isolated_persistence,
                email=email,
                password=password,
                headers={
                    "X-Forwarded-For": forwarded,
                    "Cookie": f"{OWNER_SESSION_COOKIE}={cookie}; "
                    f"{OWNER_CSRF_COOKIE}={csrf}",
                },
            )

    output = "\n".join(record.getMessage() for record in caplog.records)
    assert response.status_code == 429
    assert "Owner login failed." in output
    assert "Owner login throttled." in output
    for secret in (
        email,
        password,
        CLIENT_A[0],
        forwarded,
        cookie,
        csrf,
        client_key_for_peer(CLIENT_A[0]),
    ):
        assert secret not in output
