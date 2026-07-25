from typing import Any

import httpx

VPIC_DECODE_URL = "https://vpic.nhtsa.dot.gov/api/vehicles/DecodeVinValues/{vin}"


class VinDecodeError(RuntimeError):
    """Raised when a VIN cannot be decoded through the vPIC service."""


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _engine_description(result: dict[str, Any]) -> str:
    displacement = _text(result.get("DisplacementL"))
    cylinders = _text(result.get("EngineCylinders"))
    fuel = _text(result.get("FuelTypePrimary"))

    parts = []
    if displacement:
        parts.append(f"{displacement}L")
    if cylinders:
        label = "cylinder" if cylinders == "1" else "cylinders"
        parts.append(f"{cylinders} {label}")
    if fuel:
        parts.append(fuel)
    return ", ".join(parts)


async def decode_vin(vin: str) -> dict[str, str]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                VPIC_DECODE_URL.format(vin=vin),
                params={"format": "json"},
            )
            response.raise_for_status()
    except httpx.TimeoutException as exc:
        raise VinDecodeError("The NHTSA vPIC request timed out.") from exc
    except httpx.HTTPError as exc:
        raise VinDecodeError("The NHTSA vPIC service could not be reached.") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise VinDecodeError("The NHTSA vPIC service returned invalid data.") from exc

    results = payload.get("Results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise VinDecodeError("The NHTSA vPIC service returned an unexpected response.")

    result = results[0]
    return {
        "year": _text(result.get("ModelYear")),
        "make": _text(result.get("Make")),
        "model": _text(result.get("Model")),
        "trim": _text(result.get("Trim")),
        "body": _text(result.get("BodyClass")),
        "engine": _engine_description(result),
        "transmission": _text(result.get("TransmissionStyle")),
        "drive": _text(result.get("DriveType")),
        "doors": _text(result.get("Doors")),
    }
