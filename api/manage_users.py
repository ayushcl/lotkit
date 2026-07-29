"""Administrative CLI for LotKit's invite-only photographer accounts."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from getpass import getpass

from dotenv import load_dotenv

from api.config import PROJECT_ROOT

load_dotenv(PROJECT_ROOT / ".env")

from api.auth import (  # noqa: E402
    DEFAULT_OWNER_EMAIL,
    hash_password,
    normalize_email,
    revoke_all_user_sessions,
    utc_now,
)
from api.db import connect_db, init_db  # noqa: E402


class UserManagementError(RuntimeError):
    """Raised for safe, operator-actionable account management failures."""


def _required_email(value: str) -> str:
    normalized = normalize_email(value)
    if not normalized:
        raise UserManagementError("Email is required.")
    return normalized


def _required_display_name(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise UserManagementError("Display name is required.")
    return normalized


def _prompt_password() -> str:
    password = getpass("Password: ")
    confirmation = getpass("Confirm password: ")
    if password != confirmation:
        raise UserManagementError("Passwords do not match.")
    try:
        return hash_password(password)
    except ValueError as exc:
        raise UserManagementError(str(exc)) from exc


def _user_by_email(
    connection: sqlite3.Connection,
    email: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT id, email, display_name, password_hash, is_active,
               created_utc, updated_utc, password_changed_utc
        FROM users
        WHERE email = ? COLLATE NOCASE
        """,
        (_required_email(email),),
    ).fetchone()


def claim_default(email: str, display_name: str) -> int:
    normalized_email = _required_email(email)
    clean_display_name = _required_display_name(display_name)
    password_hash = _prompt_password()
    timestamp = utc_now().isoformat()
    connection = connect_db()
    try:
        owner = _user_by_email(connection, DEFAULT_OWNER_EMAIL)
        if owner is None:
            raise UserManagementError(
                "The legacy owner@local account was not found."
            )
        duplicate = _user_by_email(connection, normalized_email)
        if duplicate is not None and int(duplicate["id"]) != int(owner["id"]):
            raise UserManagementError("An account with that email exists.")
        connection.execute(
            """
            UPDATE users
            SET email = ?, display_name = ?, password_hash = ?,
                is_active = 1, updated_utc = ?, password_changed_utc = ?
            WHERE id = ?
            """,
            (
                normalized_email,
                clean_display_name,
                password_hash,
                timestamp,
                timestamp,
                owner["id"],
            ),
        )
        revoke_all_user_sessions(connection, int(owner["id"]))
        connection.commit()
        return int(owner["id"])
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise UserManagementError("An account with that email exists.") from exc
    finally:
        connection.close()


def create_user(email: str, display_name: str) -> int:
    normalized_email = _required_email(email)
    clean_display_name = _required_display_name(display_name)
    password_hash = _prompt_password()
    timestamp = utc_now().isoformat()
    connection = connect_db()
    try:
        cursor = connection.execute(
            """
            INSERT INTO users (
                email, display_name, password_hash, is_active, created_utc,
                updated_utc, password_changed_utc
            ) VALUES (?, ?, ?, 1, ?, ?, ?)
            """,
            (
                normalized_email,
                clean_display_name,
                password_hash,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise UserManagementError("An account with that email exists.") from exc
    finally:
        connection.close()


def set_password(email: str) -> int:
    password_hash = _prompt_password()
    timestamp = utc_now().isoformat()
    connection = connect_db()
    try:
        user = _user_by_email(connection, email)
        if user is None:
            raise UserManagementError("Account not found.")
        connection.execute(
            """
            UPDATE users
            SET password_hash = ?, updated_utc = ?,
                password_changed_utc = ?
            WHERE id = ?
            """,
            (password_hash, timestamp, timestamp, user["id"]),
        )
        revoke_all_user_sessions(connection, int(user["id"]))
        connection.commit()
        return int(user["id"])
    finally:
        connection.close()


def set_active(email: str, active: bool) -> int:
    timestamp = utc_now().isoformat()
    connection = connect_db()
    try:
        user = _user_by_email(connection, email)
        if user is None:
            raise UserManagementError("Account not found.")
        connection.execute(
            """
            UPDATE users
            SET is_active = ?, updated_utc = ?
            WHERE id = ?
            """,
            (int(active), timestamp, user["id"]),
        )
        if not active:
            revoke_all_user_sessions(connection, int(user["id"]))
        connection.commit()
        return int(user["id"])
    finally:
        connection.close()


def list_users() -> list[sqlite3.Row]:
    connection = connect_db()
    try:
        return connection.execute(
            """
            SELECT id, email, display_name, is_active, created_utc,
                   updated_utc, password_changed_utc,
                   CASE WHEN password_hash IS NULL THEN 0 ELSE 1 END
                       AS has_password
            FROM users
            ORDER BY email COLLATE NOCASE
            """
        ).fetchall()
    finally:
        connection.close()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage invite-only LotKit photographer accounts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ("claim-default", "create"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--email")
        subparser.add_argument("--display-name")

    password_parser = subparsers.add_parser("set-password")
    password_parser.add_argument("--email")

    for command in ("disable", "enable"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--email")

    subparsers.add_parser("list")
    return parser


def main(argv: list[str] | None = None) -> int:
    supplied_arguments = list(sys.argv[1:] if argv is None else argv)
    if any(
        argument == "--password"
        or argument.startswith("--password=")
        for argument in supplied_arguments
    ):
        print(
            "Error: passwords are accepted only through the secure prompt.",
            file=sys.stderr,
        )
        return 2
    args = _parser().parse_args(supplied_arguments)
    try:
        init_db()
        if args.command == "claim-default":
            user_id = claim_default(
                args.email or input("Real email: "),
                args.display_name or input("Display name: "),
            )
            print(f"Claimed legacy account (user_id={user_id}).")
        elif args.command == "create":
            user_id = create_user(
                args.email or input("Email: "),
                args.display_name or input("Display name: "),
            )
            print(f"Created account (user_id={user_id}).")
        elif args.command == "set-password":
            user_id = set_password(args.email or input("Email: "))
            print(f"Password updated; sessions revoked (user_id={user_id}).")
        elif args.command == "disable":
            user_id = set_active(args.email or input("Email: "), False)
            print(f"Disabled account; sessions revoked (user_id={user_id}).")
        elif args.command == "enable":
            user_id = set_active(args.email or input("Email: "), True)
            print(f"Enabled account (user_id={user_id}).")
        else:
            rows = list_users()
            print(
                "ID\tACTIVE\tPASSWORD\tEMAIL\tDISPLAY NAME\t"
                "PASSWORD CHANGED"
            )
            for row in rows:
                print(
                    f"{row['id']}\t{row['is_active']}\t"
                    f"{row['has_password']}\t{row['email']}\t"
                    f"{row['display_name'] or ''}\t"
                    f"{row['password_changed_utc'] or ''}"
                )
        return 0
    except UserManagementError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
