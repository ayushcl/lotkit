import sqlite3
from pathlib import Path

import pytest

from api.db import connect_db, init_db


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
                1, 'owner@local', 'Owner', '2026-01-01T00:00:00+00:00'
            );

            INSERT INTO runs (
                id, run_id, owner_id, vin, status, created_utc, updated_utc
            ) VALUES (
                1, 'legacy-run', 1, '1HGCM82633A004352', 'ready',
                '2026-01-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00'
            );

            INSERT INTO delivery_links (
                id, owner_id, run_id, token_hash, token_hint,
                artifact_manifest_json, created_utc, expires_utc
            ) VALUES (
                1, 1, 'legacy-run',
                'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'legacy', '{}',
                '2026-01-01T00:00:00+00:00',
                '2026-02-01T00:00:00+00:00'
            );

            INSERT INTO delivery_links (
                id, owner_id, run_id, token_hash, token_hint,
                artifact_manifest_json, created_utc, expires_utc,
                revoked_utc, revocation_reason
            ) VALUES (
                2, 1, 'legacy-run',
                'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                'oldone', '{}',
                '2025-12-01T00:00:00+00:00',
                '2026-01-01T00:00:00+00:00',
                '2025-12-15T00:00:00+00:00', 'manual'
            );
            """
        )
        connection.commit()
    finally:
        connection.close()


def test_phase_5a_database_is_migrated_additively_and_idempotently(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "phase-5a.db"
    _create_phase_5a_database(database_path)

    init_db(database_path)
    connection = connect_db(database_path)
    try:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(delivery_links)")
        }
        assert "public_id" in columns

        rows = connection.execute(
            """
            SELECT id, public_id, token_hint, revoked_utc, revocation_reason
            FROM delivery_links
            ORDER BY id
            """
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["id"] == 1
        assert rows[0]["token_hint"] == "legacy"
        assert rows[0]["public_id"] is None
        assert rows[0]["revoked_utc"]
        assert rows[0]["revocation_reason"] == "transport_migrated"
        migrated_revoked_utc = rows[0]["revoked_utc"]

        assert rows[1]["id"] == 2
        assert rows[1]["public_id"] is None
        assert rows[1]["revoked_utc"] == "2025-12-15T00:00:00+00:00"
        assert rows[1]["revocation_reason"] == "manual"
    finally:
        connection.close()

    init_db(database_path)
    connection = connect_db(database_path)
    try:
        migrated = connection.execute(
            """
            SELECT revoked_utc, revocation_reason
            FROM delivery_links
            WHERE id = 1
            """
        ).fetchone()
        assert migrated["revoked_utc"] == migrated_revoked_utc
        assert migrated["revocation_reason"] == "transport_migrated"
    finally:
        connection.close()


def test_public_id_and_delivery_session_indexes_and_cascade(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "phase-5a.db"
    _create_phase_5a_database(database_path)
    init_db(database_path)

    connection = connect_db(database_path)
    try:
        link_indexes = {
            row["name"]: row
            for row in connection.execute("PRAGMA index_list(delivery_links)")
        }
        public_id_index = link_indexes["delivery_links_public_id_idx"]
        assert public_id_index["unique"] == 1
        assert public_id_index["partial"] == 1

        session_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(delivery_sessions)"
            )
        }
        assert session_columns == {
            "id",
            "delivery_link_id",
            "session_hash",
            "created_utc",
            "expires_utc",
        }
        session_indexes = {
            row["name"]
            for row in connection.execute(
                "PRAGMA index_list(delivery_sessions)"
            )
        }
        assert "delivery_sessions_session_hash_idx" in session_indexes
        assert "delivery_sessions_delivery_link_id_idx" in session_indexes

        session_foreign_keys = {
            row["from"]: (row["table"], row["to"], row["on_delete"])
            for row in connection.execute(
                "PRAGMA foreign_key_list(delivery_sessions)"
            )
        }
        assert session_foreign_keys["delivery_link_id"] == (
            "delivery_links",
            "id",
            "CASCADE",
        )

        connection.execute(
            """
            INSERT INTO delivery_links (
                owner_id, run_id, public_id, token_hash, token_hint,
                artifact_manifest_json, created_utc, expires_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                1,
                "legacy-run",
                "public-one",
                "c" * 64,
                "newone",
                "{}",
                "2026-01-01T00:00:00+00:00",
                "2026-02-01T00:00:00+00:00",
            ),
        )
        link_id = connection.execute(
            "SELECT id FROM delivery_links WHERE public_id = 'public-one'"
        ).fetchone()["id"]
        connection.execute(
            """
            INSERT INTO delivery_sessions (
                delivery_link_id, session_hash, created_utc, expires_utc
            ) VALUES (?, ?, ?, ?)
            """,
            (
                link_id,
                "d" * 64,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T04:00:00+00:00",
            ),
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO delivery_links (
                    owner_id, run_id, public_id, token_hash, token_hint,
                    artifact_manifest_json, created_utc, expires_utc,
                    revoked_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    "legacy-run",
                    "public-one",
                    "e" * 64,
                    "again1",
                    "{}",
                    "2026-01-01T00:00:00+00:00",
                    "2026-02-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO delivery_links (
                    owner_id, run_id, token_hash, token_hint,
                    artifact_manifest_json, created_utc, expires_utc,
                    revoked_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    1,
                    "legacy-run",
                    "f" * 64,
                    "nopubl",
                    "{}",
                    "2026-01-01T00:00:00+00:00",
                    "2026-02-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )

        connection.execute(
            "DELETE FROM delivery_links WHERE id = ?",
            (link_id,),
        )
        remaining_sessions = connection.execute(
            "SELECT COUNT(*) FROM delivery_sessions"
        ).fetchone()[0]
        assert remaining_sessions == 0
    finally:
        connection.close()
