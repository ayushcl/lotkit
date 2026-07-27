import sqlite3
from datetime import datetime, timezone

from api.db import connect_db

DEFAULT_OWNER_EMAIL = "owner@local"
DEFAULT_OWNER_DISPLAY_NAME = "Owner"


def get_or_create_default_owner(connection: sqlite3.Connection) -> int:
    owner = connection.execute(
        "SELECT id FROM users WHERE email = ?",
        (DEFAULT_OWNER_EMAIL,),
    ).fetchone()
    if owner is None:
        connection.execute(
            """
            INSERT OR IGNORE INTO users (email, display_name, created_utc)
            VALUES (?, ?, ?)
            """,
            (
                DEFAULT_OWNER_EMAIL,
                DEFAULT_OWNER_DISPLAY_NAME,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        connection.commit()
        owner = connection.execute(
            "SELECT id FROM users WHERE email = ?",
            (DEFAULT_OWNER_EMAIL,),
        ).fetchone()

    if owner is None:
        raise RuntimeError("Could not create the default LotKit owner.")
    return int(owner[0])


def current_owner_id() -> int:
    # Phase 5 auth replaces ONLY this dependency; CRUD and schema stay unchanged.
    connection = connect_db()
    try:
        return get_or_create_default_owner(connection)
    finally:
        connection.close()
