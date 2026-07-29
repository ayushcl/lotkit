import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from api.config import get_settings


class DatabaseMigrationError(RuntimeError):
    """Raised when existing data makes an additive migration unsafe."""


def connect_db(db_path: str | Path | None = None) -> sqlite3.Connection:
    settings = get_settings()
    if db_path is None:
        path = settings.database_path
    else:
        path = Path(db_path).expanduser()
        if not path.is_absolute():
            path = settings.project_root / path
        path = path.resolve()
    if (
        settings.environment == "production"
        and path != settings.database_path.resolve()
    ):
        raise ValueError("Database path is outside configured storage.")

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    database_existed = path.exists()
    connection = sqlite3.connect(path)
    if not database_existed:
        try:
            path.chmod(0o600)
        except OSError:
            pass
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _validate_existing_user_emails(
    connection: sqlite3.Connection,
) -> None:
    invalid = connection.execute(
        """
        SELECT id
        FROM users
        WHERE email IS NULL
           OR length(trim(email)) = 0
           OR email != trim(email)
        LIMIT 1
        """
    ).fetchone()
    if invalid is not None:
        raise DatabaseMigrationError(
            "Cannot migrate users: every existing email must be non-blank "
            "and must not contain surrounding whitespace."
        )

    duplicate = connection.execute(
        """
        SELECT email
        FROM users
        GROUP BY trim(email) COLLATE NOCASE
        HAVING COUNT(*) > 1
        LIMIT 1
        """
    ).fetchone()
    if duplicate is not None:
        raise DatabaseMigrationError(
            "Cannot migrate users: case-insensitive duplicate emails exist."
        )


def _migrate_user_authentication(
    connection: sqlite3.Connection,
) -> None:
    """Add Phase 5b identity constraints without rebuilding ``users``."""

    _validate_existing_user_emails(connection)
    user_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(users)")
    }
    additions = {
        "password_hash": "TEXT",
        "is_active": (
            "INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))"
        ),
        "updated_utc": "TEXT",
        "password_changed_utc": "TEXT",
    }
    for name, declaration in additions.items():
        if name not in user_columns:
            connection.execute(
                f"ALTER TABLE users ADD COLUMN {name} {declaration}"
            )

    try:
        connection.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS users_email_nocase_idx
            ON users(email COLLATE NOCASE);

            CREATE TRIGGER IF NOT EXISTS users_require_email_insert
            BEFORE INSERT ON users
            WHEN NEW.email IS NULL OR length(trim(NEW.email)) = 0
            BEGIN
                SELECT RAISE(ABORT, 'users.email is required');
            END;

            CREATE TRIGGER IF NOT EXISTS users_require_email_update
            BEFORE UPDATE OF email ON users
            WHEN NEW.email IS NULL OR length(trim(NEW.email)) = 0
            BEGIN
                SELECT RAISE(ABORT, 'users.email is required');
            END;

            CREATE TABLE IF NOT EXISTS user_sessions (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL
                    REFERENCES users(id) ON DELETE CASCADE,
                session_hash TEXT NOT NULL,
                csrf_hash TEXT NOT NULL,
                created_utc TEXT NOT NULL,
                expires_utc TEXT NOT NULL,
                last_seen_utc TEXT NOT NULL,
                revoked_utc TEXT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS
                user_sessions_session_hash_idx
            ON user_sessions(session_hash);

            CREATE INDEX IF NOT EXISTS user_sessions_user_id_idx
            ON user_sessions(user_id);

            CREATE INDEX IF NOT EXISTS user_sessions_expires_utc_idx
            ON user_sessions(expires_utc);
            """
        )
    except sqlite3.IntegrityError as exc:
        raise DatabaseMigrationError(
            "Cannot migrate users: email identity constraints are unsafe."
        ) from exc


def init_db(db_path: str | Path | None = None) -> None:
    connection = connect_db(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL COLLATE NOCASE,
                display_name TEXT,
                password_hash TEXT,
                is_active INTEGER NOT NULL DEFAULT 1
                    CHECK (is_active IN (0, 1)),
                created_utc TEXT,
                updated_utc TEXT,
                password_changed_utc TEXT
            );

            CREATE TABLE IF NOT EXISTS dealership_profiles (
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

            CREATE INDEX IF NOT EXISTS dealership_profiles_owner_id_idx
            ON dealership_profiles(owner_id);

            CREATE TABLE IF NOT EXISTS runs (
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

            CREATE INDEX IF NOT EXISTS runs_owner_updated_utc_idx
            ON runs(owner_id, updated_utc);

            CREATE TABLE IF NOT EXISTS delivery_links (
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

            CREATE INDEX IF NOT EXISTS delivery_links_token_hash_idx
            ON delivery_links(token_hash);

            CREATE INDEX IF NOT EXISTS delivery_links_run_id_idx
            ON delivery_links(run_id);

            CREATE INDEX IF NOT EXISTS delivery_links_owner_id_idx
            ON delivery_links(owner_id);

            CREATE UNIQUE INDEX IF NOT EXISTS
                ux_delivery_links_one_unrevoked
            ON delivery_links(run_id)
            WHERE revoked_utc IS NULL;
            """
        )

        _migrate_user_authentication(connection)

        # Phase 5a.1 uses an additive migration so existing delivery history
        # remains intact. Legacy rows intentionally retain a NULL public_id:
        # their former URL-path bearer secret cannot be reconstructed safely.
        delivery_link_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(delivery_links)")
        }
        if "public_id" not in delivery_link_columns:
            connection.execute(
                "ALTER TABLE delivery_links ADD COLUMN public_id TEXT"
            )

        connection.execute(
            """
            UPDATE delivery_links
            SET revoked_utc = ?, revocation_reason = 'transport_migrated'
            WHERE public_id IS NULL AND revoked_utc IS NULL
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )

        connection.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
                delivery_links_public_id_idx
            ON delivery_links(public_id)
            WHERE public_id IS NOT NULL;

            CREATE TRIGGER IF NOT EXISTS
                delivery_links_require_public_id
            BEFORE INSERT ON delivery_links
            WHEN NEW.public_id IS NULL
            BEGIN
                SELECT RAISE(
                    ABORT,
                    'delivery_links.public_id is required'
                );
            END;

            CREATE TABLE IF NOT EXISTS delivery_sessions (
                id INTEGER PRIMARY KEY,
                delivery_link_id INTEGER NOT NULL
                    REFERENCES delivery_links(id) ON DELETE CASCADE,
                session_hash TEXT NOT NULL UNIQUE,
                created_utc TEXT NOT NULL,
                expires_utc TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS delivery_sessions_session_hash_idx
            ON delivery_sessions(session_hash);

            CREATE INDEX IF NOT EXISTS
                delivery_sessions_delivery_link_id_idx
            ON delivery_sessions(delivery_link_id);
            """
        )
        connection.commit()
    finally:
        connection.close()
