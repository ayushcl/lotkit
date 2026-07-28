import hashlib
import json
import logging
import re
import secrets
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from fastapi.responses import HTMLResponse, Response

from api.runs import (
    ARTIFACT_TYPES,
    InvalidRunDataError,
    get_run_detail,
    parse_json_object,
    safe_run_directory,
)

DELIVERY_LIFETIME = timedelta(days=30)
TOKEN_HINT_LENGTH = 6
TOKEN_MIN_LENGTH = 40
TOKEN_MAX_LENGTH = 64
TOKEN_PATTERN = re.compile(
    rf"^[A-Za-z0-9_-]{{{TOKEN_MIN_LENGTH},{TOKEN_MAX_LENGTH}}}$"
)

DELIVERY_STATES = frozenset({"active", "expired", "revoked"})
REVOCATION_REASONS = frozenset(
    {"replaced", "manual", "reopened", "outputs_changed"}
)

ARTIFACT_POLICIES = {
    "photos_zip": {
        "suffix": ".zip",
        "download_name": "vehicle_photos.zip",
        "content_type": "application/zip",
    },
    "sticker_pdf": {
        "suffix": ".pdf",
        "download_name": "window_sticker.pdf",
        "content_type": "application/pdf",
    },
    "buyers_guide_pdf": {
        "suffix": ".pdf",
        "download_name": "draft_buyers_guide.pdf",
        "content_type": "application/pdf",
    },
}

PUBLIC_UNAVAILABLE_COPY = (
    "This delivery link is unavailable. It may have expired or been "
    "withdrawn. Ask the person who sent it to create a new link."
)
PUBLIC_UNAVAILABLE_HTML = (
    "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
    "<title>Delivery link unavailable</title>"
    "<style>body{font-family:system-ui,sans-serif;max-width:42rem;"
    "margin:4rem auto;padding:0 1.25rem;line-height:1.5;color:#202124}"
    "</style></head><body><main><h1>Delivery link unavailable</h1><p>"
    f"{PUBLIC_UNAVAILABLE_COPY}"
    "</p></main></body></html>"
)

PUBLIC_BASE_SECURITY_HEADERS = {
    "Cache-Control": "no-store, private",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Resource-Policy": "same-origin",
}
PUBLIC_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
    "base-uri 'none'; frame-ancestors 'none'"
)

# Invalid tokens still take the indexed hash-lookup path. This constant is
# not a bearer credential and avoids putting malformed input into SQL.
INVALID_TOKEN_LOOKUP_HASH = hashlib.sha256(
    b"LotKit invalid delivery token lookup"
).hexdigest()


class DeliveryError(Exception):
    """Base class for expected delivery-link service errors."""


class DeliveryRunNotFoundError(DeliveryError):
    """The Run does not exist for the current owner."""


class RunNotReadyError(DeliveryError):
    """Only ready Runs may receive delivery links."""


class RunHasNoArtifactsError(DeliveryError):
    """No durable allowlisted artifacts are available to share."""


class RunDeliveredError(DeliveryError):
    """Delivered Runs are immutable until explicitly reopened."""


class RunNotDeliveredError(DeliveryError):
    """Only a delivered Run may be reopened."""


class DeliveryStorageError(DeliveryError):
    """Stored delivery metadata is malformed or unsafe."""


@dataclass(frozen=True)
class ResolvedManifestArtifact:
    artifact_type: str
    path: Path
    download_name: str
    content_type: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime:
    current = value or utc_now()
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc(parsed)


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        if isinstance(row, Mapping):
            return row.get(key, default)
        return default


def generate_delivery_token() -> str:
    """Create an approximately 256-bit URL-safe bearer token."""

    return secrets.token_urlsafe(32)


def hash_delivery_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_delivery_token_format(token: Any) -> bool:
    return isinstance(token, str) and TOKEN_PATTERN.fullmatch(token) is not None


def delivery_token_hint(token: str) -> str:
    return token[-TOKEN_HINT_LENGTH:]


def derive_delivery_link_state(
    row: Any,
    now: datetime | None = None,
) -> str:
    if _row_value(row, "revoked_utc"):
        return "revoked"

    expires = _parse_utc(_row_value(row, "expires_utc"))
    if expires is None or _as_utc(now) >= expires:
        return "expired"
    return "active"


def immutable_artifact_filename(artifact_type: str) -> str:
    policy = ARTIFACT_POLICIES.get(artifact_type)
    if policy is None:
        raise DeliveryStorageError("Unsupported delivery artifact type.")
    return (
        f"{artifact_type}_{uuid.uuid4().hex}"
        f"{policy['suffix']}"
    )


# Clearer alias for output endpoints creating a fresh immutable generation.
new_artifact_filename = immutable_artifact_filename


def _safe_stored_filename(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    filename = value.strip()
    if (
        not filename
        or filename in {".", ".."}
        or "\x00" in filename
        or "/" in filename
        or "\\" in filename
        or Path(filename).name != filename
    ):
        return None
    return filename


def build_artifact_manifest(
    run: Any,
    runs_root: str | Path,
) -> dict[str, dict[str, str]]:
    """
    Copy current Run outputs to immutable, server-named files.

    The manifest stores paths relative to the approved Runs root. It never
    stores absolute paths or client-provided download names.
    """

    run_id = _row_value(run, "run_id")
    outputs_value = _row_value(run, "outputs_json")
    if outputs_value is None:
        outputs_value = _row_value(run, "outputs", {})
    outputs = (
        parse_json_object(outputs_value)
        if isinstance(outputs_value, str)
        else outputs_value
    )
    if not isinstance(outputs, Mapping):
        outputs = {}

    try:
        run_directory = safe_run_directory(str(run_id), runs_root)
    except InvalidRunDataError as exc:
        raise DeliveryStorageError("Run storage is unavailable.") from exc

    manifest: dict[str, dict[str, str]] = {}
    created_files: list[Path] = []
    try:
        for artifact_type in ARTIFACT_TYPES:
            policy = ARTIFACT_POLICIES[artifact_type]
            source_filename = _safe_stored_filename(
                outputs.get(artifact_type)
            )
            if source_filename is None:
                continue

            lexical_source = run_directory / source_filename
            source = lexical_source.resolve()
            if (
                source != lexical_source
                or source.parent != run_directory
                or not source.is_file()
            ):
                continue

            immutable_filename = immutable_artifact_filename(artifact_type)
            destination = run_directory / immutable_filename
            shutil.copyfile(source, destination)
            try:
                destination.chmod(0o600)
            except OSError:
                pass
            created_files.append(destination)

            relative_path = (
                Path(str(run_id)) / immutable_filename
            ).as_posix()
            manifest[artifact_type] = {
                "relative_path": relative_path,
                "download_name": policy["download_name"],
                "content_type": policy["content_type"],
            }
    except Exception:
        for created_file in created_files:
            created_file.unlink(missing_ok=True)
        raise

    return manifest


def _manifest_from(value: Any) -> dict[str, Any]:
    raw_manifest = _row_value(value, "artifact_manifest_json")
    if raw_manifest is None and isinstance(value, Mapping):
        raw_manifest = value
    if isinstance(raw_manifest, str):
        try:
            parsed = json.loads(raw_manifest)
        except json.JSONDecodeError:
            return {}
    else:
        parsed = raw_manifest
    return parsed if isinstance(parsed, dict) else {}


def manifest_artifact_types(value: Any) -> list[str]:
    manifest = _manifest_from(value)
    return [
        artifact_type
        for artifact_type in ARTIFACT_POLICIES
        if isinstance(manifest.get(artifact_type), dict)
    ]


def resolve_manifest_artifact(
    delivery_link_or_manifest: Any,
    artifact_type: str,
    runs_root: str | Path,
    *,
    run_id: str | None = None,
) -> ResolvedManifestArtifact | None:
    if artifact_type not in ARTIFACT_POLICIES:
        return None

    manifest = _manifest_from(delivery_link_or_manifest)
    entry = manifest.get(artifact_type)
    if not isinstance(entry, dict):
        return None

    expected = ARTIFACT_POLICIES[artifact_type]
    if (
        entry.get("download_name") != expected["download_name"]
        or entry.get("content_type") != expected["content_type"]
    ):
        return None

    stored_run_id = run_id or _row_value(
        delivery_link_or_manifest,
        "run_id",
    )
    if not isinstance(stored_run_id, str):
        return None

    relative_value = entry.get("relative_path")
    if not isinstance(relative_value, str):
        return None
    relative_path = Path(relative_value)
    if (
        relative_path.is_absolute()
        or ".." in relative_path.parts
        or len(relative_path.parts) != 2
        or relative_path.parts[0] != stored_run_id
    ):
        return None

    filename = _safe_stored_filename(relative_path.parts[1])
    if filename is None:
        return None

    try:
        run_directory = safe_run_directory(stored_run_id, runs_root)
    except InvalidRunDataError:
        return None

    lexical_candidate = run_directory / filename
    candidate = lexical_candidate.resolve()
    if (
        candidate != lexical_candidate
        or candidate.parent != run_directory
        or not candidate.is_file()
    ):
        return None

    return ResolvedManifestArtifact(
        artifact_type=artifact_type,
        path=candidate,
        download_name=expected["download_name"],
        content_type=expected["content_type"],
    )


def _lookup_delivery_link_by_hash(
    connection: sqlite3.Connection,
    token_hash: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT delivery_links.*,
               runs.vin AS run_vin,
               runs.vehicle_json AS run_vehicle_json,
               runs.status AS run_status,
               runs.updated_utc AS run_updated_utc
        FROM delivery_links
        JOIN runs ON runs.run_id = delivery_links.run_id
        WHERE delivery_links.token_hash = ?
        """,
        (token_hash,),
    ).fetchone()


def get_usable_delivery_link_by_token(
    connection: sqlite3.Connection,
    token: Any,
    now: datetime | None = None,
    *,
    mark_opened: bool = False,
) -> sqlite3.Row | None:
    current = _as_utc(now)
    lookup_hash = (
        hash_delivery_token(token)
        if validate_delivery_token_format(token)
        else INVALID_TOKEN_LOOKUP_HASH
    )
    row = _lookup_delivery_link_by_hash(connection, lookup_hash)
    if (
        row is None
        or derive_delivery_link_state(row, current) != "active"
        or not manifest_artifact_types(row)
    ):
        return None

    if mark_opened and row["first_opened_utc"] is None:
        connection.execute(
            """
            UPDATE delivery_links
            SET first_opened_utc = ?
            WHERE id = ?
              AND first_opened_utc IS NULL
              AND revoked_utc IS NULL
            """,
            (_utc_text(current), row["id"]),
        )
        connection.commit()
        row = _lookup_delivery_link_by_hash(connection, lookup_hash)
        if (
            row is None
            or derive_delivery_link_state(row, current) != "active"
            or not manifest_artifact_types(row)
        ):
            return None
    return row


def _delete_manifest_files(
    manifest: Mapping[str, Any],
    run_id: str,
    runs_root: str | Path,
) -> None:
    for artifact_type in ARTIFACT_POLICIES:
        resolved = resolve_manifest_artifact(
            manifest,
            artifact_type,
            runs_root,
            run_id=run_id,
        )
        if resolved is not None:
            resolved.path.unlink(missing_ok=True)


def revoke_active_delivery_link(
    connection: sqlite3.Connection,
    run_id: str,
    reason: str,
    now: datetime | None = None,
    *,
    owner_id: int | None = None,
) -> int:
    if reason not in REVOCATION_REASONS:
        raise ValueError("Unsupported delivery-link revocation reason.")

    clauses = ["run_id = ?", "revoked_utc IS NULL"]
    parameters: list[Any] = [_utc_text(_as_utc(now)), reason, run_id]
    if owner_id is not None:
        clauses.append("owner_id = ?")
        parameters.append(owner_id)

    cursor = connection.execute(
        f"""
        UPDATE delivery_links
        SET revoked_utc = ?, revocation_reason = ?
        WHERE {' AND '.join(clauses)}
        """,
        parameters,
    )
    return int(cursor.rowcount)


def create_delivery_link(
    connection: sqlite3.Connection,
    owner_id: int,
    run_id: str,
    runs_root: str | Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = _as_utc(now)
    expires = current + DELIVERY_LIFETIME
    manifest: dict[str, dict[str, str]] = {}

    if connection.in_transaction:
        raise DeliveryError(
            "Delivery-link creation requires a fresh transaction."
        )

    try:
        connection.execute("BEGIN IMMEDIATE")
        run = connection.execute(
            """
            SELECT *
            FROM runs
            WHERE run_id = ? AND owner_id = ?
            """,
            (run_id, owner_id),
        ).fetchone()
        if run is None:
            raise DeliveryRunNotFoundError("Run not found.")
        if run["status"] != "ready":
            raise RunNotReadyError("Run is not ready for delivery.")

        manifest = build_artifact_manifest(run, runs_root)
        if not manifest:
            raise RunHasNoArtifactsError(
                "Run has no durable artifacts to deliver."
            )

        revoke_active_delivery_link(
            connection,
            run_id,
            "replaced",
            current,
            owner_id=owner_id,
        )

        token = ""
        for _attempt in range(8):
            candidate = generate_delivery_token()
            token_hash = hash_delivery_token(candidate)
            if connection.execute(
                "SELECT 1 FROM delivery_links WHERE token_hash = ?",
                (token_hash,),
            ).fetchone():
                continue

            try:
                connection.execute(
                    """
                    INSERT INTO delivery_links (
                        owner_id,
                        run_id,
                        token_hash,
                        token_hint,
                        artifact_manifest_json,
                        created_utc,
                        expires_utc
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        owner_id,
                        run_id,
                        token_hash,
                        delivery_token_hint(candidate),
                        json.dumps(
                            manifest,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        _utc_text(current),
                        _utc_text(expires),
                    ),
                )
            except sqlite3.IntegrityError:
                if connection.execute(
                    "SELECT 1 FROM delivery_links WHERE token_hash = ?",
                    (token_hash,),
                ).fetchone():
                    continue
                raise
            token = candidate
            break
        else:
            raise DeliveryError("Could not allocate a delivery credential.")

        connection.commit()
    except Exception:
        connection.rollback()
        if manifest:
            _delete_manifest_files(manifest, run_id, runs_root)
        raise

    return {
        "public_path": f"/d/{token}",
        "expires_utc": _utc_text(expires),
        "state": "active",
        "token_hint": delivery_token_hint(token),
    }


def get_owner_delivery_status(
    connection: sqlite3.Connection,
    owner_id: int,
    run_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    run_exists = connection.execute(
        "SELECT 1 FROM runs WHERE run_id = ? AND owner_id = ?",
        (run_id, owner_id),
    ).fetchone()
    if run_exists is None:
        raise DeliveryRunNotFoundError("Run not found.")

    row = connection.execute(
        """
        SELECT *
        FROM delivery_links
        WHERE run_id = ? AND owner_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (run_id, owner_id),
    ).fetchone()
    if row is None:
        return {
            "state": "none",
            "token_hint": None,
            "created_utc": None,
            "expires_utc": None,
            "first_opened_utc": None,
            "first_download_started_utc": None,
            "revoked_utc": None,
            "revocation_reason": None,
        }
    return {
        "state": derive_delivery_link_state(row, now),
        "token_hint": row["token_hint"],
        "created_utc": row["created_utc"],
        "expires_utc": row["expires_utc"],
        "first_opened_utc": row["first_opened_utc"],
        "first_download_started_utc": row[
            "first_download_started_utc"
        ],
        "revoked_utc": row["revoked_utc"],
        "revocation_reason": row["revocation_reason"],
    }


def revoke_owner_delivery_link(
    connection: sqlite3.Connection,
    owner_id: int,
    run_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = _as_utc(now)
    if connection.in_transaction:
        raise DeliveryError("Revocation requires a fresh transaction.")

    try:
        connection.execute("BEGIN IMMEDIATE")
        run_exists = connection.execute(
            "SELECT 1 FROM runs WHERE run_id = ? AND owner_id = ?",
            (run_id, owner_id),
        ).fetchone()
        if run_exists is None:
            raise DeliveryRunNotFoundError("Run not found.")
        changed = revoke_active_delivery_link(
            connection,
            run_id,
            "manual",
            current,
            owner_id=owner_id,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    result = get_owner_delivery_status(
        connection,
        owner_id,
        run_id,
        current,
    )
    return {"revoked": bool(changed), **result}


def apply_output_change_lifecycle(
    connection: sqlite3.Connection,
    owner_id: int,
    run_id: str,
    now: datetime | None = None,
) -> sqlite3.Row:
    """
    Apply the shared mutation rule inside the caller's write transaction.

    Call this only after artifact generation succeeds, immediately before
    updating Run snapshots/outputs and committing the same transaction.
    """

    run = connection.execute(
        "SELECT * FROM runs WHERE run_id = ? AND owner_id = ?",
        (run_id, owner_id),
    ).fetchone()
    if run is None:
        raise DeliveryRunNotFoundError("Run not found.")
    if run["status"] == "delivered":
        raise RunDeliveredError(
            "Delivered Runs must be reopened before changes."
        )

    revoked = revoke_active_delivery_link(
        connection,
        run_id,
        "outputs_changed",
        now,
        owner_id=owner_id,
    )
    if revoked and run["status"] == "ready":
        connection.execute(
            """
            UPDATE runs
            SET status = 'in_progress', updated_utc = ?
            WHERE run_id = ? AND owner_id = ?
            """,
            (_utc_text(_as_utc(now)), run_id, owner_id),
        )
    return run


def reopen_delivered_run(
    connection: sqlite3.Connection,
    owner_id: int,
    run_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = _as_utc(now)
    if connection.in_transaction:
        raise DeliveryError("Reopening requires a fresh transaction.")

    try:
        connection.execute("BEGIN IMMEDIATE")
        run = connection.execute(
            "SELECT * FROM runs WHERE run_id = ? AND owner_id = ?",
            (run_id, owner_id),
        ).fetchone()
        if run is None:
            raise DeliveryRunNotFoundError("Run not found.")
        if run["status"] != "delivered":
            raise RunNotDeliveredError("Run is not delivered.")

        revoke_active_delivery_link(
            connection,
            run_id,
            "reopened",
            current,
            owner_id=owner_id,
        )
        connection.execute(
            """
            UPDATE runs
            SET status = 'in_progress', updated_utc = ?
            WHERE run_id = ? AND owner_id = ?
            """,
            (_utc_text(current), run_id, owner_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    detail = get_run_detail(connection, owner_id, run_id)
    if detail is None:
        raise RuntimeError("Reopened Run could not be loaded.")
    return detail


def begin_public_artifact_download(
    connection: sqlite3.Connection,
    token: Any,
    artifact_type: str,
    runs_root: str | Path,
    now: datetime | None = None,
) -> ResolvedManifestArtifact | None:
    """
    Authorise a manifest artifact and record the first download start.

    A successful first call marks the Run delivered immediately before the
    caller begins returning the attachment response. It does not prove that
    every response byte reached the recipient.
    """

    current = _as_utc(now)
    link = get_usable_delivery_link_by_token(
        connection,
        token,
        current,
    )
    if link is None:
        return None
    artifact = resolve_manifest_artifact(
        link,
        artifact_type,
        runs_root,
    )
    if artifact is None:
        return None

    if connection.in_transaction:
        raise DeliveryError(
            "Public download authorisation requires a fresh transaction."
        )

    try:
        connection.execute("BEGIN IMMEDIATE")
        link = get_usable_delivery_link_by_token(
            connection,
            token,
            current,
        )
        if link is None:
            connection.rollback()
            return None
        artifact = resolve_manifest_artifact(
            link,
            artifact_type,
            runs_root,
        )
        if artifact is None:
            connection.rollback()
            return None

        if link["first_download_started_utc"] is None:
            timestamp = _utc_text(current)
            updated = connection.execute(
                """
                UPDATE delivery_links
                SET first_download_started_utc = ?
                WHERE id = ?
                  AND first_download_started_utc IS NULL
                  AND revoked_utc IS NULL
                """,
                (timestamp, link["id"]),
            )
            if updated.rowcount != 1:
                connection.rollback()
                return None
            connection.execute(
                """
                UPDATE runs
                SET status = 'delivered', updated_utc = ?
                WHERE run_id = ?
                """,
                (timestamp, link["run_id"]),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    return artifact


def public_security_headers(*, html_response: bool = False) -> dict[str, str]:
    headers = dict(PUBLIC_BASE_SECURITY_HEADERS)
    if html_response:
        headers["Content-Security-Policy"] = (
            PUBLIC_CONTENT_SECURITY_POLICY
        )
    return headers


def apply_public_security_headers(
    response: Response,
    *,
    html_response: bool = False,
) -> Response:
    for name, value in public_security_headers(
        html_response=html_response
    ).items():
        response.headers[name] = value
    return response


def public_unavailable_response() -> HTMLResponse:
    return HTMLResponse(
        content=PUBLIC_UNAVAILABLE_HTML,
        status_code=404,
        headers=public_security_headers(html_response=True),
    )


DELIVERY_LOG_PATH_PATTERN = re.compile(
    r"(?P<prefix>/d/)[A-Za-z0-9_-]{20,128}"
    r"(?=(?:/|[?#\s\"']|$))"
)


def redact_delivery_tokens(value: str) -> str:
    return DELIVERY_LOG_PATH_PATTERN.sub(
        r"\g<prefix>[REDACTED]",
        value,
    )


def _redact_log_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_delivery_tokens(value)
    if isinstance(value, bytes):
        try:
            redacted = redact_delivery_tokens(value.decode("utf-8"))
        except UnicodeDecodeError:
            return value
        return redacted.encode("utf-8")
    if isinstance(value, tuple):
        return tuple(_redact_log_value(item) for item in value)
    if isinstance(value, list):
        return [_redact_log_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _redact_log_value(item)
            for key, item in value.items()
        }
    return value


class RedactDeliveryTokenFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_log_value(record.msg)
        record.args = _redact_log_value(record.args)

        # Structured logging adapters sometimes place the request path on
        # one of these extra LogRecord attributes rather than in args.
        for attribute in ("path", "url", "request_line", "scope"):
            if hasattr(record, attribute):
                setattr(
                    record,
                    attribute,
                    _redact_log_value(getattr(record, attribute)),
                )
        return True


def _has_redaction_filter(target: Any) -> bool:
    return any(
        isinstance(existing, RedactDeliveryTokenFilter)
        for existing in target.filters
    )


def install_delivery_token_log_redaction() -> RedactDeliveryTokenFilter:
    """
    Install redaction on application and Uvicorn log paths.

    Phase 5 deployment must also configure its reverse proxy/platform not to
    retain raw bearer-token paths; an application filter cannot redact logs
    emitted before a request reaches this process.
    """

    redaction_filter = RedactDeliveryTokenFilter()
    loggers = [
        logging.getLogger(),
        logging.getLogger("lotkit"),
        logging.getLogger("uvicorn"),
        logging.getLogger("uvicorn.access"),
        logging.getLogger("uvicorn.error"),
    ]
    for logger in loggers:
        if not _has_redaction_filter(logger):
            logger.addFilter(redaction_filter)
        for handler in logger.handlers:
            if not _has_redaction_filter(handler):
                handler.addFilter(redaction_filter)
    return redaction_filter
