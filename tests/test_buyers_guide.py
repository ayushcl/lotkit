from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

import api.main
from api.buyers_guide import render_buyers_guide
from api.main import app

VIN = "1HGCM82633A004352"


def _assert_static_single_page(pdf_bytes: bytes) -> None:
    assert pdf_bytes.startswith(b"%PDF")
    reader = PdfReader(BytesIO(pdf_bytes))
    assert len(reader.pages) == 1
    assert reader.get_fields() in (None, {})
    assert reader.trailer["/Root"].get("/AcroForm") is None
    assert all(
        annotation.get_object().get("/Subtype") != "/Widget"
        for annotation in reader.pages[0].get("/Annots", [])
    )


def test_render_buyers_guide_rejects_bad_version() -> None:
    with pytest.raises(ValueError):
        render_buyers_guide(VIN, "HONDA", "Accord", "2003", "dealer_choice")


@pytest.mark.parametrize("version", ["as_is", "implied_only"])
def test_render_buyers_guide_returns_flattened_front_page(version: str) -> None:
    pdf_bytes = render_buyers_guide(
        VIN,
        "HONDA",
        "Accord",
        "2003",
        version,
    )

    _assert_static_single_page(pdf_bytes)
    page_text = PdfReader(BytesIO(pdf_bytes)).pages[0].extract_text()
    assert all(value in page_text for value in ("HONDA", "Accord", "2003", VIN))


def test_buyers_guide_endpoint_and_download(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(api.main, "RUNS_ROOT", tmp_path)
    client = TestClient(app)

    response = client.post(
        "/api/buyers-guide",
        json={
            "vin": VIN,
            "make": "HONDA",
            "model": "Accord",
            "year": "2003",
            "version": "as_is",
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["run_id"].startswith(f"{VIN}_")
    assert (
        result["download_url"]
        == f"/api/buyers-guide/download/{result['run_id']}"
    )
    expected_file = (
        tmp_path
        / result["run_id"]
        / f"{VIN}_buyers_guide_as_is.pdf"
    )
    assert expected_file.is_file()

    download = client.get(result["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/pdf"
    _assert_static_single_page(download.content)


def test_buyers_guide_endpoint_rejects_invalid_vin(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(api.main, "RUNS_ROOT", tmp_path)
    client = TestClient(app)

    response = client.post(
        "/api/buyers-guide",
        json={
            "vin": "not-a-vin",
            "make": "HONDA",
            "model": "Accord",
            "year": "2003",
            "version": "as_is",
        },
    )

    assert response.status_code == 422
    assert response.json() == {"error": "invalid_vin"}
    assert list(tmp_path.iterdir()) == []


def test_buyers_guide_endpoint_requires_version(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(api.main, "RUNS_ROOT", tmp_path)
    client = TestClient(app)

    response = client.post(
        "/api/buyers-guide",
        json={
            "vin": VIN,
            "make": "HONDA",
            "model": "Accord",
            "year": "2003",
        },
    )

    assert response.status_code == 422
    assert response.json() == {"error": "version_required"}
    assert list(tmp_path.iterdir()) == []


def test_buyers_guide_endpoint_rejects_invalid_version(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(api.main, "RUNS_ROOT", tmp_path)
    client = TestClient(app)

    response = client.post(
        "/api/buyers-guide",
        json={
            "vin": VIN,
            "make": "HONDA",
            "model": "Accord",
            "year": "2003",
            "version": "unknown",
        },
    )

    assert response.status_code == 422
    assert response.json() == {"error": "version_required"}
    assert list(tmp_path.iterdir()) == []


def test_buyers_guide_download_rejects_invalid_run_id(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(api.main, "RUNS_ROOT", tmp_path)
    client = TestClient(app)

    response = client.get("/api/buyers-guide/download/not-a-run")

    assert response.status_code == 404
