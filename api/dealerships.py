import mimetypes
import sqlite3
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image

from api.config import Settings, ensure_persistent_directories, get_settings
from api.photos import safe_ext

PROFILE_FIELDS = (
    "nickname",
    "dealership_name",
    "address",
    "phone",
    "email",
    "complaints_contact",
    "sticker_footer_text",
    "notes",
)

IMAGE_FORMAT_EXTENSIONS = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
}


class InvalidLogoError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_profile_data(values: dict[str, Any]) -> dict[str, str]:
    return {
        field: str(values.get(field) or "").strip()
        for field in PROFILE_FIELDS
    }


def validate_logo(filename: str, file_bytes: bytes) -> str:
    try:
        extension = safe_ext(filename)
    except ValueError as exc:
        raise InvalidLogoError(
            "Logo must be a JPEG, PNG, or WebP image."
        ) from exc

    try:
        with Image.open(BytesIO(file_bytes)) as image:
            image_format = image.format
            image.verify()
    except Exception as exc:
        raise InvalidLogoError("Logo must be a valid image file.") from exc

    actual_extension = IMAGE_FORMAT_EXTENSIONS.get(str(image_format).upper())
    if actual_extension is None or actual_extension != extension:
        raise InvalidLogoError(
            "Logo file extension must match its image format."
        )
    return extension


def _logo_relative_path(
    profile_id: int,
    extension: str,
    settings: Settings,
) -> Path:
    logo_root = settings.dealership_logos_dir.resolve()
    try:
        relative_root = logo_root.relative_to(settings.data_dir.resolve())
    except ValueError as exc:
        raise InvalidLogoError(
            "Could not resolve the logo storage path."
        ) from exc
    return relative_root / f"profile_{profile_id}{extension}"


def resolve_logo_path(
    logo_path: str | None,
    *,
    settings: Settings | None = None,
) -> Path | None:
    if not logo_path:
        return None

    relative_path = Path(logo_path)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        return None

    configured = settings or get_settings()
    data_root = configured.data_dir.resolve()
    logo_root = configured.dealership_logos_dir.resolve()
    if not logo_root.is_relative_to(data_root):
        return None
    candidate = (configured.data_dir / relative_path).resolve()
    if (
        candidate.parent != logo_root
        or not candidate.is_relative_to(data_root)
    ):
        return None
    return candidate


def save_logo(profile_id: int, extension: str, file_bytes: bytes) -> str:
    settings = get_settings()
    ensure_persistent_directories(settings)
    relative_path = _logo_relative_path(profile_id, extension, settings)
    destination = resolve_logo_path(
        relative_path.as_posix(),
        settings=settings,
    )
    if destination is None:
        raise InvalidLogoError("Could not resolve the logo storage path.")
    destination.write_bytes(file_bytes)
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return relative_path.as_posix()


def delete_logo_file(logo_path: str | None) -> None:
    logo_file = resolve_logo_path(logo_path)
    if logo_file is not None and logo_file.is_file():
        logo_file.unlink()


def get_profile_row(
    connection: sqlite3.Connection,
    owner_id: int,
    profile_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT *
        FROM dealership_profiles
        WHERE id = ? AND owner_id = ?
        """,
        (profile_id, owner_id),
    ).fetchone()


def _profile_response(row: sqlite3.Row) -> dict[str, Any]:
    logo_file = resolve_logo_path(row["logo_path"])
    has_logo = bool(logo_file is not None and logo_file.is_file())
    return {
        "id": int(row["id"]),
        "nickname": row["nickname"],
        "dealership_name": row["dealership_name"] or "",
        "address": row["address"] or "",
        "phone": row["phone"] or "",
        "email": row["email"] or "",
        "complaints_contact": row["complaints_contact"] or "",
        "sticker_footer_text": row["sticker_footer_text"] or "",
        "logo_path": row["logo_path"],
        "has_logo": has_logo,
        "logo_url": (
            f"/api/dealerships/{int(row['id'])}/logo" if has_logo else None
        ),
        "notes": row["notes"] or "",
        "created_utc": row["created_utc"],
        "updated_utc": row["updated_utc"],
    }


def create_profile(
    connection: sqlite3.Connection,
    owner_id: int,
    values: dict[str, Any],
    logo: tuple[str, bytes] | None = None,
) -> dict[str, Any]:
    profile = normalize_profile_data(values)
    timestamp = utc_now()
    logo_path = None

    cursor = connection.execute(
        """
        INSERT INTO dealership_profiles (
            owner_id,
            nickname,
            dealership_name,
            address,
            phone,
            email,
            complaints_contact,
            sticker_footer_text,
            logo_path,
            notes,
            created_utc,
            updated_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
        """,
        (
            owner_id,
            profile["nickname"],
            profile["dealership_name"],
            profile["address"],
            profile["phone"],
            profile["email"],
            profile["complaints_contact"],
            profile["sticker_footer_text"],
            profile["notes"],
            timestamp,
            timestamp,
        ),
    )
    profile_id = int(cursor.lastrowid)

    try:
        if logo is not None:
            extension, file_bytes = logo
            logo_path = save_logo(profile_id, extension, file_bytes)
            connection.execute(
                """
                UPDATE dealership_profiles
                SET logo_path = ?
                WHERE id = ? AND owner_id = ?
                """,
                (logo_path, profile_id, owner_id),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        delete_logo_file(logo_path)
        raise

    row = get_profile_row(connection, owner_id, profile_id)
    if row is None:
        raise RuntimeError("Created dealership profile could not be loaded.")
    return _profile_response(row)


def list_profiles(
    connection: sqlite3.Connection,
    owner_id: int,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT *
        FROM dealership_profiles
        WHERE owner_id = ?
        ORDER BY nickname COLLATE NOCASE, id
        """,
        (owner_id,),
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "nickname": row["nickname"],
            "dealership_name": row["dealership_name"] or "",
            "has_logo": bool(
                (logo_file := resolve_logo_path(row["logo_path"]))
                is not None
                and logo_file.is_file()
            ),
        }
        for row in rows
    ]


def get_profile(
    connection: sqlite3.Connection,
    owner_id: int,
    profile_id: int,
) -> dict[str, Any] | None:
    row = get_profile_row(connection, owner_id, profile_id)
    return _profile_response(row) if row is not None else None


def update_profile(
    connection: sqlite3.Connection,
    owner_id: int,
    profile_id: int,
    values: dict[str, Any],
    logo: tuple[str, bytes] | None = None,
) -> dict[str, Any] | None:
    existing = get_profile_row(connection, owner_id, profile_id)
    if existing is None:
        return None

    profile = normalize_profile_data(values)
    old_logo_path = existing["logo_path"]
    new_logo_path = old_logo_path
    saved_logo_path = None

    try:
        if logo is not None:
            extension, file_bytes = logo
            new_logo_path = save_logo(profile_id, extension, file_bytes)
            saved_logo_path = new_logo_path

        connection.execute(
            """
            UPDATE dealership_profiles
            SET nickname = ?,
                dealership_name = ?,
                address = ?,
                phone = ?,
                email = ?,
                complaints_contact = ?,
                sticker_footer_text = ?,
                logo_path = ?,
                notes = ?,
                updated_utc = ?
            WHERE id = ? AND owner_id = ?
            """,
            (
                profile["nickname"],
                profile["dealership_name"],
                profile["address"],
                profile["phone"],
                profile["email"],
                profile["complaints_contact"],
                profile["sticker_footer_text"],
                new_logo_path,
                profile["notes"],
                utc_now(),
                profile_id,
                owner_id,
            ),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        if saved_logo_path != old_logo_path:
            delete_logo_file(saved_logo_path)
        raise

    if new_logo_path != old_logo_path:
        delete_logo_file(old_logo_path)

    row = get_profile_row(connection, owner_id, profile_id)
    return _profile_response(row) if row is not None else None


def delete_profile(
    connection: sqlite3.Connection,
    owner_id: int,
    profile_id: int,
) -> bool:
    existing = get_profile_row(connection, owner_id, profile_id)
    if existing is None:
        return False

    connection.execute(
        "DELETE FROM dealership_profiles WHERE id = ? AND owner_id = ?",
        (profile_id, owner_id),
    )
    connection.commit()
    delete_logo_file(existing["logo_path"])
    return True


def logo_media_type(logo_file: Path) -> str:
    return mimetypes.guess_type(logo_file.name)[0] or "application/octet-stream"
