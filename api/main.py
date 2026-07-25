import json
import re
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.decode import VinDecodeError, decode_vin
from api.photos import build_run
from api.vin import is_valid_vin, normalize_vin

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
RUNS_ROOT = BASE_DIR / "runs"
RUN_ID_PATTERN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}_\d{8}T\d{6}Z$")

load_dotenv(BASE_DIR / ".env")

app = FastAPI()


class VinRequest(BaseModel):
    vin: str


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


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
