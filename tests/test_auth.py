import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.config
import api.main
from api.auth import (
    GENERIC_LOGIN_DETAIL,
    OWNER_CSRF_COOKIE,
    OWNER_SESSION_COOKIE,
    SESSION_LIFETIME_SECONDS,
    create_session,
    current_owner_id,
    hash_password,
    verify_password,
)
from api.db import connect_db
from api.main import app, create_app

PASSWORD = "correct horse battery staple"
OTHER_PASSWORD = "another very safe password"


def _set_owner_password(owner_id: int, password: str = PASSWORD) -> str:
    password_hash = hash_password(password)
    timestamp = datetime.now(timezone.utc).isoformat()
    connection = connect_db()
    try:
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
                password_hash,
                timestamp,
                timestamp,
                owner_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return password_hash


@pytest.fixture
def auth_client(isolated_persistence):
    app.dependency_overrides.pop(current_owner_id, None)
    _set_owner_password(isolated_persistence["owner_id"])
    with TestClient(app) as client:
        yield client


def _origin(isolated_persistence) -> dict[str, str]:
    return {"Origin": isolated_persistence["settings"].public_origin}


def _login(
    client: TestClient,
    isolated_persistence,
    *,
    email: str = "photographer@example.com",
    password: str = PASSWORD,
):
    return client.post(
        "/api/auth/login",
        headers=_origin(isolated_persistence),
        json={"email": email, "password": password},
    )


def _session_rows():
    connection = connect_db()
    try:
        return connection.execute(
            "SELECT * FROM user_sessions ORDER BY id"
        ).fetchall()
    finally:
        connection.close()


def test_passwords_are_argon2_hashes_and_verify_without_plaintext(
    isolated_persistence,
) -> None:
    password_hash = _set_owner_password(isolated_persistence["owner_id"])

    assert password_hash.startswith("$argon2")
    assert PASSWORD not in password_hash
    assert verify_password(PASSWORD, password_hash) is True
    assert verify_password("wrong password value", password_hash) is False

    connection = connect_db()
    try:
        stored = connection.execute(
            "SELECT password_hash FROM users WHERE id = ?",
            (isolated_persistence["owner_id"],),
        ).fetchone()["password_hash"]
    finally:
        connection.close()
    assert stored == password_hash
    assert stored != PASSWORD


@pytest.mark.parametrize("length", [0, 11, 257])
def test_password_policy_rejects_too_short_or_large_passwords(
    length: int,
) -> None:
    with pytest.raises(ValueError):
        hash_password("x" * length)


def test_successful_login_is_case_insensitive_and_creates_hashed_session(
    auth_client: TestClient,
    isolated_persistence,
) -> None:
    response = _login(
        auth_client,
        isolated_persistence,
        email="  PHOTOGRAPHER@EXAMPLE.COM  ",
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": isolated_persistence["owner_id"],
        "email": "photographer@example.com",
        "display_name": "Test Photographer",
    }
    assert response.headers["cache-control"] == "no-store"
    session_credential = response.cookies[OWNER_SESSION_COOKIE]
    csrf_credential = response.cookies[OWNER_CSRF_COOKIE]
    assert len(session_credential) == 43
    assert len(csrf_credential) == 43
    assert session_credential not in response.text
    assert csrf_credential not in response.text

    rows = _session_rows()
    assert len(rows) == 1
    assert rows[0]["session_hash"] == hashlib.sha256(
        session_credential.encode()
    ).hexdigest()
    assert rows[0]["csrf_hash"] == hashlib.sha256(
        csrf_credential.encode()
    ).hexdigest()
    assert session_credential not in tuple(rows[0])
    assert csrf_credential not in tuple(rows[0])


def test_owner_cookie_attributes_are_exact_in_local_http(
    auth_client: TestClient,
    isolated_persistence,
) -> None:
    response = _login(auth_client, isolated_persistence)
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(
        value
        for value in cookies
        if value.startswith(f"{OWNER_SESSION_COOKIE}=")
    )
    csrf_cookie = next(
        value
        for value in cookies
        if value.startswith(f"{OWNER_CSRF_COOKIE}=")
    )

    assert "HttpOnly" in session_cookie
    assert "Secure" not in session_cookie
    assert "SameSite=strict" in session_cookie
    assert "Path=/api" in session_cookie
    assert f"Max-Age={SESSION_LIFETIME_SECONDS}" in session_cookie
    assert "Domain=" not in session_cookie

    assert "HttpOnly" not in csrf_cookie
    assert "Secure" not in csrf_cookie
    assert "SameSite=strict" in csrf_cookie
    assert "Path=/" in csrf_cookie
    assert f"Max-Age={SESSION_LIFETIME_SECONDS}" in csrf_cookie
    assert "Domain=" not in csrf_cookie


def test_owner_cookies_are_secure_in_production(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOTKIT_ENV", "production")
    monkeypatch.setenv("LOTKIT_DATA_DIR", str(tmp_path / "production"))
    monkeypatch.setenv(
        "LOTKIT_PUBLIC_BASE_URL",
        "https://lotkit.example",
    )
    monkeypatch.setenv("LOTKIT_TRUSTED_HOSTS", "lotkit.example")
    api.config.reset_settings_cache()
    settings = api.config.get_settings()
    production_app = create_app(settings)

    with TestClient(
        production_app,
        base_url="https://lotkit.example",
    ) as client:
        connection = connect_db()
        try:
            timestamp = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """
                INSERT INTO users (
                    email, display_name, password_hash, created_utc,
                    updated_utc, password_changed_utc
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "secure@example.com",
                    "Secure User",
                    hash_password(PASSWORD),
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()
        finally:
            connection.close()
        response = client.post(
            "/api/auth/login",
            headers={"Origin": settings.public_origin},
            json={"email": "secure@example.com", "password": PASSWORD},
        )

    assert response.status_code == 200
    for cookie in response.headers.get_list("set-cookie"):
        assert "Secure" in cookie


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("unknown@example.com", PASSWORD),
        ("photographer@example.com", "wrong password value"),
    ],
)
def test_unknown_email_and_wrong_password_are_byte_equivalent(
    auth_client: TestClient,
    isolated_persistence,
    email: str,
    password: str,
) -> None:
    response = _login(
        auth_client,
        isolated_persistence,
        email=email,
        password=password,
    )
    assert response.status_code == 401
    assert response.content == (
        f'{{"detail":"{GENERIC_LOGIN_DETAIL}"}}'.encode()
    )
    assert response.headers["cache-control"] == "no-store"
    assert not response.headers.get_list("set-cookie")


def test_inactive_and_passwordless_users_use_generic_login_failure(
    auth_client: TestClient,
    isolated_persistence,
) -> None:
    connection = connect_db()
    try:
        timestamp = datetime.now(timezone.utc).isoformat()
        connection.executemany(
            """
            INSERT INTO users (
                email, display_name, password_hash, is_active, created_utc
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    "inactive@example.com",
                    "Inactive",
                    hash_password(PASSWORD),
                    0,
                    timestamp,
                ),
                (
                    "passwordless@example.com",
                    "Passwordless",
                    None,
                    1,
                    timestamp,
                ),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    failures = [
        _login(
            auth_client,
            isolated_persistence,
            email="inactive@example.com",
        ),
        _login(
            auth_client,
            isolated_persistence,
            email="passwordless@example.com",
        ),
    ]
    assert all(response.status_code == 401 for response in failures)
    assert failures[0].content == failures[1].content


@pytest.mark.parametrize("origin", [None, "null", "http://attacker.example"])
def test_login_requires_exact_origin(
    auth_client: TestClient,
    isolated_persistence,
    origin: str | None,
) -> None:
    headers = {} if origin is None else {"Origin": origin}
    response = auth_client.post(
        "/api/auth/login",
        headers=headers,
        json={
            "email": "photographer@example.com",
            "password": PASSWORD,
        },
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "Request could not be verified."}


def test_login_does_not_require_csrf(
    auth_client: TestClient,
    isolated_persistence,
) -> None:
    response = _login(auth_client, isolated_persistence)
    assert response.status_code == 200


def test_framework_generated_auth_errors_are_not_cacheable(
    auth_client: TestClient,
    isolated_persistence,
) -> None:
    response = auth_client.post(
        "/api/auth/login",
        headers=_origin(isolated_persistence),
        json={"email": "photographer@example.com"},
    )
    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"


def test_authenticated_get_succeeds_without_csrf_and_updates_last_seen(
    auth_client: TestClient,
    isolated_persistence,
) -> None:
    login = _login(auth_client, isolated_persistence)
    before = _session_rows()[0]["last_seen_utc"]

    response = auth_client.get("/api/auth/me")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["display_name"] == "Test Photographer"
    assert _session_rows()[0]["last_seen_utc"] >= before
    assert login.cookies[OWNER_CSRF_COOKIE]


def test_authenticated_unsafe_request_requires_csrf_and_exact_origin(
    auth_client: TestClient,
    isolated_persistence,
) -> None:
    login = _login(auth_client, isolated_persistence)
    csrf = login.cookies[OWNER_CSRF_COOKIE]
    payload = {"vin": "invalid"}

    missing = auth_client.post(
        "/api/decode",
        headers=_origin(isolated_persistence),
        json=payload,
    )
    wrong = auth_client.post(
        "/api/decode",
        headers={
            **_origin(isolated_persistence),
            "X-CSRF-Token": "wrong",
        },
        json=payload,
    )
    missing_origin = auth_client.post(
        "/api/decode",
        headers={"X-CSRF-Token": csrf},
        json=payload,
    )
    mismatched_origin = auth_client.post(
        "/api/decode",
        headers={
            "Origin": "http://attacker.example",
            "X-CSRF-Token": csrf,
        },
        json=payload,
    )
    accepted = auth_client.post(
        "/api/decode",
        headers={
            **_origin(isolated_persistence),
            "X-CSRF-Token": csrf,
        },
        json=payload,
    )

    assert {missing.status_code, wrong.status_code} == {403}
    assert missing_origin.status_code == 403
    assert mismatched_origin.status_code == 403
    assert accepted.status_code == 422


def test_expired_revoked_and_disabled_user_sessions_return_401(
    auth_client: TestClient,
    isolated_persistence,
) -> None:
    _login(auth_client, isolated_persistence)
    connection = connect_db()
    try:
        session_id = connection.execute(
            "SELECT id FROM user_sessions ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]
        connection.execute(
            "UPDATE user_sessions SET expires_utc = ? WHERE id = ?",
            (
                (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                session_id,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    assert auth_client.get("/api/auth/me").status_code == 401

    _login(auth_client, isolated_persistence)
    connection = connect_db()
    try:
        connection.execute(
            """
            UPDATE user_sessions
            SET revoked_utc = ?
            WHERE id = (SELECT MAX(id) FROM user_sessions)
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )
        connection.commit()
    finally:
        connection.close()
    assert auth_client.get("/api/auth/me").status_code == 401

    _login(auth_client, isolated_persistence)
    connection = connect_db()
    try:
        connection.execute(
            "UPDATE users SET is_active = 0 WHERE id = ?",
            (isolated_persistence["owner_id"],),
        )
        connection.commit()
    finally:
        connection.close()
    assert auth_client.get("/api/auth/me").status_code == 401


def test_logout_requires_csrf_revokes_only_current_session_and_clears_cookies(
    auth_client: TestClient,
    isolated_persistence,
) -> None:
    first = _login(auth_client, isolated_persistence)
    first_session = first.cookies[OWNER_SESSION_COOKIE]
    first_csrf = first.cookies[OWNER_CSRF_COOKIE]

    connection = connect_db()
    try:
        second = create_session(
            connection,
            isolated_persistence["owner_id"],
        )
    finally:
        connection.close()

    missing = auth_client.post(
        "/api/auth/logout",
        headers=_origin(isolated_persistence),
    )
    assert missing.status_code == 403

    response = auth_client.post(
        "/api/auth/logout",
        headers={
            **_origin(isolated_persistence),
            "X-CSRF-Token": first_csrf,
        },
    )
    assert response.status_code == 204
    cookies = response.headers.get_list("set-cookie")
    assert any(
        value.startswith(f"{OWNER_SESSION_COOKIE}=")
        and "Max-Age=0" in value
        and "Path=/api" in value
        for value in cookies
    )
    assert any(
        value.startswith(f"{OWNER_CSRF_COOKIE}=")
        and "Max-Age=0" in value
        and "Path=/" in value
        for value in cookies
    )

    connection = connect_db()
    try:
        rows = connection.execute(
            """
            SELECT session_hash, revoked_utc
            FROM user_sessions
            ORDER BY id
            """
        ).fetchall()
    finally:
        connection.close()
    first_hash = hashlib.sha256(first_session.encode()).hexdigest()
    second_hash = hashlib.sha256(
        second.session_credential.encode()
    ).hexdigest()
    assert next(
        row["revoked_utc"]
        for row in rows
        if row["session_hash"] == first_hash
    )
    assert next(
        row["revoked_utc"]
        for row in rows
        if row["session_hash"] == second_hash
    ) is None


def test_session_expiry_is_absolute_and_multiple_sessions_are_valid(
    auth_client: TestClient,
    isolated_persistence,
) -> None:
    first = _login(auth_client, isolated_persistence)
    first_expiry = _session_rows()[0]["expires_utc"]

    second_client = TestClient(app)
    second = _login(second_client, isolated_persistence)
    assert first.status_code == second.status_code == 200
    assert len(_session_rows()) == 2
    assert auth_client.get("/api/auth/me").status_code == 200
    assert second_client.get("/api/auth/me").status_code == 200
    assert _session_rows()[0]["expires_utc"] == first_expiry

    created = datetime.fromisoformat(_session_rows()[0]["created_utc"])
    expires = datetime.fromisoformat(first_expiry)
    assert (expires - created).total_seconds() == SESSION_LIFETIME_SECONDS
    second_client.close()


def test_representative_owner_routes_require_a_session(
    auth_client: TestClient,
) -> None:
    auth_client.cookies.clear()
    assert auth_client.get("/api/runs").status_code == 401
    assert auth_client.post(
        "/api/decode",
        json={"vin": "invalid"},
    ).status_code == 401


def test_health_readiness_root_and_recipient_routes_remain_public(
    auth_client: TestClient,
) -> None:
    auth_client.cookies.clear()
    assert auth_client.get("/health").status_code == 200
    assert auth_client.get("/ready").status_code == 200
    root = auth_client.get("/")
    assert root.status_code == 200
    assert 'id="login-form"' in root.text
    assert auth_client.get("/d/not-a-real-public-id").status_code == 200


def test_login_logs_do_not_contain_credentials(
    auth_client: TestClient,
    isolated_persistence,
    caplog: pytest.LogCaptureFixture,
) -> None:
    password = PASSWORD
    caplog.set_level("INFO", logger="uvicorn.error")
    login = _login(auth_client, isolated_persistence, password=password)
    session = login.cookies[OWNER_SESSION_COOKIE]
    csrf = login.cookies[OWNER_CSRF_COOKIE]

    output = "\n".join(record.getMessage() for record in caplog.records)
    assert password not in output
    assert session not in output
    assert csrf not in output
