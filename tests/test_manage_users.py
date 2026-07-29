import sqlite3
from datetime import datetime, timezone

import pytest

import api.manage_users
from api.auth import (
    create_session,
    get_or_create_default_owner,
    verify_password,
)
from api.db import connect_db

PASSWORD = "administrative password one"
NEW_PASSWORD = "administrative password two"


def _mock_password(
    monkeypatch: pytest.MonkeyPatch,
    password: str,
) -> None:
    answers = iter((password, password))
    monkeypatch.setattr(
        api.manage_users,
        "getpass",
        lambda _prompt: next(answers),
    )


def test_claim_default_preserves_id_and_all_owned_data(
    isolated_persistence,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id = isolated_persistence["owner_id"]
    connection = connect_db()
    try:
        connection.execute(
            """
            INSERT INTO dealership_profiles (id, owner_id, nickname)
            VALUES (41, ?, 'Claimed dealer')
            """,
            (owner_id,),
        )
        connection.execute(
            """
            INSERT INTO runs (
                run_id, owner_id, dealership_id, vin
            ) VALUES ('claimed-run', ?, 41, '1HGCM82633A004352')
            """,
            (owner_id,),
        )
        connection.execute(
            """
            INSERT INTO delivery_links (
                owner_id, run_id, public_id, token_hash, token_hint,
                artifact_manifest_json, created_utc, expires_utc,
                revoked_utc
            ) VALUES (
                ?, 'claimed-run', 'claimed-public', ?, 'claim1', '{}',
                '2026-01-01T00:00:00+00:00',
                '2026-02-01T00:00:00+00:00',
                '2026-01-02T00:00:00+00:00'
            )
            """,
            (owner_id, "a" * 64),
        )
        connection.commit()
    finally:
        connection.close()
    _mock_password(monkeypatch, PASSWORD)

    claimed_id = api.manage_users.claim_default(
        " Ayush@Example.com ",
        "Ayush Photographer",
    )

    assert claimed_id == owner_id
    connection = connect_db()
    try:
        user = connection.execute(
            "SELECT * FROM users WHERE id = ?",
            (owner_id,),
        ).fetchone()
        assert user["email"] == "ayush@example.com"
        assert user["display_name"] == "Ayush Photographer"
        assert verify_password(PASSWORD, user["password_hash"])
        assert connection.execute(
            "SELECT owner_id FROM dealership_profiles WHERE id = 41"
        ).fetchone()["owner_id"] == owner_id
        assert connection.execute(
            "SELECT owner_id FROM runs WHERE run_id = 'claimed-run'"
        ).fetchone()["owner_id"] == owner_id
        assert connection.execute(
            "SELECT owner_id FROM delivery_links WHERE public_id = ?",
            ("claimed-public",),
        ).fetchone()["owner_id"] == owner_id
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_bare_claim_default_command_prompts_for_identity_and_password(
    isolated_persistence,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    identity_answers = iter(("owner@example.com", "Owner Name"))
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: next(identity_answers),
    )
    _mock_password(monkeypatch, PASSWORD)

    assert api.manage_users.main(["claim-default"]) == 0
    output = capsys.readouterr().out
    assert "Claimed legacy account" in output
    assert PASSWORD not in output


def test_create_is_invite_only_normalizes_email_and_rejects_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_password(monkeypatch, PASSWORD)
    user_id = api.manage_users.create_user(
        " New.User@Example.COM ",
        "New User",
    )
    connection = connect_db()
    try:
        row = connection.execute(
            "SELECT * FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        assert row["email"] == "new.user@example.com"
        assert row["is_active"] == 1
        assert verify_password(PASSWORD, row["password_hash"])
    finally:
        connection.close()

    _mock_password(monkeypatch, PASSWORD)
    with pytest.raises(
        api.manage_users.UserManagementError,
        match="exists",
    ):
        api.manage_users.create_user(
            "NEW.USER@example.com",
            "Duplicate",
        )


def test_set_password_and_disable_revoke_all_sessions(
    isolated_persistence,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id = isolated_persistence["owner_id"]
    connection = connect_db()
    try:
        connection.execute(
            """
            UPDATE users
            SET email = ?, password_hash = ?
            WHERE id = ?
            """,
            (
                "session-owner@example.com",
                api.manage_users.hash_password(PASSWORD),
                owner_id,
            ),
        )
        create_session(connection, owner_id)
        create_session(connection, owner_id)
    finally:
        connection.close()

    _mock_password(monkeypatch, NEW_PASSWORD)
    api.manage_users.set_password("session-owner@example.com")
    connection = connect_db()
    try:
        assert connection.execute(
            """
            SELECT COUNT(*)
            FROM user_sessions
            WHERE user_id = ? AND revoked_utc IS NULL
            """,
            (owner_id,),
        ).fetchone()[0] == 0
        assert verify_password(
            NEW_PASSWORD,
            connection.execute(
                "SELECT password_hash FROM users WHERE id = ?",
                (owner_id,),
            ).fetchone()["password_hash"],
        )
        create_session(connection, owner_id)
    finally:
        connection.close()

    api.manage_users.set_active("session-owner@example.com", False)
    connection = connect_db()
    try:
        user = connection.execute(
            "SELECT is_active FROM users WHERE id = ?",
            (owner_id,),
        ).fetchone()
        assert user["is_active"] == 0
        assert connection.execute(
            """
            SELECT COUNT(*)
            FROM user_sessions
            WHERE user_id = ? AND revoked_utc IS NULL
            """,
            (owner_id,),
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_enable_does_not_invent_a_password(
    isolated_persistence,
) -> None:
    owner_id = isolated_persistence["owner_id"]
    connection = connect_db()
    try:
        connection.execute(
            """
            UPDATE users
            SET email = 'no-password@example.com',
                password_hash = NULL, is_active = 0
            WHERE id = ?
            """,
            (owner_id,),
        )
        connection.commit()
    finally:
        connection.close()

    api.manage_users.set_active("no-password@example.com", True)
    connection = connect_db()
    try:
        row = connection.execute(
            "SELECT is_active, password_hash FROM users WHERE id = ?",
            (owner_id,),
        ).fetchone()
        assert row["is_active"] == 1
        assert row["password_hash"] is None
    finally:
        connection.close()


def test_list_prints_only_safe_metadata(
    isolated_persistence,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_hash = api.manage_users.hash_password(PASSWORD)
    connection = connect_db()
    try:
        connection.execute(
            """
            UPDATE users
            SET email = 'list@example.com', password_hash = ?
            WHERE id = ?
            """,
            (secret_hash, isolated_persistence["owner_id"]),
        )
        connection.commit()
    finally:
        connection.close()

    assert api.manage_users.main(["list"]) == 0
    output = capsys.readouterr().out
    assert "list@example.com" in output
    assert "PASSWORD" in output
    assert secret_hash not in output
    assert "$argon2" not in output
    assert "session_hash" not in output


def test_cli_has_no_plaintext_password_argument_or_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = api.manage_users.main(
        [
            "set-password",
            "--email",
            "owner@local",
            "--password",
            "must-not-appear",
        ]
    )
    captured = capsys.readouterr()
    assert result == 2
    assert "must-not-appear" not in captured.out
    assert "must-not-appear" not in captured.err


def test_default_owner_helper_is_passwordless_test_data_only() -> None:
    connection = connect_db()
    try:
        owner_id = get_or_create_default_owner(connection)
        row = connection.execute(
            "SELECT password_hash FROM users WHERE id = ?",
            (owner_id,),
        ).fetchone()
    finally:
        connection.close()
    assert row["password_hash"] is None
