import csv
import json
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Mapping, Sequence

from api.config import get_settings

RUN_STATUSES = frozenset({"in_progress", "ready", "delivered"})
ARTIFACT_TYPES = frozenset(
    {"photos_zip", "sticker_pdf", "buyers_guide_pdf"}
)
OUTPUT_KEYS = ARTIFACT_TYPES | {"buyers_guide_version"}
BUYERS_GUIDE_VERSIONS = frozenset({"as_is", "implied_only"})

DEALERSHIP_SNAPSHOT_FIELDS = (
    "nickname",
    "dealership_name",
    "address",
    "phone",
    "email",
    "sticker_footer_text",
    "logo_path",
)

CSV_COLUMNS = (
    "run_id",
    "created_utc",
    "updated_utc",
    "status",
    "vin",
    "year",
    "make",
    "model",
    "trim",
    "price",
    "exterior_colour",
    "interior_colour",
    "dealership_nickname",
    "photo_count",
    "has_sticker",
    "has_buyers_guide",
    "buyers_guide_version",
)

UNSET = object()


class RunError(Exception):
    """Base class for expected Run service errors."""


class RunNotFoundError(RunError):
    """The Run does not exist for the current owner."""


class RunConflictError(RunError):
    """A supplied Run belongs to a different car or dealership."""


class RunStatusConflictError(RunConflictError):
    """The requested transition is not valid for the Run's status."""


class RunDeliveredError(RunStatusConflictError):
    """A delivered Run must be explicitly reopened before it can change."""


class DealershipNotFoundError(RunError):
    """The dealership does not exist for the current owner."""


class InvalidRunDataError(RunError, ValueError):
    """Run data or a filesystem identifier is unsafe or malformed."""


@dataclass(frozen=True)
class RunTarget:
    """A validated existing Run, or an unpersisted UUID for a new Run."""

    run_id: str
    is_new: bool


@dataclass(frozen=True)
class RunFilters:
    vin: str | None = None
    dealership_id: int | None = None
    status: str | None = None
    date_from: str | None = None
    date_to: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    """Return a random, opaque UUID4 suitable for both DB and folder names."""

    return str(uuid.uuid4())


def is_uuid4(value: str) -> bool:
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        return False
    return parsed.version == 4 and str(parsed) == value


def parse_json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_json_list(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def serialize_json(value: Mapping[str, Any] | Sequence[Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _clean_filename(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise InvalidRunDataError(f"{field_name} must be a filename.")
    filename = value.strip()
    if (
        not filename
        or filename in {".", ".."}
        or "\x00" in filename
        or "/" in filename
        or "\\" in filename
        or Path(filename).is_absolute()
        or Path(filename).name != filename
    ):
        raise InvalidRunDataError(f"{field_name} must be a safe filename.")
    return filename


def normalize_output_updates(values: Mapping[str, Any]) -> dict[str, str]:
    unknown = set(values) - OUTPUT_KEYS
    if unknown:
        raise InvalidRunDataError(
            f"Unknown Run output field: {sorted(unknown)[0]}."
        )

    normalized: dict[str, str] = {}
    for artifact_type in ARTIFACT_TYPES:
        if artifact_type in values:
            normalized[artifact_type] = _clean_filename(
                values[artifact_type],
                artifact_type,
            )

    if "buyers_guide_version" in values:
        version = values["buyers_guide_version"]
        if version not in BUYERS_GUIDE_VERSIONS:
            raise InvalidRunDataError("Invalid Buyers Guide version.")
        normalized["buyers_guide_version"] = str(version)

    if not any(key in normalized for key in ARTIFACT_TYPES):
        raise InvalidRunDataError(
            "A successful durable artifact is required to save a Run."
        )
    return normalized


def normalize_photo_order(value: Sequence[Any] | None) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)):
        raise InvalidRunDataError("Photo order must be a list of filenames.")
    return [
        _clean_filename(filename, "photo_order")
        for filename in value
    ]


def get_run_row(
    connection: sqlite3.Connection,
    owner_id: int,
    run_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT *
        FROM runs
        WHERE run_id = ? AND owner_id = ?
        """,
        (run_id, owner_id),
    ).fetchone()


def get_dealership_snapshot(
    connection: sqlite3.Connection,
    owner_id: int,
    dealership_id: int | None,
) -> dict[str, Any]:
    if dealership_id is None:
        return {}

    row = connection.execute(
        """
        SELECT nickname,
               dealership_name,
               address,
               phone,
               email,
               sticker_footer_text,
               logo_path
        FROM dealership_profiles
        WHERE id = ? AND owner_id = ?
        """,
        (dealership_id, owner_id),
    ).fetchone()
    if row is None:
        raise DealershipNotFoundError(
            "Dealership profile not found for this owner."
        )
    return {
        field: (
            row[field]
            if row[field] is not None
            else (None if field == "logo_path" else "")
        )
        for field in DEALERSHIP_SNAPSHOT_FIELDS
    }


# Backwards-friendly, concise name for endpoint integrations.
snapshot_dealership = get_dealership_snapshot


def normalize_dealership_snapshot(
    value: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise InvalidRunDataError(
            "Dealership snapshot must be a JSON object."
        )
    return {
        field: (
            value.get(field)
            if value.get(field) is not None
            else (None if field == "logo_path" else "")
        )
        for field in DEALERSHIP_SNAPSHOT_FIELDS
    }


def prepare_run_target(
    connection: sqlite3.Connection,
    owner_id: int,
    vin: str,
    dealership_id: int | None,
    requested_run_id: str | None = None,
    *,
    preallocated_run_id: str | None = None,
) -> RunTarget:
    """
    Validate an existing target before generation, or reserve no DB state.

    For a new car output, this only returns a UUID. The caller writes the
    durable artifact first, then calls record_successful_output().
    """

    if requested_run_id:
        row = get_run_row(connection, owner_id, requested_run_id)
        if row is None:
            raise RunNotFoundError("Run not found.")
        if row["status"] == "delivered":
            raise RunDeliveredError(
                "Delivered Runs must be reopened before they can change."
            )

        stored_dealership_id = row["dealership_id"]
        if (
            row["vin"] != vin
            or stored_dealership_id != dealership_id
        ):
            raise RunConflictError(
                "Run VIN or dealership does not match this vehicle."
            )
        return RunTarget(run_id=requested_run_id, is_new=False)

    # Reject a cross-owner or missing dealership before artifact generation.
    if dealership_id is not None:
        get_dealership_snapshot(connection, owner_id, dealership_id)

    allocated = preallocated_run_id or new_run_id()
    if not is_uuid4(allocated):
        raise InvalidRunDataError("New Run ID must be a canonical UUID4.")
    return RunTarget(run_id=allocated, is_new=True)


# Alternative wording used by some endpoint call sites.
prepare_run = prepare_run_target


def _select_value(
    value: Any,
    existing: sqlite3.Row | None,
    column: str,
) -> Any:
    if value is not UNSET:
        return value
    return existing[column] if existing is not None else None


def record_successful_output(
    connection: sqlite3.Connection,
    owner_id: int,
    target: RunTarget,
    *,
    vin: str,
    dealership_id: int | None,
    output_updates: Mapping[str, Any],
    vehicle: Mapping[str, Any] | None | object = UNSET,
    price: str | None | object = UNSET,
    exterior_colour: str | None | object = UNSET,
    interior_colour: str | None | object = UNSET,
    photo_order: Sequence[str] | None | object = UNSET,
    dealership_snapshot: Mapping[str, Any] | None | object = UNSET,
) -> dict[str, Any]:
    """
    Insert or update a Run only after its output file has been written.

    output_updates is merged into the existing outputs JSON, allowing all
    artifacts for one car session to remain on exactly one Run.
    """

    normalized_outputs = normalize_output_updates(output_updates)
    if vehicle is not UNSET and vehicle is not None and not isinstance(
        vehicle, Mapping
    ):
        raise InvalidRunDataError("Vehicle must be a JSON object.")
    normalized_order = (
        UNSET
        if photo_order is UNSET
        else normalize_photo_order(photo_order)
    )

    try:
        # This is the single mutation transaction for every output type.
        # It rechecks lifecycle state after generation has succeeded, so a
        # concurrent public delivery can still lock the Run before mutation.
        connection.execute("BEGIN IMMEDIATE")
        existing = get_run_row(connection, owner_id, target.run_id)

        if target.is_new:
            if existing is not None:
                raise RunConflictError("Run ID is already in use.")
            if not is_uuid4(target.run_id):
                raise InvalidRunDataError(
                    "New Run ID must be a canonical UUID4."
                )
        else:
            if existing is None:
                raise RunNotFoundError("Run not found.")
            if existing["status"] == "delivered":
                raise RunDeliveredError(
                    "Delivered Runs must be reopened before they can change."
                )
            if (
                existing["vin"] != vin
                or existing["dealership_id"] != dealership_id
            ):
                raise RunConflictError(
                    "Run VIN or dealership does not match this vehicle."
                )

        old_outputs = (
            parse_json_object(existing["outputs_json"]) if existing else {}
        )
        outputs = {**old_outputs, **normalized_outputs}

        if vehicle is UNSET:
            vehicle_json = existing["vehicle_json"] if existing else None
        elif vehicle is None:
            vehicle_json = None
        else:
            vehicle_json = serialize_json(dict(vehicle))

        if normalized_order is UNSET:
            photo_order_json = (
                existing["photo_order_json"] if existing else None
            )
        else:
            photo_order_json = (
                serialize_json(normalized_order)
                if normalized_order is not None
                else None
            )

        if dealership_snapshot is not UNSET:
            resolved_dealership_snapshot = normalize_dealership_snapshot(
                dealership_snapshot
            )
        elif dealership_id is not None:
            resolved_dealership_snapshot = get_dealership_snapshot(
                connection,
                owner_id,
                dealership_id,
            )
        elif existing is not None:
            # ON DELETE SET NULL must not erase the historical snapshot.
            resolved_dealership_snapshot = parse_json_object(
                existing["dealership_snapshot_json"]
            )
        else:
            resolved_dealership_snapshot = {}

        timestamp = utc_now()
        if target.is_new:
            connection.execute(
                """
                INSERT INTO runs (
                    run_id,
                    owner_id,
                    dealership_id,
                    dealership_snapshot_json,
                    vin,
                    vehicle_json,
                    price,
                    exterior_colour,
                    interior_colour,
                    photo_order_json,
                    outputs_json,
                    status,
                    created_utc,
                    updated_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', ?, ?)
                """,
                (
                    target.run_id,
                    owner_id,
                    dealership_id,
                    serialize_json(resolved_dealership_snapshot),
                    vin,
                    vehicle_json,
                    _select_value(price, None, "price"),
                    _select_value(
                        exterior_colour,
                        None,
                        "exterior_colour",
                    ),
                    _select_value(
                        interior_colour,
                        None,
                        "interior_colour",
                    ),
                    photo_order_json,
                    serialize_json(outputs),
                    timestamp,
                    timestamp,
                ),
            )
        else:
            # Successful changes invalidate every outstanding bearer link.
            # Historical rows remain available for audit.
            connection.execute(
                """
                UPDATE delivery_links
                SET revoked_utc = ?,
                    revocation_reason = 'outputs_changed'
                WHERE run_id = ? AND revoked_utc IS NULL
                """,
                (timestamp, target.run_id),
            )
            connection.execute(
                """
                UPDATE runs
                SET dealership_snapshot_json = ?,
                    vehicle_json = ?,
                    price = ?,
                    exterior_colour = ?,
                    interior_colour = ?,
                    photo_order_json = ?,
                    outputs_json = ?,
                    status = CASE
                        WHEN status = 'ready' THEN 'in_progress'
                        ELSE status
                    END,
                    updated_utc = ?
                WHERE run_id = ? AND owner_id = ?
                """,
                (
                    serialize_json(resolved_dealership_snapshot),
                    vehicle_json,
                    _select_value(price, existing, "price"),
                    _select_value(
                        exterior_colour,
                        existing,
                        "exterior_colour",
                    ),
                    _select_value(
                        interior_colour,
                        existing,
                        "interior_colour",
                    ),
                    photo_order_json,
                    serialize_json(outputs),
                    timestamp,
                    target.run_id,
                    owner_id,
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    detail = get_run_detail(connection, owner_id, target.run_id)
    if detail is None:
        raise RuntimeError("Saved Run could not be loaded.")
    return detail


# Integration alias that emphasizes the create-or-update behavior.
create_or_update_run = record_successful_output


def _safe_uuid_run_directory(
    run_id: str,
    runs_root: str | Path | None = None,
) -> Path:
    if not is_uuid4(run_id):
        raise InvalidRunDataError("Run ID must be a canonical UUID4.")

    settings = get_settings()
    if runs_root is None:
        root = settings.runs_dir
    else:
        root = Path(runs_root).expanduser()
        if not root.is_absolute():
            root = settings.project_root / root
    root = root.resolve()
    configured_root = settings.runs_dir.resolve()
    data_root = settings.data_dir.resolve()
    if (
        not configured_root.is_relative_to(data_root)
        or (
            settings.environment == "production"
            and root != configured_root
        )
    ):
        raise InvalidRunDataError("Unsafe Run storage root.")
    lexical_candidate = root / run_id
    candidate = lexical_candidate.resolve()
    if candidate != lexical_candidate or candidate.parent != root:
        raise InvalidRunDataError("Unsafe Run directory.")
    return candidate


def safe_run_directory(
    run_id: str,
    runs_root: str | Path | None = None,
) -> Path:
    return _safe_uuid_run_directory(run_id, runs_root)


def create_run_directory(
    run_id: str,
    runs_root: str | Path | None = None,
) -> Path:
    directory = _safe_uuid_run_directory(run_id, runs_root)
    root = directory.parent
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.mkdir(mode=0o700, exist_ok=False)
    return directory


def _run_response(row: sqlite3.Row) -> dict[str, Any]:
    vehicle = parse_json_object(row["vehicle_json"])
    dealership = parse_json_object(row["dealership_snapshot_json"])
    outputs = parse_json_object(row["outputs_json"])
    photo_order = parse_json_list(row["photo_order_json"])

    return {
        "run_id": row["run_id"],
        "dealership_id": row["dealership_id"],
        "dealership_snapshot": dealership,
        "vin": row["vin"],
        "vehicle": vehicle,
        "price": row["price"] or "",
        "exterior_colour": row["exterior_colour"] or "",
        "interior_colour": row["interior_colour"] or "",
        "photo_order": photo_order,
        "outputs": outputs,
        "status": row["status"],
        "created_utc": row["created_utc"],
        "updated_utc": row["updated_utc"],
        "photo_count": len(photo_order),
        "has_photos": bool(outputs.get("photos_zip")),
        "has_sticker": bool(outputs.get("sticker_pdf")),
        "has_buyers_guide": bool(outputs.get("buyers_guide_pdf")),
    }


def get_run_detail(
    connection: sqlite3.Connection,
    owner_id: int,
    run_id: str,
) -> dict[str, Any] | None:
    row = get_run_row(connection, owner_id, run_id)
    return _run_response(row) if row is not None else None


def _normalize_date_filter(value: str, *, end: bool) -> str:
    candidate = value.strip()
    try:
        if len(candidate) == 10:
            parsed_date = date.fromisoformat(candidate)
            parsed = datetime.combine(
                parsed_date,
                time.max if end else time.min,
                tzinfo=timezone.utc,
            )
        else:
            parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed = parsed.astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise InvalidRunDataError("Invalid Run date filter.") from exc
    return parsed.isoformat()


def _filter_sql(
    owner_id: int,
    filters: RunFilters,
) -> tuple[str, list[Any]]:
    clauses = ["owner_id = ?"]
    parameters: list[Any] = [owner_id]

    if filters.vin:
        escaped = (
            filters.vin.strip().upper()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        clauses.append("UPPER(vin) LIKE ? ESCAPE '\\'")
        parameters.append(f"%{escaped}%")

    if filters.dealership_id is not None:
        clauses.append("dealership_id = ?")
        parameters.append(filters.dealership_id)

    if filters.status:
        if filters.status not in RUN_STATUSES:
            raise InvalidRunDataError("Invalid Run status filter.")
        clauses.append("status = ?")
        parameters.append(filters.status)

    if filters.date_from:
        clauses.append("created_utc >= ?")
        parameters.append(
            _normalize_date_filter(filters.date_from, end=False)
        )

    if filters.date_to:
        clauses.append("created_utc <= ?")
        parameters.append(_normalize_date_filter(filters.date_to, end=True))

    return " AND ".join(clauses), parameters


def _query_run_rows(
    connection: sqlite3.Connection,
    owner_id: int,
    filters: RunFilters,
) -> list[sqlite3.Row]:
    where_sql, parameters = _filter_sql(owner_id, filters)
    return connection.execute(
        f"""
        SELECT *
        FROM runs
        WHERE {where_sql}
        ORDER BY updated_utc DESC, id DESC
        """,
        parameters,
    ).fetchall()


def list_runs(
    connection: sqlite3.Connection,
    owner_id: int,
    *,
    vin: str | None = None,
    dealership_id: int | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[dict[str, Any]]:
    filters = RunFilters(
        vin=vin,
        dealership_id=dealership_id,
        status=status,
        date_from=date_from,
        date_to=date_to,
    )
    summaries: list[dict[str, Any]] = []
    for row in _query_run_rows(connection, owner_id, filters):
        detail = _run_response(row)
        vehicle = detail["vehicle"]
        dealership = detail["dealership_snapshot"]
        summaries.append(
            {
                "run_id": detail["run_id"],
                "vin": detail["vin"],
                "year": str(vehicle.get("year") or ""),
                "make": str(vehicle.get("make") or ""),
                "model": str(vehicle.get("model") or ""),
                "dealership_nickname": str(
                    dealership.get("nickname") or ""
                ),
                "status": detail["status"],
                "outputs": detail["outputs"],
                "has_photos": detail["has_photos"],
                "has_sticker": detail["has_sticker"],
                "has_buyers_guide": detail["has_buyers_guide"],
                "created_utc": detail["created_utc"],
                "updated_utc": detail["updated_utc"],
            }
        )
    return summaries


def mark_run_ready(
    connection: sqlite3.Connection,
    owner_id: int,
    run_id: str,
) -> dict[str, Any]:
    row = get_run_row(connection, owner_id, run_id)
    if row is None:
        raise RunNotFoundError("Run not found.")
    if row["status"] not in {"in_progress", "ready"}:
        raise RunStatusConflictError(
            "Only an in-progress Run can be marked ready."
        )

    connection.execute(
        """
        UPDATE runs
        SET status = 'ready', updated_utc = ?
        WHERE run_id = ? AND owner_id = ?
        """,
        (utc_now(), run_id, owner_id),
    )
    connection.commit()
    detail = get_run_detail(connection, owner_id, run_id)
    if detail is None:
        raise RuntimeError("Updated Run could not be loaded.")
    return detail


def discard_run(
    connection: sqlite3.Connection,
    owner_id: int,
    run_id: str,
    runs_root: str | Path | None = None,
) -> None:
    row = get_run_row(connection, owner_id, run_id)
    if row is None:
        raise RunNotFoundError("Run not found.")
    if row["status"] != "in_progress":
        raise RunStatusConflictError(
            "Ready or delivered Runs cannot be discarded."
        )

    directory = _safe_uuid_run_directory(run_id, runs_root)
    if directory.exists():
        if not directory.is_dir():
            raise InvalidRunDataError("Run storage path is not a directory.")
        shutil.rmtree(directory)

    connection.execute(
        "DELETE FROM runs WHERE run_id = ? AND owner_id = ?",
        (run_id, owner_id),
    )
    connection.commit()


def resolve_artifact_path(
    connection: sqlite3.Connection,
    owner_id: int,
    run_id: str,
    artifact_type: str,
    runs_root: str | Path | None = None,
) -> Path | None:
    if artifact_type not in ARTIFACT_TYPES:
        return None

    row = get_run_row(connection, owner_id, run_id)
    if row is None:
        return None

    outputs = parse_json_object(row["outputs_json"])
    try:
        filename = _clean_filename(outputs.get(artifact_type), artifact_type)
        directory = _safe_uuid_run_directory(run_id, runs_root)
    except InvalidRunDataError:
        return None

    candidate = (directory / filename).resolve()
    if candidate.parent != directory or not candidate.is_file():
        return None
    return candidate


def export_runs_csv(
    connection: sqlite3.Connection,
    owner_id: int,
    *,
    vin: str | None = None,
    dealership_id: int | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> str:
    filters = RunFilters(
        vin=vin,
        dealership_id=dealership_id,
        status=status,
        date_from=date_from,
        date_to=date_to,
    )
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS)
    writer.writeheader()

    for row in _query_run_rows(connection, owner_id, filters):
        detail = _run_response(row)
        vehicle = detail["vehicle"]
        dealership = detail["dealership_snapshot"]
        outputs = detail["outputs"]
        writer.writerow(
            {
                "run_id": detail["run_id"],
                "created_utc": detail["created_utc"],
                "updated_utc": detail["updated_utc"],
                "status": detail["status"],
                "vin": detail["vin"],
                "year": vehicle.get("year") or "",
                "make": vehicle.get("make") or "",
                "model": vehicle.get("model") or "",
                "trim": vehicle.get("trim") or "",
                "price": detail["price"],
                "exterior_colour": detail["exterior_colour"],
                "interior_colour": detail["interior_colour"],
                "dealership_nickname": dealership.get("nickname") or "",
                "photo_count": detail["photo_count"],
                "has_sticker": (
                    "true" if outputs.get("sticker_pdf") else "false"
                ),
                "has_buyers_guide": (
                    "true"
                    if outputs.get("buyers_guide_pdf")
                    else "false"
                ),
                "buyers_guide_version": (
                    outputs.get("buyers_guide_version") or ""
                ),
            }
        )
    return output.getvalue()
