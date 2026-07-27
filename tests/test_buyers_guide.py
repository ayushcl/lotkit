import uuid
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter

import api.main
from api.buyers_guide import FORM_PATH, render_buyers_guide
from api.main import app

VIN = "1HGCM82633A004352"
DRAFT_WARNING = (
    "Draft — dealer completion required. LotKit populates vehicle information "
    "only. The dealership must complete all applicable warranty and "
    "dealer-contact fields before display."
)
PRINT_GUIDANCE = (
    "Print double-sided at actual size (100%) in black ink on white paper. "
    "Both sides must remain readily visible."
)
VEHICLE_VALUES = {
    "VehicleMake[0]": "HONDA",
    "Model[0]": "Accord",
    "Year[0]": "2003",
    "VIN[0]": VIN,
}


def _assert_static_two_page(pdf_bytes: bytes) -> PdfReader:
    assert pdf_bytes.startswith(b"%PDF")
    reader = PdfReader(BytesIO(pdf_bytes))
    assert len(reader.pages) == 2
    assert reader.get_fields() in (None, {})
    assert reader.trailer["/Root"].get("/AcroForm") is None
    assert all(
        annotation.get_object().get("/Subtype") != "/Widget"
        for page in reader.pages
        for annotation in page.get("/Annots", [])
    )
    return reader


def test_render_buyers_guide_rejects_bad_version() -> None:
    with pytest.raises(ValueError):
        render_buyers_guide(VIN, "HONDA", "Accord", "2003", "dealer_choice")


@pytest.mark.parametrize(
    ("version", "source_front_index"),
    [("as_is", 0), ("implied_only", 1)],
)
def test_render_buyers_guide_returns_filled_front_and_official_back(
    version: str,
    source_front_index: int,
) -> None:
    pdf_bytes = render_buyers_guide(
        VIN,
        "HONDA",
        "Accord",
        "2003",
        version,
    )

    reader = _assert_static_two_page(pdf_bytes)
    source = PdfReader(FORM_PATH)
    front_text = reader.pages[0].extract_text() or ""
    source_front_text = source.pages[source_front_index].extract_text() or ""
    back_text = reader.pages[1].extract_text() or ""

    assert front_text.startswith(source_front_text)
    assert front_text[len(source_front_text) :].strip().splitlines() == list(
        VEHICLE_VALUES.values()
    )
    assert all(front_text.count(value) == 1 for value in VEHICLE_VALUES.values())
    assert all(value not in back_text for value in VEHICLE_VALUES.values())

    value_positions: dict[str, list[tuple[float, float]]] = {}

    def capture_vehicle_text(text, current_matrix, *_args) -> None:
        value = text.strip()
        if value in VEHICLE_VALUES.values():
            value_positions.setdefault(value, []).append(
                (current_matrix[4], current_matrix[5])
            )

    reader.pages[0].extract_text(visitor_text=capture_vehicle_text)
    for annotation_reference in source.pages[source_front_index].get(
        "/Annots",
        [],
    ):
        annotation = annotation_reference.get_object()
        widget_name = str(annotation.get("/T", ""))
        if widget_name not in VEHICLE_VALUES:
            continue
        rectangle = annotation["/Rect"]
        expected_position = (float(rectangle[0]), float(rectangle[1]))
        assert expected_position in value_positions[VEHICLE_VALUES[widget_name]]

    assert back_text == (source.pages[2].extract_text() or "")
    assert (
        reader.pages[1].get_contents().get_data()
        == source.pages[2].get_contents().get_data()
    )
    for box_name in (
        "mediabox",
        "cropbox",
        "trimbox",
        "bleedbox",
        "artbox",
    ):
        assert getattr(reader.pages[1], box_name) == getattr(
            source.pages[2],
            box_name,
        )
    assert reader.pages[1].rotation == source.pages[2].rotation


@pytest.mark.parametrize(
    ("version", "field_prefix"),
    [
        ("as_is", "topmostSubform[0].BG-AsIs[0]"),
        ("implied_only", "topmostSubform[0].BG-Implied[0]"),
    ],
)
def test_render_buyers_guide_fills_only_four_vehicle_fields(
    version: str,
    field_prefix: str,
    monkeypatch,
) -> None:
    calls: list[dict[str, str]] = []
    original_update = PdfWriter.update_page_form_field_values

    def capture_update(self, page, fields, *args, **kwargs):
        calls.append(dict(fields))
        return original_update(self, page, fields, *args, **kwargs)

    monkeypatch.setattr(
        PdfWriter,
        "update_page_form_field_values",
        capture_update,
    )

    render_buyers_guide(VIN, "HONDA", "Accord", "2003", version)

    assert calls == [
        {
            f"{field_prefix}.VehicleMake[0]": "HONDA",
            f"{field_prefix}.Model[0]": "Accord",
            f"{field_prefix}.Year[0]": "2003",
            f"{field_prefix}.VIN[0]": VIN,
        }
    ]


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
    assert uuid.UUID(result["run_id"]).version == 4
    assert response.headers["X-LotKit-Run-ID"] == result["run_id"]
    assert (
        result["download_url"]
        == f"/api/buyers-guide/download/{result['run_id']}"
    )
    expected_file = (
        tmp_path
        / result["run_id"]
        / f"DRAFT_buyers_guide_{VIN}_as_is.pdf"
    )
    assert expected_file.is_file()

    download = client.get(result["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/pdf"
    assert expected_file.name in download.headers["content-disposition"]
    _assert_static_two_page(download.content)


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


def test_buyers_guide_ui_has_required_draft_notices() -> None:
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert response.text.count(DRAFT_WARNING) == 2
    assert PRINT_GUIDANCE in response.text
