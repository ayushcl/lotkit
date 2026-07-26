import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.buyers_guide import render_buyers_guide
from api.decode import VinDecodeError, decode_vin
from api.photos import build_run
from api.sticker import build_sticker_pdf
from api.vin import is_valid_vin, normalize_vin

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
RUNS_ROOT = BASE_DIR / "runs"
RUN_ID_PATTERN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}_\d{8}T\d{6}Z$")

load_dotenv(BASE_DIR / ".env")

app = FastAPI()


class VinRequest(BaseModel):
    vin: str


class BuyersGuideRequest(BaseModel):
    vin: str = ""
    make: str = ""
    model: str = ""
    year: str = ""
    version: str | None = None


@app.get("/health")
def health() -> dict[str, bool | str]:
    return {"ok": True, "service": "lotkit"}


@app.post("/api/decode")
async def decode_vin_endpoint(request: VinRequest):
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


@app.post("/api/photos/package")
async def package_photos(
    vin: str = Form(...),
    vehicle: str = Form(...),
    order: str = Form(...),
    photos: list[UploadFile] = File(...),
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

    summary = build_run(
        normalized_vin,
        vehicle_data,
        ordered_files,
        str(RUNS_ROOT),
    )
    return {
        **summary,
        "download_url": f"/api/photos/download/{summary['run_id']}",
    }


@app.get("/api/photos/download/{run_id}")
def download_photos(run_id: str):
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise HTTPException(status_code=404, detail="Photo package not found.")

    runs_root = RUNS_ROOT.resolve()
    run_directory = (runs_root / run_id).resolve()
    if run_directory.parent != runs_root:
        raise HTTPException(status_code=404, detail="Photo package not found.")

    vin = run_id[:17]
    zip_file = run_directory / f"{vin}_photos.zip"
    if not zip_file.is_file():
        raise HTTPException(status_code=404, detail="Photo package not found.")

    return FileResponse(
        zip_file,
        media_type="application/zip",
        filename=zip_file.name,
    )


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


@app.post("/api/sticker")
async def create_sticker(request: Request):
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
    created_utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{normalized_vin}_{created_utc}"
    run_directory = RUNS_ROOT / run_id
    run_directory.mkdir(parents=True, exist_ok=True)
    sticker_file = run_directory / f"{normalized_vin}_sticker.pdf"
    sticker_file.write_bytes(pdf_bytes)

    return {
        "run_id": run_id,
        "download_url": f"/api/sticker/download/{run_id}",
    }


@app.get("/api/sticker/download/{run_id}")
def download_sticker(run_id: str):
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise HTTPException(status_code=404, detail="Window sticker not found.")

    runs_root = RUNS_ROOT.resolve()
    run_directory = (runs_root / run_id).resolve()
    if run_directory.parent != runs_root:
        raise HTTPException(status_code=404, detail="Window sticker not found.")

    vin = run_id[:17]
    sticker_file = run_directory / f"{vin}_sticker.pdf"
    if not sticker_file.is_file():
        raise HTTPException(status_code=404, detail="Window sticker not found.")

    return FileResponse(
        sticker_file,
        media_type="application/pdf",
        filename=sticker_file.name,
        content_disposition_type="inline",
    )


@app.post("/api/buyers-guide")
def create_buyers_guide(request: BuyersGuideRequest):
    normalized_vin = normalize_vin(request.vin)
    if not is_valid_vin(normalized_vin):
        return JSONResponse(status_code=422, content={"error": "invalid_vin"})

    if request.version not in {"as_is", "implied_only"}:
        return JSONResponse(
            status_code=422,
            content={"error": "version_required"},
        )

    pdf_bytes = render_buyers_guide(
        normalized_vin,
        request.make,
        request.model,
        request.year,
        request.version,
    )

    created_utc = datetime.now(timezone.utc)
    for seconds_to_add in range(60):
        run_id = (
            f"{normalized_vin}_"
            f"{(created_utc + timedelta(seconds=seconds_to_add)):%Y%m%dT%H%M%SZ}"
        )
        run_directory = RUNS_ROOT / run_id
        existing_guides = list(
            run_directory.glob(f"DRAFT_buyers_guide_{normalized_vin}_*.pdf")
        )
        if not existing_guides:
            break
    else:
        raise HTTPException(
            status_code=500,
            detail="Could not allocate a Buyers Guide run.",
        )

    run_directory.mkdir(parents=True, exist_ok=True)
    buyers_guide_file = (
        run_directory
        / f"DRAFT_buyers_guide_{normalized_vin}_{request.version}.pdf"
    )
    buyers_guide_file.write_bytes(pdf_bytes)

    return {
        "run_id": run_id,
        "download_url": f"/api/buyers-guide/download/{run_id}",
    }


@app.get("/api/buyers-guide/download/{run_id}")
def download_buyers_guide(run_id: str):
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise HTTPException(status_code=404, detail="Buyers Guide not found.")

    runs_root = RUNS_ROOT.resolve()
    run_directory = (runs_root / run_id).resolve()
    if run_directory.parent != runs_root:
        raise HTTPException(status_code=404, detail="Buyers Guide not found.")

    vin = run_id[:17]
    buyers_guide_files = [
        run_directory / f"DRAFT_buyers_guide_{vin}_{version}.pdf"
        for version in ("as_is", "implied_only")
    ]
    available_files = [
        buyers_guide_file
        for buyers_guide_file in buyers_guide_files
        if buyers_guide_file.is_file()
    ]
    if len(available_files) != 1:
        raise HTTPException(status_code=404, detail="Buyers Guide not found.")

    buyers_guide_file = available_files[0]
    return FileResponse(
        buyers_guide_file,
        media_type="application/pdf",
        filename=buyers_guide_file.name,
        content_disposition_type="inline",
    )


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
