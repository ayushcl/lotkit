import html
import json
import logging
import os
import shutil
import sqlite3
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory, mkstemp
from typing import Any

from dotenv import load_dotenv
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.requests import ClientDisconnect

from api.auth import current_owner_id
from api.auth_routes import AuthNoStoreMiddleware, router as auth_router
from api.buyers_guide import render_buyers_guide
from api.config import (
    ConfigurationError,
    Settings,
    ensure_persistent_directories,
    get_settings,
)
from api.db import connect_db, init_db
from api.decode import VinDecodeError, decode_vin
from api.delivery import (
    DELIVERY_SESSION_COOKIE,
    DeliveryRunNotFoundError,
    RunHasNoArtifactsError,
    RunNotDeliveredError,
    RunNotReadyError,
    begin_public_artifact_download_by_session,
    create_delivery_link,
    exchange_delivery_secret,
    get_owner_delivery_status,
    get_usable_delivery_link_by_session,
    install_delivery_secret_log_redaction,
    manifest_artifact_types,
    public_bootstrap_response,
    public_security_headers,
    public_unavailable_response,
    reopen_delivered_run,
    resolve_manifest_artifact,
    revoke_owner_delivery_link,
    validate_public_id_format,
)
from api.dealerships import (
    InvalidLogoError,
    create_profile,
    delete_profile,
    get_profile,
    get_profile_row,
    list_profiles,
    logo_media_type,
    resolve_logo_path,
    update_profile,
    validate_logo,
)
from api.photos import build_run
from api.runs import (
    UNSET,
    DealershipNotFoundError,
    InvalidRunDataError,
    RunConflictError,
    RunDeliveredError,
    RunNotFoundError,
    RunStatusConflictError,
    RunTarget,
    discard_run,
    export_runs_csv,
    get_run_detail,
    list_runs,
    mark_run_ready,
    prepare_run_target,
    record_successful_output,
    resolve_artifact_path,
    safe_run_directory,
    snapshot_dealership,
)
from api.sticker import build_sticker_pdf
from api.vin import is_valid_vin, normalize_vin

STATIC_DIR = Path(__file__).resolve().parent / "static"
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BASE_DIR / ".env")

RUNS_ROOT = get_settings().runs_dir
LOGGER = logging.getLogger("uvicorn.error")
REQUIRED_TABLES = frozenset(
    {
        "users",
        "dealership_profiles",
        "runs",
        "delivery_links",
        "delivery_sessions",
        "user_sessions",
        "storage_cleanup_jobs",
    }
)
router = APIRouter()


class VinRequest(BaseModel):
    vin: str


class DeliveryExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    secret: str = Field(
        min_length=43,
        max_length=43,
        pattern=r"^[A-Za-z0-9_-]{43}$",
    )


class BuyersGuideRequest(BaseModel):
    vin: str = ""
    make: str = ""
    model: str = ""
    year: str = ""
    version: str | None = None
    run_id: str | None = None
    dealership_id: int | None = None
    vehicle: dict[str, Any] | None = None
    price: str | None = None
    exterior_colour: str | None = None
    interior_colour: str | None = None


@router.get("/health")
def health() -> dict[str, bool | str]:
    return {"ok": True, "service": "lotkit"}


def _verify_readiness(app_instance: FastAPI) -> None:
    if not bool(getattr(app_instance.state, "startup_completed", False)):
        raise RuntimeError("Application startup is incomplete.")

    settings: Settings = app_instance.state.settings
    data_dir = settings.data_dir.resolve()
    required_directories = (
        data_dir,
        settings.runs_dir.resolve(),
        settings.dealership_logos_dir.resolve(),
    )
    if any(not directory.is_dir() for directory in required_directories):
        raise RuntimeError("Required persistent storage is unavailable.")
    if any(
        not directory.is_relative_to(data_dir)
        for directory in required_directories[1:]
    ):
        raise RuntimeError("Persistent storage configuration is unsafe.")
    if (
        not settings.database_path.is_file()
        or not settings.database_path.resolve().is_relative_to(data_dir)
    ):
        raise RuntimeError("The database is unavailable.")

    connection = sqlite3.connect(
        f"{settings.database_path.resolve().as_uri()}?mode=rw",
        uri=True,
    )
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        available_tables = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                """
            )
        }
        if not REQUIRED_TABLES.issubset(available_tables):
            raise RuntimeError("Required database schema is unavailable.")
    finally:
        connection.close()

    descriptor: int | None = None
    probe_path: Path | None = None
    try:
        descriptor, raw_path = mkstemp(
            prefix=".lotkit-readiness-",
            dir=data_dir,
        )
        probe_path = Path(raw_path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if probe_path is not None:
            probe_path.unlink(missing_ok=True)


@router.get("/ready")
def readiness(request: Request) -> JSONResponse:
    try:
        _verify_readiness(request.app)
    except Exception as exc:
        LOGGER.warning(
            "Readiness check failed (%s).",
            type(exc).__name__,
        )
        return JSONResponse(
            status_code=503,
            content={"ok": False, "service": "lotkit", "ready": False},
        )
    return JSONResponse(
        content={"ok": True, "service": "lotkit", "ready": True}
    )


def _parse_optional_dealership_id(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("Dealership profile is invalid or not owned.")
    try:
        dealership_id = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Dealership profile is invalid or not owned."
        ) from exc
    if dealership_id <= 0:
        raise ValueError("Dealership profile is invalid or not owned.")
    return dealership_id


def _normalize_requested_run_id(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not value.strip():
        raise InvalidRunDataError("Run ID must be a UUID4 string.")
    return value.strip()


def _run_text_value(values: dict[str, Any], key: str) -> str | object:
    if key not in values:
        return UNSET
    value = values[key]
    return "" if value is None else str(value).strip()


def _run_error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, RunNotFoundError):
        return JSONResponse(
            status_code=404,
            content={"error": "run_not_found", "detail": str(exc)},
        )
    if isinstance(exc, RunDeliveredError):
        return JSONResponse(
            status_code=409,
            content={"error": "run_delivered"},
        )
    if isinstance(exc, RunStatusConflictError):
        return JSONResponse(
            status_code=409,
            content={"error": "run_status_conflict", "detail": str(exc)},
        )
    if isinstance(exc, RunConflictError):
        return JSONResponse(
            status_code=409,
            content={
                "error": "run_mismatch",
                "detail": (
                    "This Run belongs to a different vehicle or dealership."
                ),
            },
        )
    if isinstance(exc, DealershipNotFoundError) or (
        isinstance(exc, ValueError)
        and not isinstance(exc, InvalidRunDataError)
    ):
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_dealership",
                "detail": "Dealership profile is invalid or not owned.",
            },
        )
    return JSONResponse(
        status_code=422,
        content={"error": "invalid_run", "detail": str(exc)},
    )


def _prepare_output_run(
    owner_id: int,
    vin: str,
    dealership_id: int | None,
    requested_run_id: str | None,
) -> tuple[RunTarget, dict[str, Any]]:
    connection = connect_db()
    try:
        target = prepare_run_target(
            connection,
            owner_id,
            vin,
            dealership_id,
            requested_run_id,
        )
        dealership_snapshot = snapshot_dealership(
            connection,
            owner_id,
            dealership_id,
        )
        return target, dealership_snapshot
    finally:
        connection.close()


def _record_output(
    owner_id: int,
    target: RunTarget,
    *,
    vin: str,
    dealership_id: int | None,
    output_updates: dict[str, str],
    vehicle: dict[str, Any] | None | object = UNSET,
    price: str | None | object = UNSET,
    exterior_colour: str | None | object = UNSET,
    interior_colour: str | None | object = UNSET,
    photo_order: list[str] | None | object = UNSET,
    dealership_snapshot: dict[str, Any] | None | object = UNSET,
) -> dict[str, Any]:
    connection = connect_db()
    try:
        return record_successful_output(
            connection,
            owner_id,
            target,
            vin=vin,
            dealership_id=dealership_id,
            output_updates=output_updates,
            vehicle=vehicle,
            price=price,
            exterior_colour=exterior_colour,
            interior_colour=interior_colour,
            photo_order=photo_order,
            dealership_snapshot=dealership_snapshot,
            runs_root=RUNS_ROOT,
        )
    finally:
        connection.close()


def _write_run_file(target: RunTarget, filename: str, content: bytes) -> Path:
    run_directory = safe_run_directory(target.run_id, RUNS_ROOT)
    run_directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_directory.mkdir(
        parents=False,
        exist_ok=not target.is_new,
        mode=0o700,
    )
    destination = (run_directory / filename).resolve()
    if destination.parent != run_directory:
        raise InvalidRunDataError("Unsafe artifact filename.")
    destination.write_bytes(content)
    try:
        destination.chmod(0o600)
    except OSError:
        pass
    return destination


def _artifact_filename(
    target: RunTarget,
    stem: str,
    suffix: str,
) -> str:
    """Give every regeneration an immutable, opaque generation filename."""

    if target.is_new:
        return f"{stem}{suffix}"
    return f"{stem}_{uuid.uuid4().hex}{suffix}"


def _remove_new_run_folder(target: RunTarget) -> None:
    if not target.is_new:
        return
    try:
        directory = safe_run_directory(target.run_id, RUNS_ROOT)
    except InvalidRunDataError:
        return
    if directory.is_dir():
        shutil.rmtree(directory)


def _remove_failed_artifact(
    target: RunTarget,
    artifact_path: Path | None,
) -> None:
    """Remove only the exactly known artifact from a failed DB mutation."""

    if target.is_new:
        _remove_new_run_folder(target)
        return
    if artifact_path is None:
        return
    try:
        run_directory = safe_run_directory(target.run_id, RUNS_ROOT)
        candidate = artifact_path
        file_stat = candidate.lstat()
    except (FileNotFoundError, InvalidRunDataError, OSError):
        return
    if (
        candidate.parent == run_directory
        and candidate.is_file()
        and not candidate.is_symlink()
        and file_stat.st_nlink >= 1
    ):
        candidate.unlink(missing_ok=True)


def _artifact_response(
    owner_id: int,
    run_id: str,
    artifact_type: str,
) -> FileResponse:
    connection = connect_db()
    try:
        artifact = resolve_artifact_path(
            connection,
            owner_id,
            run_id,
            artifact_type,
            RUNS_ROOT,
        )
    finally:
        connection.close()

    if artifact is None:
        raise HTTPException(status_code=404, detail="Run artifact not found.")

    media_types = {
        "photos_zip": "application/zip",
        "sticker_pdf": "application/pdf",
        "buyers_guide_pdf": "application/pdf",
    }
    return FileResponse(
        artifact,
        media_type=media_types[artifact_type],
        filename=artifact.name,
        headers={"X-LotKit-Run-ID": run_id},
        content_disposition_type=(
            "attachment" if artifact_type == "photos_zip" else "inline"
        ),
    )


def _delivery_error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, DeliveryRunNotFoundError):
        return JSONResponse(
            status_code=404,
            content={"error": "run_not_found"},
        )
    if isinstance(exc, RunNotReadyError):
        return JSONResponse(
            status_code=409,
            content={"error": "run_not_ready"},
        )
    if isinstance(exc, RunHasNoArtifactsError):
        return JSONResponse(
            status_code=409,
            content={"error": "run_has_no_artifacts"},
        )
    if isinstance(exc, RunNotDeliveredError):
        return JSONResponse(
            status_code=409,
            content={"error": "run_not_delivered"},
        )
    raise exc


def _public_delivery_page(
    public_id: str,
    link: Any,
    artifact_types: list[str],
) -> HTMLResponse:
    try:
        vehicle = json.loads(link["run_vehicle_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        vehicle = {}
    if not isinstance(vehicle, dict):
        vehicle = {}

    year = html.escape(str(vehicle.get("year") or ""), quote=True)
    make = html.escape(str(vehicle.get("make") or ""), quote=True)
    model = html.escape(str(vehicle.get("model") or ""), quote=True)
    vehicle_name = " ".join(
        value for value in (year, make, model) if value
    ) or "Vehicle"
    vin = str(link["run_vin"] or "")
    vin_suffix = html.escape(vin[-4:], quote=True)
    safe_public_id = html.escape(public_id, quote=True)

    labels = {
        "photos_zip": ("Vehicle photos", "Download vehicle photos"),
        "sticker_pdf": ("Window sticker", "Download window sticker"),
        "buyers_guide_pdf": (
            "Draft Buyers Guide",
            "Download Draft Buyers Guide",
        ),
    }
    download_controls: list[str] = []
    for artifact_type in artifact_types:
        label, button = labels[artifact_type]
        warning = ""
        if artifact_type == "buyers_guide_pdf":
            warning = (
                "<p class=\"warning\">Draft Buyers Guide — the dealership "
                "must complete all applicable warranty and dealer-contact "
                "fields before display.</p>"
            )
        download_controls.append(
            "<section class=\"file\">"
            f"<h2>{label}</h2>"
            f"{warning}"
            f"<form method=\"post\" action=\"/d/{safe_public_id}/artifact/"
            f"{artifact_type}\">"
            f"<button type=\"submit\">{button}</button>"
            "</form></section>"
        )

    page = (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Vehicle files</title>"
        "<script src=\"/delivery-bootstrap.js\" defer></script><style>"
        "body{font-family:system-ui,sans-serif;background:#f6f7f9;"
        "color:#18202a;margin:0;padding:2rem 1rem;line-height:1.5}"
        "main{max-width:42rem;margin:auto;background:white;padding:2rem;"
        "border:1px solid #d8dde5;border-radius:.75rem}"
        "h1{margin-top:0}.vehicle{font-size:1.1rem}.file{padding:1rem 0;"
        "border-top:1px solid #e3e7ec}.file h2{font-size:1rem}"
        "button{font:inherit;font-weight:650;padding:.65rem 1rem;"
        "border:0;border-radius:.4rem;background:#174ea6;color:white;"
        "cursor:pointer}.warning{padding:.9rem;background:#fff4d6;"
        "border-left:4px solid #b06000}.note{color:#4f5965}</style>"
        "</head><body><main><h1>Vehicle files</h1>"
        f"<p class=\"vehicle\"><strong>{vehicle_name}</strong><br>"
        f"VIN ending {vin_suffix}</p>"
        f"{''.join(download_controls)}"
        "<p class=\"note\">This link provides access to files for this "
        "vehicle. Do not forward it unnecessarily.</p>"
        "</main></body></html>"
    )
    return HTMLResponse(
        content=page,
        headers=public_security_headers(html_response=True),
    )


def _dealership_values(
    nickname: str | None,
    dealership_name: str | None,
    address: str | None,
    phone: str | None,
    email: str | None,
    complaints_contact: str | None,
    sticker_footer_text: str | None,
    notes: str | None,
) -> dict[str, str]:
    values = {
        "nickname": nickname or "",
        "dealership_name": dealership_name or "",
        "address": address or "",
        "phone": phone or "",
        "email": email or "",
        "complaints_contact": complaints_contact or "",
        "sticker_footer_text": sticker_footer_text or "",
        "notes": notes or "",
    }
    if not values["nickname"].strip():
        raise HTTPException(status_code=422, detail="Nickname is required.")
    return values


async def _read_dealership_logo(
    logo: UploadFile | None,
) -> tuple[str, bytes] | None:
    if logo is None or not logo.filename:
        return None

    try:
        file_bytes = await logo.read()
    finally:
        await logo.close()

    try:
        extension = validate_logo(logo.filename, file_bytes)
    except InvalidLogoError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return extension, file_bytes


@router.post("/api/dealerships")
async def create_dealership(
    nickname: str | None = Form(None),
    dealership_name: str | None = Form(None),
    address: str | None = Form(None),
    phone: str | None = Form(None),
    email: str | None = Form(None),
    complaints_contact: str | None = Form(None),
    sticker_footer_text: str | None = Form(None),
    notes: str | None = Form(None),
    logo: UploadFile | None = File(None),
    owner_id: int = Depends(current_owner_id),
):
    values = _dealership_values(
        nickname,
        dealership_name,
        address,
        phone,
        email,
        complaints_contact,
        sticker_footer_text,
        notes,
    )
    prepared_logo = await _read_dealership_logo(logo)

    connection = connect_db()
    try:
        return create_profile(connection, owner_id, values, prepared_logo)
    finally:
        connection.close()


@router.get("/api/dealerships")
def get_dealerships(owner_id: int = Depends(current_owner_id)):
    connection = connect_db()
    try:
        return list_profiles(connection, owner_id)
    finally:
        connection.close()


@router.get("/api/dealerships/{profile_id}")
def get_dealership(
    profile_id: int,
    owner_id: int = Depends(current_owner_id),
):
    connection = connect_db()
    try:
        profile = get_profile(connection, owner_id, profile_id)
    finally:
        connection.close()

    if profile is None:
        raise HTTPException(
            status_code=404,
            detail="Dealership profile not found.",
        )
    return profile


@router.put("/api/dealerships/{profile_id}")
async def update_dealership(
    profile_id: int,
    nickname: str | None = Form(None),
    dealership_name: str | None = Form(None),
    address: str | None = Form(None),
    phone: str | None = Form(None),
    email: str | None = Form(None),
    complaints_contact: str | None = Form(None),
    sticker_footer_text: str | None = Form(None),
    notes: str | None = Form(None),
    logo: UploadFile | None = File(None),
    owner_id: int = Depends(current_owner_id),
):
    values = _dealership_values(
        nickname,
        dealership_name,
        address,
        phone,
        email,
        complaints_contact,
        sticker_footer_text,
        notes,
    )
    prepared_logo = await _read_dealership_logo(logo)

    connection = connect_db()
    try:
        profile = update_profile(
            connection,
            owner_id,
            profile_id,
            values,
            prepared_logo,
        )
    finally:
        connection.close()

    if profile is None:
        raise HTTPException(
            status_code=404,
            detail="Dealership profile not found.",
        )
    return profile


@router.delete("/api/dealerships/{profile_id}")
def delete_dealership(
    profile_id: int,
    owner_id: int = Depends(current_owner_id),
):
    connection = connect_db()
    try:
        deleted = delete_profile(connection, owner_id, profile_id)
    finally:
        connection.close()

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Dealership profile not found.",
        )
    return {"ok": True}


@router.get("/api/dealerships/{profile_id}/logo")
def get_dealership_logo(
    profile_id: int,
    owner_id: int = Depends(current_owner_id),
):
    connection = connect_db()
    try:
        profile = get_profile_row(connection, owner_id, profile_id)
    finally:
        connection.close()

    if profile is None:
        raise HTTPException(status_code=404, detail="Dealership logo not found.")

    logo_file = resolve_logo_path(profile["logo_path"])
    if logo_file is None or not logo_file.is_file():
        raise HTTPException(status_code=404, detail="Dealership logo not found.")

    return FileResponse(
        logo_file,
        media_type=logo_media_type(logo_file),
        filename=logo_file.name,
        content_disposition_type="inline",
    )


@router.post("/api/decode")
async def decode_vin_endpoint(
    request: VinRequest,
    owner_id: int = Depends(current_owner_id),
):
    vin = normalize_vin(request.vin)
    if not is_valid_vin(vin):
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_vin",
                "detail": "VIN must be 17 valid characters and pass check-digit validation.",
            },
        )

    try:
        vehicle = await decode_vin(vin)
    except VinDecodeError as exc:
        return JSONResponse(
            status_code=502,
            content={"error": "decode_failed", "detail": str(exc)},
        )

    return {"vin": vin, "vehicle": vehicle}


@router.post("/api/photos/package")
async def package_photos(
    vin: str = Form(...),
    vehicle: str = Form(...),
    order: str = Form(...),
    photos: list[UploadFile] = File(...),
    run_id: str | None = Form(None),
    dealership_id: str | None = Form(None),
    owner_id: int = Depends(current_owner_id),
):
    normalized_vin = normalize_vin(vin)
    if not is_valid_vin(normalized_vin):
        return JSONResponse(status_code=422, content={"error": "invalid_vin"})

    try:
        vehicle_data = json.loads(vehicle)
    except json.JSONDecodeError:
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_vehicle",
                "detail": "Vehicle must be a valid JSON object.",
            },
        )
    if not isinstance(vehicle_data, dict):
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_vehicle",
                "detail": "Vehicle must be a valid JSON object.",
            },
        )

    try:
        requested_order = json.loads(order)
    except json.JSONDecodeError:
        requested_order = None
    if not isinstance(requested_order, list) or not all(
        isinstance(filename, str) for filename in requested_order
    ):
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_order",
                "detail": "Order must be a JSON array of filenames.",
            },
        )

    try:
        parsed_dealership_id = _parse_optional_dealership_id(dealership_id)
        requested_run_id = _normalize_requested_run_id(run_id)
        target, dealership_snapshot = _prepare_output_run(
            owner_id,
            normalized_vin,
            parsed_dealership_id,
            requested_run_id,
        )
    except (
        DealershipNotFoundError,
        InvalidRunDataError,
        RunConflictError,
        RunNotFoundError,
        ValueError,
    ) as exc:
        return _run_error_response(exc)

    uploaded_files = [
        (photo.filename or "unnamed", await photo.read()) for photo in photos
    ]
    remaining = list(uploaded_files)
    ordered_files: list[tuple[str, bytes]] = []
    for requested_filename in requested_order:
        for position, uploaded_file in enumerate(remaining):
            if uploaded_file[0] == requested_filename:
                ordered_files.append(remaining.pop(position))
                break
    ordered_files.extend(remaining)

    new_artifact_path: Path | None = None
    pending_report_bytes: bytes | None = None
    run_directory: Path | None = None
    try:
        safe_run_directory(target.run_id, RUNS_ROOT)
        RUNS_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        original_zip_filename = f"{normalized_vin}_photos.zip"
        zip_filename = _artifact_filename(
            target,
            f"{normalized_vin}_photos",
            ".zip",
        )
        with TemporaryDirectory(
            prefix=f".{target.run_id}-photos-",
            dir=RUNS_ROOT,
        ) as staging_root:
            summary = build_run(
                normalized_vin,
                vehicle_data,
                ordered_files,
                staging_root,
            )
            staged_directory = Path(staging_root) / summary["run_id"]
            run_directory = safe_run_directory(target.run_id, RUNS_ROOT)
            if target.is_new:
                if run_directory.exists():
                    raise FileExistsError("Run storage already exists.")
                staged_directory.replace(run_directory)
                new_artifact_path = run_directory / zip_filename
            else:
                run_directory.mkdir(
                    parents=False,
                    exist_ok=True,
                    mode=0o700,
                )
                staged_zip = staged_directory / original_zip_filename
                new_artifact_path = run_directory / zip_filename
                staged_zip.replace(new_artifact_path)
                report_name = Path(summary["report_path"]).name
                pending_report_bytes = (
                    staged_directory / report_name
                ).read_bytes()

        _record_output(
            owner_id,
            target,
            vin=normalized_vin,
            dealership_id=parsed_dealership_id,
            output_updates={"photos_zip": zip_filename},
            vehicle=vehicle_data,
            price=_run_text_value(vehicle_data, "price"),
            exterior_colour=_run_text_value(
                vehicle_data,
                "exterior_colour",
            ),
            interior_colour=_run_text_value(
                vehicle_data,
                "interior_colour",
            ),
            photo_order=summary["filenames"],
            dealership_snapshot=(
                dealership_snapshot
                if parsed_dealership_id is not None
                else UNSET
            ),
        )
        if (
            not target.is_new
            and pending_report_bytes is not None
            and run_directory is not None
        ):
            temporary_report = (
                run_directory / f".run_report-{uuid.uuid4().hex}.tmp"
            )
            try:
                temporary_report.write_bytes(pending_report_bytes)
                try:
                    temporary_report.chmod(0o600)
                except OSError:
                    pass
                temporary_report.replace(
                    run_directory / "run_report.json"
                )
            except OSError as exc:
                try:
                    temporary_report.unlink(missing_ok=True)
                except OSError:
                    pass
                LOGGER.warning(
                    "Run report refresh failed after committed packaging "
                    "(%s).",
                    type(exc).__name__,
                )
    except (
        InvalidRunDataError,
        RunConflictError,
        RunNotFoundError,
        RunStatusConflictError,
    ) as exc:
        _remove_failed_artifact(target, new_artifact_path)
        return _run_error_response(exc)
    except Exception:
        _remove_failed_artifact(target, new_artifact_path)
        raise

    return JSONResponse(
        content={
            **summary,
            "run_id": target.run_id,
            "zip_path": f"{target.run_id}/{zip_filename}",
            "report_path": f"{target.run_id}/run_report.json",
            "download_url": f"/api/photos/download/{target.run_id}",
        },
        headers={"X-LotKit-Run-ID": target.run_id},
    )


@router.get("/api/photos/download/{run_id}")
def download_photos(
    run_id: str,
    owner_id: int = Depends(current_owner_id),
):
    return _artifact_response(owner_id, run_id, "photos_zip")


def _parse_sticker_object(value, field_name: str, required: bool = False):
    if value is None or value == "":
        if required:
            raise ValueError(f"{field_name} is required.")
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name} must be a valid JSON object.") from exc
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"{field_name} must be a valid JSON object.")


@router.post("/api/sticker")
async def create_sticker(
    request: Request,
    owner_id: int = Depends(current_owner_id),
):
    content_type = request.headers.get("content-type", "").lower()
    logo_bytes = None

    if content_type.startswith("application/json"):
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            payload = None
        if not isinstance(payload, dict):
            return JSONResponse(
                status_code=422,
                content={
                    "error": "invalid_request",
                    "detail": "Request body must be a JSON object.",
                },
            )
    elif content_type.startswith(
        ("multipart/form-data", "application/x-www-form-urlencoded")
    ):
        form = await request.form()
        payload = {
            "vin": form.get("vin"),
            "vehicle": form.get("vehicle"),
            "dealer": form.get("dealer"),
            "price": form.get("price"),
            "extras": form.get("extras"),
            "dealership_id": form.get("dealership_id"),
            "run_id": form.get("run_id"),
        }
        logo = form.get("logo")
        if logo is not None and hasattr(logo, "read"):
            logo_bytes = await logo.read()
    else:
        return JSONResponse(
            status_code=422,
            content={
                "error": "invalid_request",
                "detail": "Use JSON or multipart form data.",
            },
        )

    vin_value = payload.get("vin")
    normalized_vin = normalize_vin(vin_value) if isinstance(vin_value, str) else ""
    if not is_valid_vin(normalized_vin):
        return JSONResponse(status_code=422, content={"error": "invalid_vin"})

    try:
        vehicle_data = _parse_sticker_object(
            payload.get("vehicle"),
            "Vehicle",
            required=True,
        )
        dealer_data = _parse_sticker_object(payload.get("dealer"), "Dealer")
        extras_data = _parse_sticker_object(payload.get("extras"), "Extras")
    except ValueError as exc:
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_request", "detail": str(exc)},
        )

    try:
        dealership_id = _parse_optional_dealership_id(
            payload.get("dealership_id")
        )
        requested_run_id = _normalize_requested_run_id(payload.get("run_id"))
        target, dealership_snapshot = _prepare_output_run(
            owner_id,
            normalized_vin,
            dealership_id,
            requested_run_id,
        )
    except (
        DealershipNotFoundError,
        InvalidRunDataError,
        RunConflictError,
        RunNotFoundError,
        ValueError,
    ) as exc:
        return _run_error_response(exc)

    inline_logo_supplied = logo_bytes is not None
    if dealership_id is not None:
        connection = connect_db()
        try:
            profile = get_profile_row(connection, owner_id, dealership_id)
        finally:
            connection.close()
        if profile is None:
            return JSONResponse(
                status_code=422,
                content={
                    "error": "invalid_dealership",
                    "detail": "Dealership profile is invalid or not owned.",
                },
            )

        profile_dealer = {
            "name": profile["dealership_name"] or "",
            "address": profile["address"] or "",
            "phone": profile["phone"] or "",
            "footer_text": profile["sticker_footer_text"] or "",
        }
        dealer_data = {**profile_dealer, **dealer_data}

        if logo_bytes is None:
            profile_logo = resolve_logo_path(profile["logo_path"])
            if profile_logo is not None and profile_logo.is_file():
                logo_bytes = profile_logo.read_bytes()

    effective_snapshot: dict[str, Any] | object = UNSET
    has_inline_branding = any(
        str(dealer_data.get(field) or "").strip()
        for field in ("name", "address", "phone", "footer_text")
    )
    if dealership_id is not None or has_inline_branding:
        effective_snapshot = {
            **dealership_snapshot,
            "dealership_name": str(dealer_data.get("name") or "").strip(),
            "address": str(dealer_data.get("address") or "").strip(),
            "phone": str(dealer_data.get("phone") or "").strip(),
            "sticker_footer_text": str(
                dealer_data.get("footer_text") or ""
            ).strip(),
        }
        if inline_logo_supplied:
            effective_snapshot["logo_path"] = None

    if logo_bytes:
        dealer_data = {**dealer_data, "logo_bytes": logo_bytes}
    price_value = payload.get("price")
    price = str(price_value).strip() if price_value is not None else None

    pdf_bytes = build_sticker_pdf(
        normalized_vin,
        vehicle_data,
        dealer_data,
        price,
        extras_data,
    )
    sticker_filename = _artifact_filename(
        target,
        f"{normalized_vin}_sticker",
        ".pdf",
    )
    new_artifact_path: Path | None = None
    try:
        new_artifact_path = _write_run_file(
            target,
            sticker_filename,
            pdf_bytes,
        )
        _record_output(
            owner_id,
            target,
            vin=normalized_vin,
            dealership_id=dealership_id,
            output_updates={"sticker_pdf": sticker_filename},
            vehicle=vehicle_data,
            price=_run_text_value(payload, "price"),
            exterior_colour=_run_text_value(
                extras_data,
                "exterior_colour",
            ),
            interior_colour=_run_text_value(
                extras_data,
                "interior_colour",
            ),
            dealership_snapshot=effective_snapshot,
        )
    except (
        InvalidRunDataError,
        RunConflictError,
        RunNotFoundError,
        RunStatusConflictError,
    ) as exc:
        _remove_failed_artifact(target, new_artifact_path)
        return _run_error_response(exc)
    except Exception:
        _remove_failed_artifact(target, new_artifact_path)
        raise

    return JSONResponse(
        content={
            "run_id": target.run_id,
            "download_url": f"/api/sticker/download/{target.run_id}",
        },
        headers={"X-LotKit-Run-ID": target.run_id},
    )


@router.get("/api/sticker/download/{run_id}")
def download_sticker(
    run_id: str,
    owner_id: int = Depends(current_owner_id),
):
    return _artifact_response(owner_id, run_id, "sticker_pdf")


@router.post("/api/buyers-guide")
def create_buyers_guide(
    request: BuyersGuideRequest,
    owner_id: int = Depends(current_owner_id),
):
    normalized_vin = normalize_vin(request.vin)
    if not is_valid_vin(normalized_vin):
        return JSONResponse(status_code=422, content={"error": "invalid_vin"})

    if request.version not in {"as_is", "implied_only"}:
        return JSONResponse(
            status_code=422,
            content={"error": "version_required"},
        )

    try:
        dealership_id = _parse_optional_dealership_id(
            request.dealership_id
        )
        requested_run_id = _normalize_requested_run_id(request.run_id)
        target, dealership_snapshot = _prepare_output_run(
            owner_id,
            normalized_vin,
            dealership_id,
            requested_run_id,
        )
    except (
        DealershipNotFoundError,
        InvalidRunDataError,
        RunConflictError,
        RunNotFoundError,
        ValueError,
    ) as exc:
        return _run_error_response(exc)

    pdf_bytes = render_buyers_guide(
        normalized_vin,
        request.make,
        request.model,
        request.year,
        request.version,
    )

    buyers_guide_filename = _artifact_filename(
        target,
        f"DRAFT_buyers_guide_{normalized_vin}_{request.version}",
        ".pdf",
    )
    vehicle_snapshot = {
        **(request.vehicle or {}),
        "year": request.year,
        "make": request.make,
        "model": request.model,
    }
    new_artifact_path: Path | None = None
    try:
        new_artifact_path = _write_run_file(
            target,
            buyers_guide_filename,
            pdf_bytes,
        )
        _record_output(
            owner_id,
            target,
            vin=normalized_vin,
            dealership_id=dealership_id,
            output_updates={
                "buyers_guide_pdf": buyers_guide_filename,
                "buyers_guide_version": request.version,
            },
            vehicle=vehicle_snapshot,
            price=request.price if request.price is not None else UNSET,
            exterior_colour=(
                request.exterior_colour
                if request.exterior_colour is not None
                else UNSET
            ),
            interior_colour=(
                request.interior_colour
                if request.interior_colour is not None
                else UNSET
            ),
            dealership_snapshot=(
                dealership_snapshot if dealership_id is not None else UNSET
            ),
        )
    except (
        InvalidRunDataError,
        RunConflictError,
        RunNotFoundError,
        RunStatusConflictError,
    ) as exc:
        _remove_failed_artifact(target, new_artifact_path)
        return _run_error_response(exc)
    except Exception:
        _remove_failed_artifact(target, new_artifact_path)
        raise

    return JSONResponse(
        content={
            "run_id": target.run_id,
            "download_url": f"/api/buyers-guide/download/{target.run_id}",
        },
        headers={"X-LotKit-Run-ID": target.run_id},
    )


@router.get("/api/buyers-guide/download/{run_id}")
def download_buyers_guide(
    run_id: str,
    owner_id: int = Depends(current_owner_id),
):
    return _artifact_response(owner_id, run_id, "buyers_guide_pdf")


@router.get("/api/runs/export.csv")
def export_run_history(
    vin: str | None = Query(None),
    dealership_id: int | None = Query(None),
    status: str | None = Query(None),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    owner_id: int = Depends(current_owner_id),
):
    connection = connect_db()
    try:
        try:
            csv_content = export_runs_csv(
                connection,
                owner_id,
                vin=vin,
                dealership_id=dealership_id,
                status=status,
                date_from=date_from,
                date_to=date_to,
            )
        except InvalidRunDataError as exc:
            return _run_error_response(exc)
    finally:
        connection.close()

    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                'attachment; filename="lotkit_run_history.csv"'
            )
        },
    )


@router.get("/api/runs")
def get_run_history(
    vin: str | None = Query(None),
    dealership_id: int | None = Query(None),
    status: str | None = Query(None),
    date_from: str | None = Query(None, alias="from"),
    date_to: str | None = Query(None, alias="to"),
    owner_id: int = Depends(current_owner_id),
):
    connection = connect_db()
    try:
        try:
            return list_runs(
                connection,
                owner_id,
                vin=vin,
                dealership_id=dealership_id,
                status=status,
                date_from=date_from,
                date_to=date_to,
            )
        except InvalidRunDataError as exc:
            return _run_error_response(exc)
    finally:
        connection.close()


@router.get("/api/runs/{run_id}")
def get_saved_run(
    run_id: str,
    owner_id: int = Depends(current_owner_id),
):
    connection = connect_db()
    try:
        run = get_run_detail(connection, owner_id, run_id)
    finally:
        connection.close()
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return run


@router.post("/api/runs/{run_id}/delivery", status_code=201)
def create_run_delivery_link(
    run_id: str,
    request: Request,
    response: Response,
    owner_id: int = Depends(current_owner_id),
):
    connection = connect_db()
    try:
        try:
            delivery = create_delivery_link(
                connection,
                owner_id,
                run_id,
                RUNS_ROOT,
            )
            public_base_url = (
                request.app.state.settings.public_base_url.rstrip("/")
            )
            response.headers["Cache-Control"] = "no-store, private"
            response.headers["Pragma"] = "no-cache"
            return {
                "share_url": (
                    f"{public_base_url}/d/{delivery.public_id}"
                    f"#{delivery.delivery_secret}"
                ),
                "expires_utc": delivery.expires_utc,
                "state": "active",
                "token_hint": delivery.token_hint,
            }
        except (
            DeliveryRunNotFoundError,
            RunHasNoArtifactsError,
            RunNotReadyError,
        ) as exc:
            return _delivery_error_response(exc)
    finally:
        connection.close()


@router.get("/api/runs/{run_id}/delivery")
def get_run_delivery_link_status(
    run_id: str,
    owner_id: int = Depends(current_owner_id),
):
    connection = connect_db()
    try:
        try:
            return get_owner_delivery_status(
                connection,
                owner_id,
                run_id,
            )
        except DeliveryRunNotFoundError as exc:
            return _delivery_error_response(exc)
    finally:
        connection.close()


@router.post("/api/runs/{run_id}/delivery/revoke")
def revoke_run_delivery_link(
    run_id: str,
    owner_id: int = Depends(current_owner_id),
):
    connection = connect_db()
    try:
        try:
            return revoke_owner_delivery_link(
                connection,
                owner_id,
                run_id,
                runs_root=RUNS_ROOT,
            )
        except DeliveryRunNotFoundError as exc:
            return _delivery_error_response(exc)
    finally:
        connection.close()


@router.post("/api/runs/{run_id}/reopen")
def reopen_saved_run(
    run_id: str,
    owner_id: int = Depends(current_owner_id),
):
    connection = connect_db()
    try:
        try:
            return reopen_delivered_run(
                connection,
                owner_id,
                run_id,
                runs_root=RUNS_ROOT,
            )
        except (
            DeliveryRunNotFoundError,
            RunNotDeliveredError,
        ) as exc:
            return _delivery_error_response(exc)
    finally:
        connection.close()


@router.post("/api/runs/{run_id}/ready")
def ready_saved_run(
    run_id: str,
    owner_id: int = Depends(current_owner_id),
):
    connection = connect_db()
    try:
        try:
            return mark_run_ready(connection, owner_id, run_id)
        except (
            RunNotFoundError,
            RunStatusConflictError,
        ) as exc:
            return _run_error_response(exc)
    finally:
        connection.close()


@router.post("/api/runs/{run_id}/discard")
def discard_saved_run(
    run_id: str,
    owner_id: int = Depends(current_owner_id),
):
    connection = connect_db()
    try:
        try:
            discard_run(
                connection,
                owner_id,
                run_id,
                RUNS_ROOT,
            )
        except (
            InvalidRunDataError,
            RunNotFoundError,
            RunStatusConflictError,
        ) as exc:
            return _run_error_response(exc)
    finally:
        connection.close()
    return {"ok": True}


@router.get("/api/runs/{run_id}/artifacts/{artifact_type}")
def download_run_artifact(
    run_id: str,
    artifact_type: str,
    owner_id: int = Depends(current_owner_id),
):
    if artifact_type not in {
        "photos_zip",
        "sticker_pdf",
        "buyers_guide_pdf",
    }:
        raise HTTPException(status_code=404, detail="Run artifact not found.")
    return _artifact_response(owner_id, run_id, artifact_type)


@router.get("/delivery-bootstrap.js", include_in_schema=False)
def delivery_bootstrap_script():
    return FileResponse(
        STATIC_DIR / "delivery-bootstrap.js",
        media_type="application/javascript",
        headers=public_security_headers(),
    )


async def _delivery_exchange_payload(
    request: Request,
    *,
    maximum_bytes: int = 512,
) -> DeliveryExchangeRequest | None:
    """Read and validate a deliberately small JSON body without disclosure."""

    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        return None

    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            return None
        if declared_length < 0 or declared_length > maximum_bytes:
            return None

    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > maximum_bytes:
                return None
            body.extend(chunk)
        return DeliveryExchangeRequest.model_validate_json(bytes(body))
    except (
        ClientDisconnect,
        ValidationError,
        ValueError,
        UnicodeError,
        RuntimeError,
    ):
        return None


def _delivery_cookie_path(public_id: str) -> str:
    return f"/d/{public_id}"


def _delete_delivery_session_cookie(
    response: Response,
    request: Request,
    public_id: str,
) -> None:
    if not validate_public_id_format(public_id):
        return
    response.delete_cookie(
        DELIVERY_SESSION_COOKIE,
        path=_delivery_cookie_path(public_id),
        secure=request.app.state.settings.environment == "production",
        httponly=True,
        samesite="strict",
    )


@router.post("/d/{public_id}/exchange", include_in_schema=False)
async def public_delivery_exchange(public_id: str, request: Request):
    payload = await _delivery_exchange_payload(request)
    if payload is None:
        return public_unavailable_response()

    connection = connect_db()
    try:
        session = exchange_delivery_secret(
            connection,
            public_id,
            payload.secret,
            RUNS_ROOT,
        )
    finally:
        connection.close()

    if session is None:
        return public_unavailable_response()

    response = Response(
        status_code=204,
        headers=public_security_headers(),
    )
    response.set_cookie(
        key=DELIVERY_SESSION_COOKIE,
        value=session.credential,
        max_age=session.max_age,
        path=_delivery_cookie_path(public_id),
        secure=request.app.state.settings.environment == "production",
        httponly=True,
        samesite="strict",
    )
    return response


@router.get("/d/{public_id}", include_in_schema=False)
def public_delivery_page(public_id: str, request: Request):
    session_credential = request.cookies.get(DELIVERY_SESSION_COOKIE)
    if not session_credential:
        return public_bootstrap_response()

    connection = connect_db()
    try:
        opened_link = get_usable_delivery_link_by_session(
            connection,
            public_id,
            session_credential,
            RUNS_ROOT,
            mark_opened=True,
        )
        if opened_link is None:
            response = public_bootstrap_response()
            _delete_delivery_session_cookie(response, request, public_id)
            return response

        available_types = [
            artifact_type
            for artifact_type in manifest_artifact_types(opened_link)
            if resolve_manifest_artifact(
                opened_link,
                artifact_type,
                RUNS_ROOT,
            )
            is not None
        ]
        if not available_types:
            response = public_bootstrap_response()
            _delete_delivery_session_cookie(response, request, public_id)
            return response
        return _public_delivery_page(
            public_id,
            opened_link,
            available_types,
        )
    finally:
        connection.close()


@router.get(
    "/d/{public_id}/artifact/{artifact_type}",
    include_in_schema=False,
)
def reject_public_get_download(public_id: str, artifact_type: str):
    return public_unavailable_response()


@router.post(
    "/d/{public_id}/artifact/{artifact_type}",
    include_in_schema=False,
)
def public_artifact_download(
    public_id: str,
    artifact_type: str,
    request: Request,
):
    session_credential = request.cookies.get(DELIVERY_SESSION_COOKIE)
    if not session_credential:
        return public_unavailable_response()

    connection = connect_db()
    try:
        artifact = begin_public_artifact_download_by_session(
            connection,
            public_id,
            session_credential,
            artifact_type,
            RUNS_ROOT,
        )
    finally:
        connection.close()

    if artifact is None:
        return public_unavailable_response()
    return FileResponse(
        artifact.path,
        media_type=artifact.content_type,
        filename=artifact.download_name,
        content_disposition_type="attachment",
        headers=public_security_headers(),
    )


@router.api_route(
    "/d/{unmatched_path:path}",
    methods=["GET", "POST"],
    include_in_schema=False,
)
def unavailable_public_delivery_path(unmatched_path: str):
    return public_unavailable_response()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application with validated runtime configuration."""

    global RUNS_ROOT
    canonical_settings = get_settings()
    if settings is not None and settings != canonical_settings:
        raise ConfigurationError(
            "FastAPI must use the process-wide cached Settings object."
        )
    configured_at_build = canonical_settings
    # Compatibility alias for existing integrations; its value is always
    # sourced from the canonical Settings object.
    RUNS_ROOT = configured_at_build.runs_dir

    @asynccontextmanager
    async def lifespan(app_instance: FastAPI):
        runtime_settings = get_settings()
        app_instance.state.settings = runtime_settings
        app_instance.state.startup_completed = False
        previous_umask = os.umask(0o077)
        try:
            LOGGER.info(
                "LotKit startup configuration: environment=%s data_dir=%s "
                "database_path=%s public_base_host=%s docs_enabled=%s",
                runtime_settings.environment,
                runtime_settings.data_dir,
                runtime_settings.database_path,
                runtime_settings.public_base_host,
                runtime_settings.docs_enabled,
            )
            install_delivery_secret_log_redaction()
            ensure_persistent_directories(runtime_settings)
            init_db(runtime_settings.database_path)

            app_instance.state.startup_completed = True
            yield
        finally:
            app_instance.state.startup_completed = False
            os.umask(previous_umask)

    docs_url = "/docs" if configured_at_build.docs_enabled else None
    redoc_url = "/redoc" if configured_at_build.docs_enabled else None
    openapi_url = (
        "/openapi.json" if configured_at_build.docs_enabled else None
    )
    application = FastAPI(
        debug=False,
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
        lifespan=lifespan,
    )
    application.state.settings = configured_at_build
    application.state.startup_completed = False
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(configured_at_build.trusted_hosts),
    )
    application.add_middleware(AuthNoStoreMiddleware)
    application.include_router(auth_router)
    application.include_router(router)
    application.mount(
        "/",
        StaticFiles(directory=STATIC_DIR, html=True),
        name="static",
    )
    return application


app = create_app()
