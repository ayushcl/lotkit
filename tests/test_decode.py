import asyncio
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from api.decode import VinDecodeError, decode_vin
from api.main import app

VIN = "1HGCM82633A004352"
VEHICLE = {
    "year": "2003",
    "make": "HONDA",
    "model": "Accord",
    "trim": "EX-V6",
    "body": "Sedan/Saloon",
    "engine": "3.0L, 6 cylinders, Gasoline",
    "transmission": "Automatic",
    "drive": "Front-Wheel Drive",
    "doors": "4",
}

client = TestClient(app)


def test_invalid_vin_returns_422() -> None:
    response = client.post("/api/decode", json={"vin": "not-a-vin"})

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_vin"


def test_valid_vin_returns_mocked_vehicle() -> None:
    mocked_decode = AsyncMock(return_value=VEHICLE)

    with patch("api.main.decode_vin", new=mocked_decode):
        response = client.post("/api/decode", json={"vin": f"  {VIN.lower()}  "})

    assert response.status_code == 200
    assert response.json() == {"vin": VIN, "vehicle": VEHICLE}
    mocked_decode.assert_awaited_once_with(VIN)


def test_decode_failure_returns_502() -> None:
    mocked_decode = AsyncMock(side_effect=VinDecodeError("vPIC is unavailable"))

    with patch("api.main.decode_vin", new=mocked_decode):
        response = client.post("/api/decode", json={"vin": VIN})

    assert response.status_code == 502
    assert response.json() == {
        "error": "decode_failed",
        "detail": "vPIC is unavailable",
    }


def test_decode_vin_extracts_clean_vehicle_data() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/DecodeVinValues/{VIN}")
        assert request.url.params["format"] == "json"
        return httpx.Response(
            200,
            json={
                "Results": [
                    {
                        "ModelYear": "2003",
                        "Make": "HONDA",
                        "Model": "Accord",
                        "Trim": "EX-V6",
                        "BodyClass": "Sedan/Saloon",
                        "DisplacementL": "3.0",
                        "EngineCylinders": "6",
                        "FuelTypePrimary": "Gasoline",
                        "TransmissionStyle": "Automatic",
                        "DriveType": "Front-Wheel Drive",
                        "Doors": "4",
                    }
                ]
            },
        )

    async def run_decode() -> dict[str, str]:
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("api.decode.httpx.AsyncClient", return_value=mock_client):
            return await decode_vin(VIN)

    assert asyncio.run(run_decode()) == VEHICLE


def test_decode_vin_turns_missing_fields_into_empty_strings() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Results": [{}]})

    async def run_decode() -> dict[str, str]:
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch("api.decode.httpx.AsyncClient", return_value=mock_client):
            return await decode_vin(VIN)

    assert asyncio.run(run_decode()) == {key: "" for key in VEHICLE}
