"""Safe, exact-path storage lifecycle operations for LotKit Runs."""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import sqlite3
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from api.config import get_settings


LOGGER = logging.getLogger("uvicorn.error")
ARTIFACT_TYPES = (
    "photos_zip",
    "sticker_pdf",
    "buyers_guide_pdf",
)
MANIFEST_POLICIES = {
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
REVOCATION_REASONS = frozenset(
    {
        "replaced",
        "manual",
        "reopened",
        "outputs_changed",
        "transport_migrated",
        "expired",
    }
)
MAX_RELATIVE_PATH_BYTES = 512
MAX_MANIFEST_BYTES = 64 * 1024
MAX_JSON_BYTES = 1024 * 1024
MAX_ERROR_LENGTH = 240
WEB_CLEANUP_LIMIT = 24
APPLY_CLEANUP_LIMIT = 10_000
_REASON_PATTERN = re.compile(r"^[a-z0-9_]{1,80}$")


class StorageSafetyError(ValueError):
    """A database-known storage path is not safe for filesystem access."""


@dataclass(frozen=True)
class ValidatedCleanupPath:
    run_id: str
    relative_path: str
    filename: str
    root: Path
    run_directory: Path
    path: Path


@dataclass(frozen=True)
class QueueResult:
    job_id: int
    newly_queued: bool


@dataclass(frozen=True)
class CleanupResult:
    attempted: int = 0
    deleted: int = 0
    already_missing: int = 0
    bytes_reclaimed: int = 0
    failures: int = 0

    def plus(self, other: CleanupResult) -> CleanupResult:
        return CleanupResult(
            attempted=self.attempted + other.attempted,
            deleted=self.deleted + other.deleted,
            already_missing=(
                self.already_missing + other.already_missing
            ),
            bytes_reclaimed=(
                self.bytes_reclaimed + other.bytes_reclaimed
            ),
            failures=self.failures + other.failures,
        )


@dataclass(frozen=True)
class RevocationResult:
    links_revoked: int
    job_ids: tuple[int, ...]
    relative_paths: tuple[str, ...]


@dataclass(frozen=True)
class RetentionCandidate:
    run_id: str
    latest_download_utc: str
    output_keys: tuple[str, ...]
    relative_paths: tuple[str, ...]


@dataclass(frozen=True)
class RetentionResult:
    runs_retired: int
    run_ids: tuple[str, ...]
    job_ids: tuple[int, ...]
    relative_paths: tuple[str, ...]


@dataclass(frozen=True)
class ExpiryResult:
    links_expired: int
    link_ids: tuple[int, ...]
    job_ids: tuple[int, ...]
    relative_paths: tuple[str, ...]
    cleanup: CleanupResult = CleanupResult()


@dataclass(frozen=True)
class StorageReport:
    total_bytes: int
    current_artifact_bytes: int
    active_snapshot_bytes: int
    revoked_snapshot_bytes: int
    pending_cleanup_bytes: int
    loose_photo_bytes: int
    unmanaged_bytes: int
    legacy_bytes: int
    total_files: int
    current_artifact_files: int
    active_snapshot_files: int
    revoked_snapshot_files: int
    pending_cleanup_files: int
    loose_photo_files: int
    unmanaged_files: int
    legacy_files: int
    largest_runs: tuple[tuple[str, int, int], ...]


@dataclass(frozen=True)
class StoragePlan:
    links_to_expire: tuple[tuple[int, str, str], ...]
    runs_to_retire: tuple[RetentionCandidate, ...]
    files_to_queue: tuple[str, ...]
    pending_job_ids: tuple[int, ...]
    estimated_files: int
    estimated_bytes: int


@dataclass(frozen=True)
class StorageApplyResult:
    links_expired: int
    runs_retired: int
    files_deleted: int
    files_already_missing: int
    bytes_reclaimed: int
    failures_pending: int


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
    except (ValueError, OverflowError):
        return None
    return _as_utc(parsed)


def _is_uuid4(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value


def _bounded_utf8(value: str, maximum: int) -> bytes:
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as exc:
        raise StorageSafetyError("Storage text is not valid UTF-8.") from exc
    if len(encoded) > maximum:
        raise StorageSafetyError("Storage text exceeds the safe limit.")
    return encoded


def _runs_root(runs_root: str | Path | None) -> Path:
    settings = get_settings()
    root = settings.runs_dir if runs_root is None else Path(runs_root)
    if not root.is_absolute():
        root = settings.project_root / root
    return root.resolve()


def validate_cleanup_path(
    run_id: str,
    relative_path: str,
    runs_root: str | Path | None = None,
) -> ValidatedCleanupPath:
    """
    Validate one exact Run-relative file path without following symlinks.

    Missing files and directories are accepted because cleanup is
    idempotent. Existing symlinks, directories, and non-regular files are
    rejected.
    """

    if not _is_uuid4(run_id):
        raise StorageSafetyError("Cleanup Run ID must be a canonical UUID4.")
    if not isinstance(relative_path, str):
        raise StorageSafetyError("Cleanup path must be relative text.")
    _bounded_utf8(relative_path, MAX_RELATIVE_PATH_BYTES)
    if (
        not relative_path
        or relative_path.startswith("/")
        or "\\" in relative_path
        or "\x00" in relative_path
    ):
        raise StorageSafetyError("Cleanup path is not a safe relative path.")

    components = relative_path.split("/")
    if (
        len(components) != 2
        or any(not component or component in {".", ".."} for component in components)
        or components[0] != run_id
    ):
        raise StorageSafetyError(
            "Cleanup path must name one file directly beneath its Run."
        )
    filename = components[1]
    _bounded_utf8(filename, 255)

    root = _runs_root(runs_root)
    run_directory = root / run_id
    candidate = run_directory / filename
    if run_directory.parent != root or candidate.parent != run_directory:
        raise StorageSafetyError("Cleanup path escapes the Runs root.")

    try:
        directory_stat = run_directory.lstat()
    except FileNotFoundError:
        directory_stat = None
    except OSError as exc:
        raise StorageSafetyError(
            "Run directory could not be validated."
        ) from exc
    if directory_stat is not None and (
        stat.S_ISLNK(directory_stat.st_mode)
        or not stat.S_ISDIR(directory_stat.st_mode)
    ):
        raise StorageSafetyError("Run directory is not a regular directory.")

    try:
        candidate_stat = candidate.lstat()
    except FileNotFoundError:
        candidate_stat = None
    except OSError as exc:
        raise StorageSafetyError("Cleanup file could not be validated.") from exc
    if candidate_stat is not None and (
        stat.S_ISLNK(candidate_stat.st_mode)
        or not stat.S_ISREG(candidate_stat.st_mode)
    ):
        raise StorageSafetyError(
            "Cleanup target must be a regular non-symlink file."
        )

    return ValidatedCleanupPath(
        run_id=run_id,
        relative_path=relative_path,
        filename=filename,
        root=root,
        run_directory=run_directory,
        path=candidate,
    )


def run_relative_file(
    run_id: str,
    filename: Any,
    runs_root: str | Path | None = None,
) -> str | None:
    if not isinstance(filename, str):
        return None
    cleaned = filename.strip()
    if (
        not cleaned
        or cleaned in {".", ".."}
        or "/" in cleaned
        or "\\" in cleaned
        or "\x00" in cleaned
    ):
        return None
    relative_path = f"{run_id}/{cleaned}"
    try:
        validate_cleanup_path(run_id, relative_path, runs_root)
    except StorageSafetyError:
        return None
    return relative_path


def _parse_json_object(value: Any, maximum: int = MAX_JSON_BYTES) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        return {}
    try:
        _bounded_utf8(value, maximum)
        parsed = json.loads(value)
    except (
        json.JSONDecodeError,
        RecursionError,
        MemoryError,
        StorageSafetyError,
    ):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def safe_manifest_paths(
    manifest_value: Any,
    run_id: str,
    runs_root: str | Path | None = None,
) -> tuple[str, ...]:
    """Extract only well-formed immutable paths from a stored manifest."""

    manifest = _parse_json_object(manifest_value, MAX_MANIFEST_BYTES)
    paths: list[str] = []
    for artifact_type, policy in MANIFEST_POLICIES.items():
        entry = manifest.get(artifact_type)
        if not isinstance(entry, Mapping):
            continue
        if (
            entry.get("download_name") != policy["download_name"]
            or entry.get("content_type") != policy["content_type"]
        ):
            continue
        relative_path = entry.get("relative_path")
        if not isinstance(relative_path, str):
            continue
        try:
            validated = validate_cleanup_path(
                run_id,
                relative_path,
                runs_root,
            )
        except StorageSafetyError:
            continue
        expected_pattern = re.compile(
            rf"^{re.escape(artifact_type)}_[0-9a-f]{{32}}"
            rf"{re.escape(policy['suffix'])}$"
        )
        if expected_pattern.fullmatch(validated.filename) is None:
            continue
        paths.append(validated.relative_path)
    return tuple(dict.fromkeys(paths))


def enqueue_cleanup_job(
    connection: sqlite3.Connection,
    run_id: str,
    relative_path: str,
    reason: str,
    runs_root: str | Path | None = None,
    *,
    now: datetime | None = None,
) -> QueueResult:
    """Insert one idempotent pending job inside the caller's transaction."""

    validate_cleanup_path(run_id, relative_path, runs_root)
    if not isinstance(reason, str) or _REASON_PATTERN.fullmatch(reason) is None:
        raise StorageSafetyError("Cleanup reason is invalid.")
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO storage_cleanup_jobs (
            run_id,
            relative_path,
            reason,
            created_utc
        )
        VALUES (?, ?, ?, ?)
        """,
        (run_id, relative_path, reason, _utc_text(_as_utc(now))),
    )
    row = connection.execute(
        """
        SELECT id
        FROM storage_cleanup_jobs
        WHERE relative_path = ? AND completed_utc IS NULL
        """,
        (relative_path,),
    ).fetchone()
    if row is None:
        raise RuntimeError("Cleanup job could not be loaded.")
    return QueueResult(
        job_id=int(row["id"]),
        newly_queued=cursor.rowcount == 1,
    )


def _known_cleanup_paths(connection: sqlite3.Connection) -> set[str]:
    if not _table_exists(connection, "storage_cleanup_jobs"):
        return set()
    return {
        str(row["relative_path"])
        for row in connection.execute(
            "SELECT relative_path FROM storage_cleanup_jobs"
        )
    }


def _queue_paths(
    connection: sqlite3.Connection,
    run_id: str,
    paths: Iterable[str],
    reason: str,
    runs_root: str | Path | None,
    now: datetime,
    *,
    skip_known_completed: bool = False,
) -> tuple[tuple[int, ...], tuple[str, ...]]:
    known = _known_cleanup_paths(connection) if skip_known_completed else set()
    job_ids: list[int] = []
    queued_paths: list[str] = []
    for relative_path in paths:
        if relative_path in known:
            continue
        try:
            queued = enqueue_cleanup_job(
                connection,
                run_id,
                relative_path,
                reason,
                runs_root,
                now=now,
            )
        except StorageSafetyError:
            continue
        job_ids.append(queued.job_id)
        queued_paths.append(relative_path)
    return tuple(dict.fromkeys(job_ids)), tuple(dict.fromkeys(queued_paths))


def revoke_run_delivery_links(
    connection: sqlite3.Connection,
    run_id: str,
    reason: str,
    runs_root: str | Path | None = None,
    now: datetime | None = None,
    *,
    owner_id: int | None = None,
) -> RevocationResult:
    """Revoke and queue manifests in the caller's current transaction."""

    if reason not in REVOCATION_REASONS:
        raise ValueError("Unsupported delivery-link revocation reason.")
    current = _as_utc(now)
    clauses = ["run_id = ?", "revoked_utc IS NULL"]
    parameters: list[Any] = [run_id]
    if owner_id is not None:
        clauses.append("owner_id = ?")
        parameters.append(owner_id)
    rows = connection.execute(
        f"""
        SELECT id, run_id, artifact_manifest_json
        FROM delivery_links
        WHERE {' AND '.join(clauses)}
        ORDER BY id
        """,
        parameters,
    ).fetchall()

    all_jobs: list[int] = []
    all_paths: list[str] = []
    changed = 0
    for row in rows:
        paths = safe_manifest_paths(
            row["artifact_manifest_json"],
            row["run_id"],
            runs_root,
        )
        job_ids, queued_paths = _queue_paths(
            connection,
            row["run_id"],
            paths,
            f"delivery_{reason}",
            runs_root,
            current,
        )
        updated = connection.execute(
            """
            UPDATE delivery_links
            SET revoked_utc = ?, revocation_reason = ?
            WHERE id = ? AND revoked_utc IS NULL
            """,
            (_utc_text(current), reason, row["id"]),
        )
        if updated.rowcount == 1:
            changed += 1
            all_jobs.extend(job_ids)
            all_paths.extend(queued_paths)
    return RevocationResult(
        links_revoked=changed,
        job_ids=tuple(dict.fromkeys(all_jobs)),
        relative_paths=tuple(dict.fromkeys(all_paths)),
    )


def _expire_links_in_transaction(
    connection: sqlite3.Connection,
    runs_root: str | Path | None,
    current: datetime,
) -> ExpiryResult:
    rows = connection.execute(
        """
        SELECT id, run_id, expires_utc, artifact_manifest_json
        FROM delivery_links
        WHERE revoked_utc IS NULL
        ORDER BY id
        """
    ).fetchall()
    expired_rows = [
        row
        for row in rows
        if (
            (expiry := _parse_utc(row["expires_utc"])) is not None
            and expiry <= current
        )
    ]
    link_ids: list[int] = []
    job_ids: list[int] = []
    relative_paths: list[str] = []
    for row in expired_rows:
        paths = safe_manifest_paths(
            row["artifact_manifest_json"],
            row["run_id"],
            runs_root,
        )
        queued_ids, queued_paths = _queue_paths(
            connection,
            row["run_id"],
            paths,
            "delivery_expired",
            runs_root,
            current,
        )
        updated = connection.execute(
            """
            UPDATE delivery_links
            SET revoked_utc = ?, revocation_reason = 'expired'
            WHERE id = ? AND revoked_utc IS NULL
            """,
            (_utc_text(current), row["id"]),
        )
        if updated.rowcount == 1:
            link_ids.append(int(row["id"]))
            job_ids.extend(queued_ids)
            relative_paths.extend(queued_paths)
    return ExpiryResult(
        links_expired=len(link_ids),
        link_ids=tuple(link_ids),
        job_ids=tuple(dict.fromkeys(job_ids)),
        relative_paths=tuple(dict.fromkeys(relative_paths)),
    )


def expire_delivery_links(
    connection: sqlite3.Connection,
    runs_root: str | Path | None = None,
    now: datetime | None = None,
) -> ExpiryResult:
    """Explicitly revoke expired links, then attempt their queued cleanup."""

    if connection.in_transaction:
        raise RuntimeError("Expiry processing requires a fresh transaction.")
    try:
        connection.execute("BEGIN IMMEDIATE")
        result = _expire_links_in_transaction(
            connection,
            runs_root,
            _as_utc(now),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    cleanup = best_effort_cleanup(
        connection,
        runs_root,
        job_ids=result.job_ids,
    )
    return ExpiryResult(
        links_expired=result.links_expired,
        link_ids=result.link_ids,
        job_ids=result.job_ids,
        relative_paths=result.relative_paths,
        cleanup=cleanup,
    )


def _safe_error(exc: BaseException) -> str:
    detail = getattr(exc, "strerror", None)
    if not isinstance(detail, str) or not detail:
        detail = type(exc).__name__
    cleaned = "".join(
        character if character.isprintable() else "?" for character in detail
    )
    return f"{type(exc).__name__}: {cleaned}"[:MAX_ERROR_LENGTH]


def _path_is_protected(
    connection: sqlite3.Connection,
    run_id: str,
    relative_path: str,
    current: datetime,
    runs_root: str | Path | None,
) -> bool:
    """Revalidate DB references immediately before every unlink attempt."""

    run = connection.execute(
        "SELECT outputs_json FROM runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if run is None:
        return False
    outputs = _parse_json_object(run["outputs_json"])
    if any(
        run_relative_file(run_id, outputs.get(artifact_type), runs_root)
        == relative_path
        for artifact_type in ARTIFACT_TYPES
    ):
        return True

    for link in connection.execute(
        """
        SELECT expires_utc, artifact_manifest_json
        FROM delivery_links
        WHERE run_id = ? AND revoked_utc IS NULL
        """,
        (run_id,),
    ):
        expiry = _parse_utc(link["expires_utc"])
        if (
            expiry is not None
            and expiry > current
            and relative_path
            in safe_manifest_paths(
                link["artifact_manifest_json"],
                run_id,
                runs_root,
            )
        ):
            return True
    return False


def _unlink_exact_file(validated: ValidatedCleanupPath) -> tuple[str, int]:
    """Delete via directory descriptors so a parent-symlink race cannot escape."""

    directory_flag = getattr(os, "O_DIRECTORY", 0)
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
    root_fd: int | None = None
    run_fd: int | None = None
    try:
        try:
            root_fd = os.open(
                validated.root,
                os.O_RDONLY | directory_flag | nofollow_flag,
            )
        except FileNotFoundError:
            return "missing", 0
        try:
            run_fd = os.open(
                validated.run_id,
                os.O_RDONLY | directory_flag | nofollow_flag,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            return "missing", 0
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise StorageSafetyError(
                    "Run directory became unsafe during cleanup."
                ) from exc
            raise

        try:
            file_stat = os.stat(
                validated.filename,
                dir_fd=run_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return "missing", 0
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(
            file_stat.st_mode
        ):
            raise StorageSafetyError(
                "Cleanup target is not a regular non-symlink file."
            )
        size = int(file_stat.st_size)
        try:
            os.unlink(validated.filename, dir_fd=run_fd)
        except FileNotFoundError:
            return "missing", 0
        return "deleted", size
    finally:
        if run_fd is not None:
            os.close(run_fd)
        if root_fd is not None:
            os.close(root_fd)


def process_cleanup_jobs(
    connection: sqlite3.Connection,
    runs_root: str | Path | None = None,
    *,
    job_ids: Sequence[int] | None = None,
    limit: int = WEB_CLEANUP_LIMIT,
    now: datetime | None = None,
) -> CleanupResult:
    """Attempt a bounded set of pending cleanup jobs and persist outcomes."""

    bounded_limit = max(0, min(int(limit), APPLY_CLEANUP_LIMIT))
    if bounded_limit == 0:
        return CleanupResult()
    clauses = ["completed_utc IS NULL"]
    parameters: list[Any] = []
    if job_ids is not None:
        unique_ids = tuple(dict.fromkeys(int(job_id) for job_id in job_ids))
        if not unique_ids:
            return CleanupResult()
        unique_ids = unique_ids[:APPLY_CLEANUP_LIMIT]
        placeholders = ",".join("?" for _ in unique_ids)
        clauses.append(f"id IN ({placeholders})")
        parameters.extend(unique_ids)
    parameters.append(bounded_limit)
    rows = connection.execute(
        f"""
        SELECT id, run_id, relative_path
        FROM storage_cleanup_jobs
        WHERE {' AND '.join(clauses)}
        ORDER BY created_utc, id
        LIMIT ?
        """,
        parameters,
    ).fetchall()

    result = CleanupResult()
    current = _as_utc(now)
    timestamp = _utc_text(current)
    for row in rows:
        try:
            if _path_is_protected(
                connection,
                row["run_id"],
                row["relative_path"],
                current,
                runs_root,
            ):
                raise StorageSafetyError(
                    "Cleanup target is still referenced."
                )
            validated = validate_cleanup_path(
                row["run_id"],
                row["relative_path"],
                runs_root,
            )
            outcome, reclaimed = _unlink_exact_file(validated)
        except Exception as exc:
            connection.execute(
                """
                UPDATE storage_cleanup_jobs
                SET attempt_count = attempt_count + 1,
                    last_attempt_utc = ?,
                    last_error = ?
                WHERE id = ? AND completed_utc IS NULL
                """,
                (timestamp, _safe_error(exc), row["id"]),
            )
            result = result.plus(CleanupResult(attempted=1, failures=1))
            continue

        connection.execute(
            """
            UPDATE storage_cleanup_jobs
            SET attempt_count = attempt_count + 1,
                last_attempt_utc = ?,
                last_error = NULL,
                completed_utc = ?
            WHERE id = ? AND completed_utc IS NULL
            """,
            (timestamp, timestamp, row["id"]),
        )
        if outcome == "deleted":
            result = result.plus(
                CleanupResult(
                    attempted=1,
                    deleted=1,
                    bytes_reclaimed=reclaimed,
                )
            )
        else:
            result = result.plus(
                CleanupResult(attempted=1, already_missing=1)
            )
    connection.commit()
    return result


def best_effort_cleanup(
    connection: sqlite3.Connection,
    runs_root: str | Path | None = None,
    *,
    job_ids: Sequence[int] | None = None,
    limit: int = WEB_CLEANUP_LIMIT,
) -> CleanupResult:
    """Never let post-commit unlink or bookkeeping errors fail a mutation."""

    try:
        return process_cleanup_jobs(
            connection,
            runs_root,
            job_ids=job_ids,
            limit=limit,
        )
    except Exception as exc:
        try:
            connection.rollback()
        except Exception:
            pass
        LOGGER.warning(
            "Storage cleanup remains pending after %s.",
            type(exc).__name__,
        )
        return CleanupResult(failures=1)


def _download_times_for_run(
    connection: sqlite3.Connection,
    run_id: str,
) -> list[datetime]:
    return [
        parsed
        for row in connection.execute(
            """
            SELECT first_download_started_utc
            FROM delivery_links
            WHERE run_id = ? AND first_download_started_utc IS NOT NULL
            """,
            (run_id,),
        )
        if (parsed := _parse_utc(row["first_download_started_utc"]))
        is not None
    ]


def _has_usable_link(
    connection: sqlite3.Connection,
    run_id: str,
    current: datetime,
) -> bool:
    for row in connection.execute(
        """
        SELECT expires_utc
        FROM delivery_links
        WHERE run_id = ? AND revoked_utc IS NULL
        """,
        (run_id,),
    ):
        expiry = _parse_utc(row["expires_utc"])
        if expiry is not None and expiry > current:
            return True
    return False


def select_retention_candidates(
    connection: sqlite3.Connection,
    runs_root: str | Path | None = None,
    *,
    retention_days: int | None = None,
    now: datetime | None = None,
) -> tuple[RetentionCandidate, ...]:
    """Use one shared, read-only eligibility rule for plan and apply."""

    days = (
        get_settings().artifact_retention_days
        if retention_days is None
        else int(retention_days)
    )
    if days <= 0:
        raise ValueError("Retention days must be positive.")
    current = _as_utc(now)
    cutoff = current - timedelta(days=days)
    candidates: list[RetentionCandidate] = []
    for run in connection.execute(
        "SELECT * FROM runs WHERE status = 'delivered' ORDER BY id"
    ):
        download_times = _download_times_for_run(connection, run["run_id"])
        if not download_times:
            continue
        latest_download = max(download_times)
        if latest_download > cutoff:
            continue
        if _has_usable_link(connection, run["run_id"], current):
            continue

        outputs = _parse_json_object(run["outputs_json"])
        output_keys = tuple(
            artifact_type
            for artifact_type in ARTIFACT_TYPES
            if (
                isinstance(outputs.get(artifact_type), str)
                and bool(outputs[artifact_type].strip())
            )
        )
        if not output_keys:
            continue
        paths = tuple(
            relative_path
            for artifact_type in output_keys
            if (
                relative_path := run_relative_file(
                    run["run_id"],
                    outputs.get(artifact_type),
                    runs_root,
                )
            )
            is not None
        )
        candidates.append(
            RetentionCandidate(
                run_id=run["run_id"],
                latest_download_utc=_utc_text(latest_download),
                output_keys=output_keys,
                relative_paths=tuple(dict.fromkeys(paths)),
            )
        )
    return tuple(candidates)


def _retire_candidates_in_transaction(
    connection: sqlite3.Connection,
    runs_root: str | Path | None,
    current: datetime,
    retention_days: int,
) -> RetentionResult:
    candidates = select_retention_candidates(
        connection,
        runs_root,
        retention_days=retention_days,
        now=current,
    )
    run_ids: list[str] = []
    job_ids: list[int] = []
    paths: list[str] = []
    for candidate in candidates:
        row = connection.execute(
            """
            SELECT outputs_json
            FROM runs
            WHERE run_id = ? AND status = 'delivered'
            """,
            (candidate.run_id,),
        ).fetchone()
        if row is None:
            continue
        outputs = _parse_json_object(row["outputs_json"])
        queued_ids, queued_paths = _queue_paths(
            connection,
            candidate.run_id,
            candidate.relative_paths,
            "retention_expired",
            runs_root,
            current,
        )
        for artifact_type in ARTIFACT_TYPES:
            outputs.pop(artifact_type, None)
        updated = connection.execute(
            """
            UPDATE runs
            SET outputs_json = ?,
                artifacts_purged_utc = ?,
                updated_utc = ?
            WHERE run_id = ?
              AND status = 'delivered'
              AND outputs_json = ?
            """,
            (
                json.dumps(
                    outputs,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                _utc_text(current),
                _utc_text(current),
                candidate.run_id,
                row["outputs_json"],
            ),
        )
        if updated.rowcount == 1:
            run_ids.append(candidate.run_id)
            job_ids.extend(queued_ids)
            paths.extend(queued_paths)
    return RetentionResult(
        runs_retired=len(run_ids),
        run_ids=tuple(run_ids),
        job_ids=tuple(dict.fromkeys(job_ids)),
        relative_paths=tuple(dict.fromkeys(paths)),
    )


def retire_eligible_runs(
    connection: sqlite3.Connection,
    runs_root: str | Path | None = None,
    *,
    retention_days: int | None = None,
    now: datetime | None = None,
) -> RetentionResult:
    if connection.in_transaction:
        raise RuntimeError("Retention processing requires a fresh transaction.")
    days = (
        get_settings().artifact_retention_days
        if retention_days is None
        else int(retention_days)
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        result = _retire_candidates_in_transaction(
            connection,
            runs_root,
            _as_utc(now),
            days,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    best_effort_cleanup(
        connection,
        runs_root,
        job_ids=result.job_ids,
    )
    return result


def _reconcile_revoked_in_transaction(
    connection: sqlite3.Connection,
    runs_root: str | Path | None,
    current: datetime,
) -> tuple[tuple[int, ...], tuple[str, ...]]:
    all_job_ids: list[int] = []
    all_paths: list[str] = []
    for row in connection.execute(
        """
        SELECT run_id, revocation_reason, artifact_manifest_json
        FROM delivery_links
        WHERE revoked_utc IS NOT NULL
        ORDER BY id
        """
    ):
        paths = safe_manifest_paths(
            row["artifact_manifest_json"],
            row["run_id"],
            runs_root,
        )
        raw_reason = str(row["revocation_reason"] or "revoked")
        reason = (
            f"delivery_{raw_reason}"
            if _REASON_PATTERN.fullmatch(f"delivery_{raw_reason}")
            else "delivery_revoked"
        )
        job_ids, queued_paths = _queue_paths(
            connection,
            row["run_id"],
            paths,
            reason,
            runs_root,
            current,
            skip_known_completed=True,
        )
        all_job_ids.extend(job_ids)
        all_paths.extend(queued_paths)
    return (
        tuple(dict.fromkeys(all_job_ids)),
        tuple(dict.fromkeys(all_paths)),
    )


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = ?
            """,
            (name,),
        ).fetchone()
        is not None
    )


def _job_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    if not _table_exists(connection, "storage_cleanup_jobs"):
        return []
    return connection.execute(
        "SELECT * FROM storage_cleanup_jobs ORDER BY created_utc, id"
    ).fetchall()


def _safe_file_size(
    run_id: str,
    relative_path: str,
    runs_root: str | Path | None,
) -> int | None:
    try:
        path = validate_cleanup_path(run_id, relative_path, runs_root).path
        file_stat = path.lstat()
    except (StorageSafetyError, FileNotFoundError, OSError):
        return None
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        return None
    return int(file_stat.st_size)


def _classified_database_paths(
    connection: sqlite3.Connection,
    runs_root: str | Path | None,
    current: datetime,
) -> tuple[set[str], set[str], set[str], set[str], set[str]]:
    current_paths: set[str] = set()
    active_paths: set[str] = set()
    revoked_paths: set[str] = set()
    report_paths: set[str] = set()
    run_ids: set[str] = set()
    for run in connection.execute("SELECT run_id, outputs_json FROM runs"):
        run_id = run["run_id"]
        if not _is_uuid4(run_id):
            continue
        run_ids.add(run_id)
        report_paths.add(f"{run_id}/run_report.json")
        outputs = _parse_json_object(run["outputs_json"])
        for artifact_type in ARTIFACT_TYPES:
            relative_path = run_relative_file(
                run_id,
                outputs.get(artifact_type),
                runs_root,
            )
            if relative_path is not None:
                current_paths.add(relative_path)
    for link in connection.execute(
        """
        SELECT run_id, expires_utc, revoked_utc, artifact_manifest_json
        FROM delivery_links
        """
    ):
        paths = set(
            safe_manifest_paths(
                link["artifact_manifest_json"],
                link["run_id"],
                runs_root,
            )
        )
        expiry = _parse_utc(link["expires_utc"])
        if (
            link["revoked_utc"] is None
            and expiry is not None
            and expiry > current
        ):
            active_paths.update(paths)
        else:
            revoked_paths.update(paths)
    return current_paths, active_paths, revoked_paths, report_paths, run_ids


def storage_report(
    connection: sqlite3.Connection,
    runs_root: str | Path | None = None,
    *,
    now: datetime | None = None,
    largest_limit: int = 10,
) -> StorageReport:
    """Measure storage without changing SQLite or the filesystem."""

    root = _runs_root(runs_root)
    current = _as_utc(now)
    (
        current_paths,
        active_paths,
        revoked_paths,
        report_paths,
        current_run_ids,
    ) = _classified_database_paths(connection, root, current)
    pending_paths = {
        str(row["relative_path"])
        for row in _job_rows(connection)
        if row["completed_utc"] is None
    }

    category_paths = (
        current_paths,
        active_paths,
        revoked_paths,
        pending_paths,
    )
    category_sizes: list[dict[str, int]] = []
    for paths in category_paths:
        sizes: dict[str, int] = {}
        for relative_path in paths:
            size = _safe_file_size(
                relative_path.split("/", 1)[0],
                relative_path,
                root,
            )
            if size is not None:
                sizes[relative_path] = size
        category_sizes.append(sizes)

    total_bytes = 0
    total_files = 0
    loose_bytes = 0
    loose_files = 0
    unmanaged_bytes = 0
    unmanaged_files = 0
    legacy_bytes = 0
    legacy_files = 0
    run_totals: dict[str, tuple[int, int]] = {}
    known_paths = set().union(
        current_paths,
        active_paths,
        revoked_paths,
        pending_paths,
        report_paths,
    )
    photo_suffixes = {".jpg", ".jpeg", ".png", ".webp"}

    if root.is_dir():
        for directory_path, directory_names, filenames in os.walk(
            root,
            topdown=True,
            followlinks=False,
        ):
            directory = Path(directory_path)
            directory_names[:] = [
                name
                for name in directory_names
                if not (directory / name).is_symlink()
            ]
            try:
                relative_directory = directory.relative_to(root)
            except ValueError:
                continue
            top_component = (
                relative_directory.parts[0]
                if relative_directory.parts
                else None
            )
            for filename in filenames:
                path = directory / filename
                try:
                    file_stat = path.lstat()
                except OSError:
                    continue
                if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(
                    file_stat.st_mode
                ):
                    continue
                size = int(file_stat.st_size)
                total_bytes += size
                total_files += 1
                relative_path = path.relative_to(root).as_posix()
                is_current_run = (
                    top_component is not None
                    and top_component in current_run_ids
                    and len(Path(relative_path).parts) >= 2
                )
                if is_current_run:
                    run_bytes, run_files = run_totals.get(
                        top_component,
                        (0, 0),
                    )
                    run_totals[top_component] = (
                        run_bytes + size,
                        run_files + 1,
                    )
                    is_loose = (
                        len(Path(relative_path).parts) == 2
                        and path.suffix.lower() in photo_suffixes
                    )
                    if is_loose:
                        loose_bytes += size
                        loose_files += 1
                    elif relative_path not in known_paths:
                        unmanaged_bytes += size
                        unmanaged_files += 1
                elif top_component is not None and not _is_uuid4(
                    top_component
                ):
                    legacy_bytes += size
                    legacy_files += 1
                else:
                    unmanaged_bytes += size
                    unmanaged_files += 1

    largest = tuple(
        (run_id, total[0], total[1])
        for run_id, total in sorted(
            run_totals.items(),
            key=lambda item: (-item[1][0], item[0]),
        )[: max(0, int(largest_limit))]
    )
    return StorageReport(
        total_bytes=total_bytes,
        current_artifact_bytes=sum(category_sizes[0].values()),
        active_snapshot_bytes=sum(category_sizes[1].values()),
        revoked_snapshot_bytes=sum(category_sizes[2].values()),
        pending_cleanup_bytes=sum(category_sizes[3].values()),
        loose_photo_bytes=loose_bytes,
        unmanaged_bytes=unmanaged_bytes,
        legacy_bytes=legacy_bytes,
        total_files=total_files,
        current_artifact_files=len(category_sizes[0]),
        active_snapshot_files=len(category_sizes[1]),
        revoked_snapshot_files=len(category_sizes[2]),
        pending_cleanup_files=len(category_sizes[3]),
        loose_photo_files=loose_files,
        unmanaged_files=unmanaged_files,
        legacy_files=legacy_files,
        largest_runs=largest,
    )


def _links_to_expire(
    connection: sqlite3.Connection,
    current: datetime,
) -> tuple[tuple[int, str, str], ...]:
    result: list[tuple[int, str, str]] = []
    for row in connection.execute(
        """
        SELECT id, run_id, expires_utc
        FROM delivery_links
        WHERE revoked_utc IS NULL
        ORDER BY id
        """
    ):
        expiry = _parse_utc(row["expires_utc"])
        if expiry is not None and expiry <= current:
            result.append(
                (int(row["id"]), str(row["run_id"]), str(row["expires_utc"]))
            )
    return tuple(result)


def storage_plan(
    connection: sqlite3.Connection,
    runs_root: str | Path | None = None,
    *,
    retention_days: int | None = None,
    now: datetime | None = None,
) -> StoragePlan:
    """Plan the same lifecycle selection as apply without writing anything."""

    current = _as_utc(now)
    known_jobs = _known_cleanup_paths(connection)
    pending_job_ids = tuple(
        int(row["id"])
        for row in _job_rows(connection)
        if row["completed_utc"] is None
    )
    links = _links_to_expire(connection, current)
    planned_paths: set[str] = set()
    expiring_ids = {link_id for link_id, _run_id, _expiry in links}
    for row in connection.execute(
        """
        SELECT id, run_id, revoked_utc, artifact_manifest_json
        FROM delivery_links
        ORDER BY id
        """
    ):
        if row["revoked_utc"] is None and int(row["id"]) not in expiring_ids:
            continue
        planned_paths.update(
            path
            for path in safe_manifest_paths(
                row["artifact_manifest_json"],
                row["run_id"],
                runs_root,
            )
            if path not in known_jobs
        )

    candidates = select_retention_candidates(
        connection,
        runs_root,
        retention_days=retention_days,
        now=current,
    )
    for candidate in candidates:
        planned_paths.update(
            path for path in candidate.relative_paths if path not in known_jobs
        )

    attempted_paths = set(planned_paths)
    attempted_paths.update(
        str(row["relative_path"])
        for row in _job_rows(connection)
        if row["completed_utc"] is None
    )
    measured_sizes: list[int] = []
    for relative_path in attempted_paths:
        size = _safe_file_size(
            relative_path.split("/", 1)[0],
            relative_path,
            runs_root,
        )
        if size is not None:
            measured_sizes.append(size)
    return StoragePlan(
        links_to_expire=links,
        runs_to_retire=candidates,
        files_to_queue=tuple(sorted(planned_paths)),
        pending_job_ids=pending_job_ids,
        estimated_files=len(measured_sizes),
        estimated_bytes=sum(measured_sizes),
    )


def apply_storage_lifecycle(
    connection: sqlite3.Connection,
    runs_root: str | Path | None = None,
    *,
    retention_days: int | None = None,
    now: datetime | None = None,
) -> StorageApplyResult:
    """Apply expiry, reconciliation, retention, then pending deletion."""

    if connection.in_transaction:
        raise RuntimeError("Storage apply requires a fresh transaction.")
    current = _as_utc(now)
    days = (
        get_settings().artifact_retention_days
        if retention_days is None
        else int(retention_days)
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        expiry = _expire_links_in_transaction(connection, runs_root, current)
        _reconcile_revoked_in_transaction(connection, runs_root, current)
        retention = _retire_candidates_in_transaction(
            connection,
            runs_root,
            current,
            days,
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    cleanup = process_cleanup_jobs(
        connection,
        runs_root,
        limit=APPLY_CLEANUP_LIMIT,
        now=current,
    )
    pending = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM storage_cleanup_jobs
            WHERE completed_utc IS NULL
            """
        ).fetchone()[0]
    )
    return StorageApplyResult(
        links_expired=expiry.links_expired,
        runs_retired=retention.runs_retired,
        files_deleted=cleanup.deleted,
        files_already_missing=cleanup.already_missing,
        bytes_reclaimed=cleanup.bytes_reclaimed,
        failures_pending=pending,
    )
