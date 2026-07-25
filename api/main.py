from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

load_dotenv(BASE_DIR / ".env")

app = FastAPI()


@app.get("/health")
def health() -> dict[str, bool | str]:
    return {"ok": True, "service": "lotkit"}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
