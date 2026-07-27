import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "lotkit.db"


def connect_db(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(db_path: str | Path | None = None) -> None:
    connection = connect_db(db_path)
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                email TEXT UNIQUE,
                display_name TEXT,
                created_utc TEXT
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
            """
        )
        connection.commit()
    finally:
        connection.close()
