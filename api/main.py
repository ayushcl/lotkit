from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from api.decode import VinDecodeError, decode_vin
from api.vin import is_valid_vin, normalize_vin

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

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


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
