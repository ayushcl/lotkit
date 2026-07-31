import concurrent.futures
import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

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
TRUSTED_PROXY = ("192.0.2.254", 53000)
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


def test_forged_forwarding_headers_do_not_create_new_application_bucket(
    isolated_persistence,
    monkeypatch: pytest.MonkeyPatch,
    throttle_clock,
) -> None:
    _set_owner_password(isolated_persistence["owner_id"])
    _use_fast_credentials(monkeypatch)
    with TestClient(app, client=CLIENT_A) as client:
        for _ in range(FAILURE_THRESHOLD - 1):
            response = _login(
                client,
                isolated_persistence,
                headers={"X-Forwarded-For": "203.0.113.1"},
            )
            assert response.status_code == 401
        eighth = _login(
            client,
            isolated_persistence,
            headers={
                "X-Forwarded-For": "203.0.113.200",
                "Forwarded": "for=198.51.100.99",
                "X-Real-IP": "192.0.2.99",
            },
        )

    assert eighth.status_code == 429
    assert len(_throttle_rows()) == 1
    assert _throttle_rows()[0]["client_key"] == client_key_for_peer(CLIENT_A[0])


def test_real_proxy_middleware_uses_first_untrusted_hop_not_forged_prefix() -> None:
    probe = FastAPI()

    @probe.get("/peer")
    def peer(request: Request) -> dict[str, str]:
        assert request.client is not None
        return {"host": request.client.host}

    explicit_trust = ProxyHeadersMiddleware(
        probe,
        trusted_hosts=[TRUSTED_PROXY[0]],
    )
    wildcard_trust = ProxyHeadersMiddleware(probe, trusted_hosts="*")
    forwarded_chain = "203.0.113.17, 198.51.100.42"

    with TestClient(explicit_trust, client=TRUSTED_PROXY) as client:
        explicit = client.get(
            "/peer",
            headers={"X-Forwarded-For": forwarded_chain},
        )
    with TestClient(wildcard_trust, client=TRUSTED_PROXY) as client:
        wildcard = client.get(
            "/peer",
            headers={"X-Forwarded-For": forwarded_chain},
        )

    assert explicit.json() == {"host": "198.51.100.42"}
    # Wildcard trust selects the leftmost, attacker-controlled entry, which is
    # why LotKit never uses "*" as its production default.
    assert wildcard.json() == {"host": "203.0.113.17"}


def test_real_proxy_middleware_prefix_changes_share_actual_client_bucket(
    isolated_persistence,
    monkeypatch: pytest.MonkeyPatch,
    throttle_clock,
) -> None:
    _set_owner_password(isolated_persistence["owner_id"])
    _use_fast_credentials(monkeypatch)
    proxied_app = ProxyHeadersMiddleware(
        app,
        trusted_hosts=[TRUSTED_PROXY[0]],
    )
    actual_client = "198.51.100.42"

    with TestClient(proxied_app, client=TRUSTED_PROXY) as client:
        responses = [
            _login(
                client,
                isolated_persistence,
                headers={
                    "X-Forwarded-For": f"203.0.113.{prefix}, {actual_client}"
                },
            )
            for prefix in range(1, FAILURE_THRESHOLD + 1)
        ]
        independent_client = _login(
            client,
            isolated_persistence,
            headers={
                "X-Forwarded-For": "203.0.113.99, 198.51.100.43"
            },
        )
        still_blocked = _login(
            client,
            isolated_persistence,
            headers={
                "X-Forwarded-For": "203.0.113.100, 198.51.100.42"
            },
            password=PASSWORD,
        )

    assert [response.status_code for response in responses] == [
        *([401] * (FAILURE_THRESHOLD - 1)),
        429,
    ]
    assert independent_client.status_code == 401
    assert still_blocked.status_code == 429
    assert {row["client_key"] for row in _throttle_rows()} == {
        client_key_for_peer("198.51.100.42"),
        client_key_for_peer("198.51.100.43"),
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
