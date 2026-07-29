import sqlite3
from pathlib import Path

import pytest

from api.db import DatabaseMigrationError, connect_db, init_db


def _create_phase_5a_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            PRAGMA foreign_keys = ON;

            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                email TEXT UNIQUE,
                display_name TEXT,
                created_utc TEXT
            );

            CREATE TABLE dealership_profiles (
                id INTEGER PRIMARY KEY,
                owner_id INTEGER NOT NULL REFERENCES users(id),
                nickname TEXT NOT NULL,
                dealership_name TEXT,
                address TEXT,
                phone TEXT,
                email TEXT,
                complaints_contact TEXT,
                sticker_footer_text TEXT,
                logo_path TEXT,
                notes TEXT,
                created_utc TEXT,
                updated_utc TEXT
            );

            CREATE TABLE runs (
                id INTEGER PRIMARY KEY,
                run_id TEXT UNIQUE NOT NULL,
                owner_id INTEGER NOT NULL REFERENCES users(id),
                dealership_id INTEGER
                    REFERENCES dealership_profiles(id) ON DELETE SET NULL,
                dealership_snapshot_json TEXT NOT NULL DEFAULT '{}',
                vin TEXT NOT NULL,
                vehicle_json TEXT,
                price TEXT,
                exterior_colour TEXT,
                interior_colour TEXT,
                photo_order_json TEXT,
                outputs_json TEXT,
                status TEXT NOT NULL DEFAULT 'in_progress',
                created_utc TEXT,
                updated_utc TEXT
            );

            CREATE TABLE delivery_links (
                id INTEGER PRIMARY KEY,
                owner_id INTEGER NOT NULL REFERENCES users(id),
                run_id TEXT NOT NULL
                    REFERENCES runs(run_id) ON DELETE CASCADE,
                public_id TEXT,
                token_hash TEXT NOT NULL UNIQUE,
                token_hint TEXT NOT NULL,
                artifact_manifest_json TEXT NOT NULL,
                created_utc TEXT NOT NULL,
                expires_utc TEXT NOT NULL,
                first_opened_utc TEXT,
                first_download_started_utc TEXT,
                revoked_utc TEXT,
                revocation_reason TEXT
            );

            INSERT INTO users (
                id, email, display_name, created_utc
            ) VALUES (
                17, 'owner@local', 'Owner', '2026-01-01T00:00:00+00:00'
            );

            INSERT INTO dealership_profiles (
                id, owner_id, nickname
            ) VALUES (23, 17, 'Legacy dealer');

            INSERT INTO runs (
                id, run_id, owner_id, dealership_id, vin
            ) VALUES (
                29, 'legacy-run', 17, 23, '1HGCM82633A004352'
            );

            INSERT INTO delivery_links (
                id, owner_id, run_id, public_id, token_hash, token_hint,
                artifact_manifest_json, created_utc, expires_utc,
                revoked_utc
            ) VALUES (
                31, 17, 'legacy-run', 'legacy-public',
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'legacy', '{}',
                '2026-01-01T00:00:00+00:00',
                '2026-02-01T00:00:00+00:00',
                '2026-01-02T00:00:00+00:00'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_fresh_auth_schema_has_constraints_sessions_and_no_seeded_user(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fresh.db"
    init_db(path)
    connection = connect_db(path)
    try:
        columns = {
            row["name"]: row
            for row in connection.execute("PRAGMA table_info(users)")
        }
        assert {
            "id",
            "email",
            "display_name",
            "password_hash",
            "is_active",
            "created_utc",
            "updated_utc",
            "password_changed_utc",
        } <= set(columns)
        assert columns["email"]["notnull"] == 1
        assert columns["is_active"]["notnull"] == 1
        assert columns["is_active"]["dflt_value"] == "1"
        assert connection.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0] == 0

        session_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(user_sessions)")
        }
        assert session_columns == {
            "id",
            "user_id",
            "session_hash",
            "csrf_hash",
            "created_utc",
            "expires_utc",
            "last_seen_utc",
            "revoked_utc",
        }
        indexes = {
            row["name"]
            for row in connection.execute(
                "PRAGMA index_list(user_sessions)"
            )
        }
        assert {
            "user_sessions_session_hash_idx",
            "user_sessions_user_id_idx",
            "user_sessions_expires_utc_idx",
        } <= indexes
        foreign_keys = {
            row["from"]: (row["table"], row["to"], row["on_delete"])
            for row in connection.execute(
                "PRAGMA foreign_key_list(user_sessions)"
            )
        }
        assert foreign_keys["user_id"] == ("users", "id", "CASCADE")
    finally:
        connection.close()


def test_phase_5a_migration_is_additive_idempotent_and_preserves_ownership(
    tmp_path: Path,
) -> None:
    path = tmp_path / "phase5a.db"
    _create_phase_5a_database(path)

    init_db(path)
    init_db(path)
    connection = connect_db(path)
    try:
        owner = connection.execute(
            "SELECT * FROM users WHERE id = 17"
        ).fetchone()
        assert owner["email"] == "owner@local"
        assert owner["password_hash"] is None
        assert owner["is_active"] == 1
        assert connection.execute(
            "SELECT owner_id FROM dealership_profiles WHERE id = 23"
        ).fetchone()["owner_id"] == 17
        assert connection.execute(
            "SELECT owner_id FROM runs WHERE id = 29"
        ).fetchone()["owner_id"] == 17
        assert connection.execute(
            "SELECT owner_id FROM delivery_links WHERE id = 31"
        ).fetchone()["owner_id"] == 17
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.parametrize("value", [None, "", "   "])
def test_null_and_blank_email_are_rejected_for_new_writes(
    tmp_path: Path,
    value: str | None,
) -> None:
    path = tmp_path / "constraints.db"
    init_db(path)
    connection = connect_db(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO users (email) VALUES (?)",
                (value,),
            )
        connection.rollback()

        connection.execute(
            "INSERT INTO users (email) VALUES ('valid@example.com')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE users SET email = ? WHERE email = ?",
                (value, "valid@example.com"),
            )
    finally:
        connection.close()


def test_email_uniqueness_is_case_insensitive(
    tmp_path: Path,
) -> None:
    path = tmp_path / "constraints.db"
    init_db(path)
    connection = connect_db(path)
    try:
        connection.execute(
            "INSERT INTO users (email) VALUES ('Person@Example.com')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO users (email) VALUES ('person@example.COM')"
            )
    finally:
        connection.close()


def test_migration_fails_clearly_for_invalid_existing_email(
    tmp_path: Path,
) -> None:
    path = tmp_path / "invalid.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                email TEXT,
                display_name TEXT,
                created_utc TEXT
            )
            """
        )
        connection.execute("INSERT INTO users (email) VALUES (NULL)")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DatabaseMigrationError, match="non-blank"):
        init_db(path)


def test_migration_fails_clearly_for_case_insensitive_duplicates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "duplicates.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                email TEXT,
                display_name TEXT,
                created_utc TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO users (email) VALUES (?)",
            [("Person@example.com",), ("person@EXAMPLE.com",)],
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(DatabaseMigrationError, match="duplicate"):
        init_db(path)
