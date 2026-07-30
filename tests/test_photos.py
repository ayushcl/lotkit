import json
from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import api.main
import api.photos
from api.db import connect_db
from api.main import app
from api.photos import (
    PhotoPackagingError,
    build_run,
    safe_ext,
    sequenced_name,
    verify_photo_zip,
)

VIN = "1HGCM82633A004352"


def image_bytes(image_format: str, color: str) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (2, 2), color=color).save(buffer, format=image_format)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("photo.jpg", ".jpg"),
        ("photo.JPEG", ".jpg"),
        ("photo.png", ".png"),
        ("photo.WEBP", ".webp"),
    ],
)
def test_safe_ext(filename: str, expected: str) -> None:
    assert safe_ext(filename) == expected


def test_safe_ext_rejects_unsupported_file() -> None:
    with pytest.raises(ValueError):
        safe_ext("photo.gif")


@pytest.mark.parametrize(
    ("index", "expected_suffix"),
    [
        (1, "_01.jpg"),
        (10, "_10.jpg"),
        (100, "_100.jpg"),
    ],
)
def test_sequenced_name(index: int, expected_suffix: str) -> None:
    assert sequenced_name(VIN, index, ".jpg") == f"{VIN}{expected_suffix}"


def test_build_run_creates_images_zip_and_report(tmp_path) -> None:
    original_files = [
        ("front.JPG", image_bytes("JPEG", "red")),
        ("rear.png", image_bytes("PNG", "blue")),
        ("interior.jpeg", image_bytes("JPEG", "green")),
        ("broken.jpg", b"this is not an image"),
    ]
    vehicle = {"year": "2003", "make": "HONDA", "model": "Accord"}

    summary = build_run(VIN, vehicle, original_files, str(tmp_path))

    expected_filenames = [
        f"{VIN}_01.jpg",
        f"{VIN}_02.png",
        f"{VIN}_03.jpg",
    ]
    run_directory = tmp_path / summary["run_id"]
    assert run_directory.is_dir()
    assert summary["photo_count"] == 3
    assert summary["filenames"] == expected_filenames
    assert summary["skipped"] == ["broken.jpg"]

    for filename in expected_filenames:
        assert not (run_directory / filename).exists()

    zip_file = tmp_path / summary["zip_path"]
    assert zip_file.is_file()
    with ZipFile(zip_file) as archive:
        assert archive.namelist() == expected_filenames
        assert archive.read(expected_filenames[0]) == original_files[0][1]
        assert archive.read(expected_filenames[1]) == original_files[1][1]
        assert archive.read(expected_filenames[2]) == original_files[2][1]

    report_file = tmp_path / summary["report_path"]
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["vin"] == VIN
    assert report["vehicle"] == vehicle
    assert report["photo_count"] == 3
    assert report["filenames"] == expected_filenames
    assert report["skipped"] == ["broken.jpg"]
    assert report["zip_filename"] == f"{VIN}_photos.zip"
    assert sorted(path.name for path in run_directory.iterdir()) == [
        f"{VIN}_photos.zip",
        "run_report.json",
    ]


def test_photo_package_endpoint_and_download(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(api.main, "RUNS_ROOT", tmp_path)
    client = TestClient(app)
    vehicle = {"year": "2003", "make": "HONDA", "model": "Accord"}
    files = [
        ("photos", ("rear.png", image_bytes("PNG", "blue"), "image/png")),
        ("photos", ("front.jpg", image_bytes("JPEG", "red"), "image/jpeg")),
        ("photos", ("side.jpg", image_bytes("JPEG", "green"), "image/jpeg")),
    ]

    response = client.post(
        "/api/photos/package",
        data={
            "vin": VIN,
            "vehicle": json.dumps(vehicle),
            "order": json.dumps(["front.jpg", "rear.png"]),
        },
        files=files,
    )

    assert response.status_code == 200
    result = response.json()
    assert result["photo_count"] == 3
    assert result["filenames"] == [
        f"{VIN}_01.jpg",
        f"{VIN}_02.png",
        f"{VIN}_03.jpg",
    ]

    download = client.get(result["download_url"])
    assert download.status_code == 200
    assert download.headers["content-type"] == "application/zip"
    with ZipFile(BytesIO(download.content)) as archive:
        assert archive.namelist() == result["filenames"]


def test_photo_package_endpoint_rejects_invalid_vin(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(api.main, "RUNS_ROOT", tmp_path)
    client = TestClient(app)

    response = client.post(
        "/api/photos/package",
        data={
            "vin": "not-a-vin",
            "vehicle": "{}",
            "order": json.dumps(["photo.jpg"]),
        },
        files=[
            ("photos", ("photo.jpg", image_bytes("JPEG", "red"), "image/jpeg"))
        ],
    )

    assert response.status_code == 422
    assert response.json() == {"error": "invalid_vin"}
    assert list(tmp_path.iterdir()) == []


def test_zip_verification_rejects_unsafe_or_unexpected_members(
    tmp_path,
) -> None:
    archive_path = tmp_path / "unsafe.zip"
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("../outside.jpg", b"photo")

    with pytest.raises(PhotoPackagingError, match="expected filenames"):
        verify_photo_zip(archive_path, ["safe.jpg"])


def test_corrupt_packaging_never_updates_run_or_leaves_permanent_files(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(api.main, "RUNS_ROOT", tmp_path)

    def reject_archive(*_args, **_kwargs):
        raise PhotoPackagingError("simulated corrupt archive")

    monkeypatch.setattr(api.photos, "verify_photo_zip", reject_archive)
    connection = connect_db()
    try:
        before = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    finally:
        connection.close()

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/photos/package",
            data={
                "vin": VIN,
                "vehicle": "{}",
                "order": json.dumps(["photo.jpg"]),
            },
            files=[
                (
                    "photos",
                    (
                        "photo.jpg",
                        image_bytes("JPEG", "red"),
                        "image/jpeg",
                    ),
                )
            ],
        )
    assert response.status_code == 500
    connection = connect_db()
    try:
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == before
    finally:
        connection.close()
    assert list(tmp_path.iterdir()) == []


def test_photo_download_rejects_invalid_run_id(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(api.main, "RUNS_ROOT", tmp_path)
    client = TestClient(app)

    response = client.get("/api/photos/download/not-a-run")

    assert response.status_code == 404
