import json
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import api.main
from api.main import app
from api.sticker import build_sticker_pdf, clean_value, format_engine

VIN = "1HGCM82633A004352"


def logo_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (160, 60), color="#f4b400").save(output, format="PNG")
    return output.getvalue()


@pytest.mark.parametrize(
    ("vehicle", "expected"),
    [
        (
            {
                "engine": "2.998832712L",
                "EngineCylinders": "6",
                "FuelTypePrimary": "Gasoline",
            },
            "3.0L 6-cyl Gasoline",
        ),
        ({"engine": "2.0L"}, "2.0L"),
        (
            {"EngineCylinders": "4", "FuelTypePrimary": "Gasoline"},
            "4-cyl Gasoline",
        ),
        ({}, ""),
    ],
)
def test_format_engine(vehicle: dict, expected: str) -> None:
    assert format_engine(vehicle) == expected


def test_clean_value() -> None:
    assert clean_value("  Accord  ") == "Accord"
    assert clean_value("") == "—"
    assert clean_value(None) == "—"


def test_build_sticker_pdf_with_full_data() -> None:
    pdf = build_sticker_pdf(
        VIN,
        {
            "year": "2021",
            "make": "HONDA",
            "model": "Pilot",
            "trim": "Touring",
            "engine": "3.498L, 6 cylinders, Gasoline",
            "transmission": "Automatic",
            "drive": "All-Wheel Drive",
            "body": "Sport Utility Vehicle",
            "doors": "4",
        },
        {
            "name": "Northstar Motors",
            "address": "100 Market Street, Springfield",
            "phone": "(555) 010-2040",
            "footer_text": "Vehicle subject to prior sale. Ask us for full details.",
            "logo_bytes": logo_bytes(),
        },
        "18995",
        {
            "stock_number": "NS-2041",
            "mileage": "42500",
            "exterior_colour": "Blue",
            "interior_colour": "Black",
            "features": [
                "Heated front seats",
                "Navigation",
                "Blind spot monitoring",
                "Power tailgate",
            ],
        },
    )

    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 2_000


def test_build_sticker_pdf_with_minimal_data() -> None:
    pdf = build_sticker_pdf(
        VIN,
        {"year": "2003", "make": "HONDA", "model": "Accord"},
        {},
        None,
        None,
    )

    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 1_000


def test_sticker_endpoint_json_and_download(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(api.main, "RUNS_ROOT", tmp_path)
    client = TestClient(app)

    response = client.post(
        "/api/sticker",
        json={
            "vin": VIN,
            "vehicle": {
                "year": "2003",
                "make": "HONDA",
                "model": "Accord",
                "engine": "3.0L, 6 cylinders, Gasoline",
            },
            "price": "7995",
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["run_id"].startswith(f"{VIN}_")
    assert result["download_url"] == f"/api/sticker/download/{result['run_id']}"

    download = client.get(result["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/pdf"
    assert download.content.startswith(b"%PDF")


def test_sticker_endpoint_accepts_multipart_logo(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(api.main, "RUNS_ROOT", tmp_path)
    client = TestClient(app)

    response = client.post(
        "/api/sticker",
        data={
            "vin": VIN,
            "vehicle": json.dumps(
                {"year": "2003", "make": "HONDA", "model": "Accord"}
            ),
            "dealer": json.dumps({"name": "Northstar Motors"}),
            "extras": json.dumps({"stock_number": "A-101"}),
        },
        files={"logo": ("logo.png", logo_bytes(), "image/png")},
    )

    assert response.status_code == 200
    assert response.json()["download_url"].startswith("/api/sticker/download/")


def test_sticker_endpoint_rejects_invalid_vin(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(api.main, "RUNS_ROOT", tmp_path)
    client = TestClient(app)

    response = client.post(
        "/api/sticker",
        json={"vin": "not-a-vin", "vehicle": {}},
    )

    assert response.status_code == 422
    assert response.json() == {"error": "invalid_vin"}
    assert list(tmp_path.iterdir()) == []


def test_sticker_download_rejects_invalid_run_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(api.main, "RUNS_ROOT", tmp_path)
    client = TestClient(app)

    response = client.get("/api/sticker/download/not-a-run")

    assert response.status_code == 404
