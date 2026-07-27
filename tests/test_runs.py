import csv
import json
import sqlite3
import uuid
from io import StringIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main
from api.auth import get_or_create_default_owner
from api.db import connect_db, init_db
from api.main import app

VIN = "1HGCM82633A004352"
SECOND_VIN = "1FTFW1ET9DFC10312"
VEHICLE = {
    "year": "2003",
    "make": "HONDA",
    "model": "Accord",
    "trim": "EX",
    "engine": "3.0L 6-cyl Gasoline",
    "transmission": "Automatic",
    "body": "Sedan",
    "drive": "FWD",
    "doors": "4",
}
STICKER_BYTES = b"%PDF-1.4\nmock LotKit sticker\n%%EOF\n"
BUYERS_GUIDE_BYTES = b"%PDF-1.4\nmock LotKit Buyers Guide\n%%EOF\n"


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def mocked_generators(monkeypatch):
    monkeypatch.setattr(
        api.main,
        "build_sticker_pdf",
        lambda *_args, **_kwargs: STICKER_BYTES,
    )
    monkeypatch.setattr(
        api.main,
        "render_buyers_guide",
        lambda *_args, **_kwargs: BUYERS_GUIDE_BYTES,
    )

    def fake_build_run(vin, _vehicle, files, output_root):
        legacy_run_id = f"{vin}_20260101T000000Z"
        run_directory = Path(output_root) / legacy_run_id
        run_directory.mkdir(parents=True, exist_ok=True)
        filenames = [
            f"{vin}_{index:02d}{Path(original_name).suffix.lower()}"
            for index, (original_name, _contents) in enumerate(files, start=1)
        ]
        zip_filename = f"{vin}_photos.zip"
        (run_directory / zip_filename).write_bytes(b"PK mock photo archive")
        report_filename = "report.json"
        (run_directory / report_filename).write_text("{}", encoding="utf-8")
        return {
            "run_id": legacy_run_id,
            "photo_count": len(filenames),
            "filenames": filenames,
            "skipped": [],
            "zip_path": f"{legacy_run_id}/{zip_filename}",
            "report_path": f"{legacy_run_id}/{report_filename}",
        }

    monkeypatch.setattr(api.main, "build_run", fake_build_run)


def _owner_id() -> int:
    connection = connect_db()
    try:
        return get_or_create_default_owner(connection)
    finally:
        connection.close()


def _run_count(owner_id: int | None = None) -> int:
    connection = connect_db()
    try:
        if owner_id is None:
            row = connection.execute("SELECT COUNT(*) AS count FROM runs").fetchone()
        else:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM runs WHERE owner_id = ?",
                (owner_id,),
            ).fetchone()
        return int(row["count"])
    finally:
        connection.close()


def _run_row(run_id: str):
    connection = connect_db()
    try:
        return connection.execute(
            "SELECT * FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    finally:
        connection.close()


def _insert_user(email: str = "other-owner@example.com") -> int:
    connection = connect_db()
    try:
        cursor = connection.execute(
            """
            INSERT INTO users (email, display_name, created_utc)
            VALUES (?, ?, ?)
            """,
            (email, "Other owner", "2026-01-01T00:00:00+00:00"),
        )
        connection.commit()
        return int(cursor.lastrowid)
    finally:
        connection.close()


def _insert_run(
    *,
    owner_id: int,
    run_id: str | None = None,
    vin: str = VIN,
    dealership_id: int | None = None,
    dealership_snapshot: dict | None = None,
    vehicle: dict | None = None,
    price: str = "",
    exterior_colour: str = "",
    interior_colour: str = "",
    photo_order: list[str] | None = None,
    outputs: dict | None = None,
    status: str = "in_progress",
    created_utc: str = "2026-01-10T12:00:00+00:00",
    updated_utc: str = "2026-01-10T12:00:00+00:00",
) -> str:
    stored_run_id = run_id or str(uuid.uuid4())
    connection = connect_db()
    try:
        connection.execute(
            """
            INSERT INTO runs (
                run_id,
                owner_id,
                dealership_id,
                dealership_snapshot_json,
                vin,
                vehicle_json,
                price,
                exterior_colour,
                interior_colour,
                photo_order_json,
                outputs_json,
                status,
                created_utc,
                updated_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stored_run_id,
                owner_id,
                dealership_id,
                json.dumps(dealership_snapshot or {}),
                vin,
                json.dumps(vehicle or {}),
                price,
                exterior_colour,
                interior_colour,
                json.dumps(photo_order or []),
                json.dumps(outputs or {}),
                status,
                created_utc,
                updated_utc,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return stored_run_id


def _create_dealership(
    client: TestClient,
    *,
    nickname: str = "Downtown client",
    dealership_name: str = "Northstar Motors",
) -> dict:
    response = client.post(
        "/api/dealerships",
        data={
            "nickname": nickname,
            "dealership_name": dealership_name,
            "address": "100 Market Street",
            "phone": "(555) 010-2040",
            "email": "sales@northstar.example",
            "complaints_contact": "General Manager",
            "sticker_footer_text": "Ask us for full details.",
            "notes": "Preferred client.",
        },
    )
    assert response.status_code == 200
    return response.json()


def _create_sticker_run(
    client: TestClient,
    *,
    vin: str = VIN,
    run_id: str | None = None,
    dealership_id: int | None = None,
):
    payload = {
        "vin": vin,
        "vehicle": {**VEHICLE, "vin": vin},
        "price": "7995",
        "extras": {
            "exterior_colour": "Blue",
            "interior_colour": "Black",
        },
    }
    if run_id is not None:
        payload["run_id"] = run_id
    if dealership_id is not None:
        payload["dealership_id"] = dealership_id
    return client.post("/api/sticker", json=payload)


def _assert_uuid4(value: str) -> None:
    parsed = uuid.UUID(value)
    assert parsed.version == 4
    assert str(parsed) == value
    assert VIN not in value


def test_init_db_creates_runs_schema_index_and_foreign_keys(
    isolated_persistence,
) -> None:
    init_db()
    init_db()
    connection = connect_db()
    try:
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(runs)")
        }
        assert columns == {
            "id",
            "run_id",
            "owner_id",
            "dealership_id",
            "dealership_snapshot_json",
            "vin",
            "vehicle_json",
            "price",
            "exterior_colour",
            "interior_colour",
            "photo_order_json",
            "outputs_json",
            "status",
            "created_utc",
            "updated_utc",
        }
        index_columns = [
            row["name"]
            for row in connection.execute(
                "PRAGMA index_info(runs_owner_updated_utc_idx)"
            )
        ]
        assert index_columns == ["owner_id", "updated_utc"]

        foreign_keys = {
            row["from"]: (row["table"], row["to"], row["on_delete"])
            for row in connection.execute("PRAGMA foreign_key_list(runs)")
        }
        assert foreign_keys["owner_id"] == ("users", "id", "NO ACTION")
        assert foreign_keys["dealership_id"] == (
            "dealership_profiles",
            "id",
            "SET NULL",
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO runs (run_id, owner_id, vin)
                VALUES (?, ?, ?)
                """,
                (str(uuid.uuid4()), 999_999, VIN),
            )
        connection.rollback()
    finally:
        connection.close()

    assert isolated_persistence["database_path"].is_file()


def test_decode_and_idle_session_do_not_create_run(
    client: TestClient,
    monkeypatch,
) -> None:
    async def fake_decode(_vin: str) -> dict:
        return dict(VEHICLE)

    monkeypatch.setattr(api.main, "decode_vin", fake_decode)

    assert _run_count() == 0
    assert client.get("/api/runs").status_code == 200
    assert _run_count() == 0

    response = client.post("/api/decode", json={"vin": VIN})
    assert response.status_code == 200
    assert response.json()["vehicle"] == VEHICLE
    assert _run_count() == 0


def test_first_output_creates_one_uuid4_run_with_header(
    client: TestClient,
    mocked_generators,
) -> None:
    response = _create_sticker_run(client)

    assert response.status_code == 200
    run_id = response.headers["X-LotKit-Run-ID"]
    _assert_uuid4(run_id)
    assert response.json()["run_id"] == run_id
    assert response.json()["download_url"] == f"/api/sticker/download/{run_id}"
    assert _run_count() == 1

    row = _run_row(run_id)
    assert row["vin"] == VIN
    assert row["status"] == "in_progress"
    assert json.loads(row["outputs_json"]) == {
        "sticker_pdf": f"{VIN}_sticker.pdf"
    }
    assert (api.main.RUNS_ROOT / run_id / f"{VIN}_sticker.pdf").read_bytes() == (
        STICKER_BYTES
    )


def test_three_outputs_merge_into_same_run_and_record_photo_order(
    client: TestClient,
    mocked_generators,
) -> None:
    first = _create_sticker_run(client)
    assert first.status_code == 200
    run_id = first.headers["X-LotKit-Run-ID"]

    photos = client.post(
        "/api/photos/package",
        data={
            "vin": VIN,
            "vehicle": json.dumps(VEHICLE),
            "order": json.dumps(["front.jpg", "rear.png"]),
            "run_id": run_id,
        },
        files=[
            ("photos", ("rear.png", b"rear", "image/png")),
            ("photos", ("front.jpg", b"front", "image/jpeg")),
        ],
    )
    assert photos.status_code == 200
    assert photos.headers["X-LotKit-Run-ID"] == run_id
    assert photos.json()["download_url"] == f"/api/photos/download/{run_id}"

    buyers_guide = client.post(
        "/api/buyers-guide",
        json={
            "vin": VIN,
            "make": VEHICLE["make"],
            "model": VEHICLE["model"],
            "year": VEHICLE["year"],
            "version": "as_is",
            "vehicle": VEHICLE,
            "price": "8250",
            "exterior_colour": "Silver",
            "interior_colour": "Gray",
            "run_id": run_id,
        },
    )
    assert buyers_guide.status_code == 200
    assert buyers_guide.headers["X-LotKit-Run-ID"] == run_id
    assert buyers_guide.json()["download_url"] == (
        f"/api/buyers-guide/download/{run_id}"
    )
    assert _run_count() == 1

    row = _run_row(run_id)
    outputs = json.loads(row["outputs_json"])
    assert set(outputs) == {
        "photos_zip",
        "sticker_pdf",
        "buyers_guide_pdf",
        "buyers_guide_version",
    }
    assert outputs["buyers_guide_version"] == "as_is"
    assert json.loads(row["photo_order_json"]) == [
        f"{VIN}_01.jpg",
        f"{VIN}_02.png",
    ]
    assert json.loads(row["vehicle_json"]) == VEHICLE
    assert row["price"] == "8250"
    assert row["exterior_colour"] == "Silver"
    assert row["interior_colour"] == "Gray"


def test_same_vin_without_run_id_creates_a_second_run(
    client: TestClient,
    mocked_generators,
) -> None:
    first = _create_sticker_run(client)
    second = _create_sticker_run(client)

    assert first.status_code == second.status_code == 200
    first_id = first.headers["X-LotKit-Run-ID"]
    second_id = second.headers["X-LotKit-Run-ID"]
    assert first_id != second_id
    _assert_uuid4(first_id)
    _assert_uuid4(second_id)
    assert _run_count() == 2


def test_run_id_rejects_vin_and_dealership_mismatches(
    client: TestClient,
    mocked_generators,
) -> None:
    first_dealership = _create_dealership(client, nickname="First")
    second_dealership = _create_dealership(client, nickname="Second")
    created = _create_sticker_run(
        client,
        dealership_id=first_dealership["id"],
    )
    run_id = created.headers["X-LotKit-Run-ID"]

    wrong_vin = _create_sticker_run(
        client,
        vin=SECOND_VIN,
        run_id=run_id,
        dealership_id=first_dealership["id"],
    )
    assert wrong_vin.status_code == 409

    wrong_dealership = _create_sticker_run(
        client,
        run_id=run_id,
        dealership_id=second_dealership["id"],
    )
    assert wrong_dealership.status_code == 409

    missing_dealership = _create_sticker_run(client, run_id=run_id)
    assert missing_dealership.status_code == 409
    assert _run_count() == 1


def test_run_id_not_owned_by_current_owner_returns_404(
    client: TestClient,
    mocked_generators,
) -> None:
    other_owner_id = _insert_user()
    other_run_id = _insert_run(owner_id=other_owner_id)

    response = _create_sticker_run(client, run_id=other_run_id)

    assert response.status_code == 404
    assert client.get(f"/api/runs/{other_run_id}").status_code == 404
    assert client.post(f"/api/runs/{other_run_id}/ready").status_code == 404
    assert client.post(f"/api/runs/{other_run_id}/discard").status_code == 404
    assert _run_count(other_owner_id) == 1
    assert _run_count(_owner_id()) == 0


def test_dealership_snapshot_survives_profile_edit_and_delete(
    client: TestClient,
    mocked_generators,
) -> None:
    profile = _create_dealership(client)
    created = _create_sticker_run(client, dealership_id=profile["id"])
    assert created.status_code == 200
    run_id = created.headers["X-LotKit-Run-ID"]

    before = json.loads(_run_row(run_id)["dealership_snapshot_json"])
    assert before == {
        "nickname": "Downtown client",
        "dealership_name": "Northstar Motors",
        "address": "100 Market Street",
        "phone": "(555) 010-2040",
        "email": "sales@northstar.example",
        "sticker_footer_text": "Ask us for full details.",
        "logo_path": None,
    }

    updated = client.put(
        f"/api/dealerships/{profile['id']}",
        data={
            "nickname": "Renamed client",
            "dealership_name": "Changed Motors",
            "address": "999 Changed Avenue",
        },
    )
    assert updated.status_code == 200
    assert json.loads(_run_row(run_id)["dealership_snapshot_json"]) == before

    assert client.delete(f"/api/dealerships/{profile['id']}").status_code == 200
    row = _run_row(run_id)
    assert row["dealership_id"] is None
    assert json.loads(row["dealership_snapshot_json"]) == before


def test_failed_artifact_generation_creates_no_run(
    mocked_generators,
    monkeypatch,
) -> None:
    def fail_generation(*_args, **_kwargs):
        raise RuntimeError("simulated generator failure")

    monkeypatch.setattr(api.main, "build_sticker_pdf", fail_generation)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = _create_sticker_run(client)

    assert response.status_code == 500
    assert _run_count() == 0


def test_ready_and_discard_lifecycle(
    client: TestClient,
    mocked_generators,
) -> None:
    draft_id = _create_sticker_run(client).headers["X-LotKit-Run-ID"]
    ready_id = _create_sticker_run(client).headers["X-LotKit-Run-ID"]
    delivered_id = _create_sticker_run(client).headers["X-LotKit-Run-ID"]
    draft_folder = api.main.RUNS_ROOT / draft_id
    ready_folder = api.main.RUNS_ROOT / ready_id
    delivered_folder = api.main.RUNS_ROOT / delivered_id
    assert draft_folder.is_dir()
    assert ready_folder.is_dir()
    assert delivered_folder.is_dir()

    discarded = client.post(f"/api/runs/{draft_id}/discard")
    assert discarded.status_code == 200
    assert _run_row(draft_id) is None
    assert not draft_folder.exists()

    marked_ready = client.post(f"/api/runs/{ready_id}/ready")
    assert marked_ready.status_code == 200
    assert _run_row(ready_id)["status"] == "ready"
    assert client.post(f"/api/runs/{ready_id}/ready").status_code == 200
    refused_ready_discard = client.post(f"/api/runs/{ready_id}/discard")
    assert refused_ready_discard.status_code == 409
    assert _run_row(ready_id)["status"] == "ready"
    assert ready_folder.is_dir()

    connection = connect_db()
    try:
        connection.execute(
            "UPDATE runs SET status = 'delivered' WHERE run_id = ?",
            (delivered_id,),
        )
        connection.commit()
    finally:
        connection.close()
    refused_delivered_discard = client.post(
        f"/api/runs/{delivered_id}/discard"
    )
    assert refused_delivered_discard.status_code == 409
    assert _run_row(delivered_id)["status"] == "delivered"
    assert delivered_folder.is_dir()


def test_secure_artifact_download_allowlist_containment_and_ownership(
    client: TestClient,
    mocked_generators,
    isolated_persistence,
) -> None:
    run_id = _create_sticker_run(client).headers["X-LotKit-Run-ID"]
    run_directory = api.main.RUNS_ROOT / run_id
    photos_filename = f"{VIN}_photos.zip"
    buyers_filename = f"DRAFT_buyers_guide_{VIN}_as_is.pdf"
    (run_directory / photos_filename).write_bytes(b"PK photo archive")
    (run_directory / buyers_filename).write_bytes(BUYERS_GUIDE_BYTES)

    connection = connect_db()
    try:
        outputs = json.loads(
            connection.execute(
                "SELECT outputs_json FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()["outputs_json"]
        )
        outputs.update(
            {
                "photos_zip": photos_filename,
                "buyers_guide_pdf": buyers_filename,
                "buyers_guide_version": "as_is",
            }
        )
        connection.execute(
            "UPDATE runs SET outputs_json = ? WHERE run_id = ?",
            (json.dumps(outputs), run_id),
        )
        connection.commit()
    finally:
        connection.close()

    expected = {
        "photos_zip": b"PK photo archive",
        "sticker_pdf": STICKER_BYTES,
        "buyers_guide_pdf": BUYERS_GUIDE_BYTES,
    }
    for artifact_type, contents in expected.items():
        response = client.get(
            f"/api/runs/{run_id}/artifacts/{artifact_type}"
        )
        assert response.status_code == 200
        assert response.content == contents

    assert client.get(f"/api/runs/{run_id}/artifacts/report_json").status_code == 404
    assert client.get(f"/api/runs/{run_id}/artifacts/%2E%2E").status_code == 404

    sentinel = isolated_persistence["database_path"].parent / "sentinel.pdf"
    sentinel.write_bytes(b"outside approved run directory")
    connection = connect_db()
    try:
        connection.execute(
            "UPDATE runs SET outputs_json = ? WHERE run_id = ?",
            (json.dumps({"sticker_pdf": "../sentinel.pdf"}), run_id),
        )
        connection.commit()
    finally:
        connection.close()
    assert (
        client.get(f"/api/runs/{run_id}/artifacts/sticker_pdf").status_code
        == 404
    )

    other_owner_id = _insert_user("artifact-owner@example.com")
    other_run_id = _insert_run(
        owner_id=other_owner_id,
        outputs={"sticker_pdf": "private.pdf"},
    )
    other_directory = api.main.RUNS_ROOT / other_run_id
    other_directory.mkdir(parents=True)
    (other_directory / "private.pdf").write_bytes(b"private")
    assert (
        client.get(
            f"/api/runs/{other_run_id}/artifacts/sticker_pdf"
        ).status_code
        == 404
    )


def test_run_history_filters_order_and_owner_scope(
    client: TestClient,
) -> None:
    dealership = _create_dealership(client)
    owner_id = _owner_id()
    other_owner_id = _insert_user("history-owner@example.com")

    january_id = _insert_run(
        owner_id=owner_id,
        dealership_id=dealership["id"],
        dealership_snapshot={"nickname": "Downtown client"},
        vehicle=VEHICLE,
        outputs={
            "sticker_pdf": f"{VIN}_sticker.pdf",
            "buyers_guide_pdf": f"DRAFT_buyers_guide_{VIN}_as_is.pdf",
            "buyers_guide_version": "as_is",
        },
        status="ready",
        created_utc="2026-01-10T12:00:00+00:00",
        updated_utc="2026-01-11T12:00:00+00:00",
    )
    february_id = _insert_run(
        owner_id=owner_id,
        vin=SECOND_VIN,
        vehicle={
            "year": "2013",
            "make": "FORD",
            "model": "F-150",
            "trim": "XLT",
        },
        outputs={"photos_zip": f"{SECOND_VIN}_photos.zip"},
        created_utc="2026-02-20T12:00:00+00:00",
        updated_utc="2026-02-21T12:00:00+00:00",
    )
    _insert_run(
        owner_id=other_owner_id,
        vin=VIN,
        vehicle={"make": "PRIVATE"},
        status="ready",
        updated_utc="2026-03-01T12:00:00+00:00",
    )

    response = client.get("/api/runs")
    assert response.status_code == 200
    summaries = response.json()
    assert [summary["run_id"] for summary in summaries] == [
        february_id,
        january_id,
    ]
    january = next(item for item in summaries if item["run_id"] == january_id)
    assert {
        "vin": january["vin"],
        "year": january["year"],
        "make": january["make"],
        "model": january["model"],
        "dealership_nickname": january["dealership_nickname"],
        "status": january["status"],
    } == {
        "vin": VIN,
        "year": "2003",
        "make": "HONDA",
        "model": "Accord",
        "dealership_nickname": "Downtown client",
        "status": "ready",
    }
    assert january["outputs"]["sticker_pdf"] == f"{VIN}_sticker.pdf"
    assert january["created_utc"] == "2026-01-10T12:00:00+00:00"
    assert january["updated_utc"] == "2026-01-11T12:00:00+00:00"

    filters = [
        ({"vin": "cm826"}, [january_id]),
        ({"dealership_id": dealership["id"]}, [january_id]),
        ({"status": "ready"}, [january_id]),
        (
            {"from": "2026-01-01", "to": "2026-01-31"},
            [january_id],
        ),
        ({"from": "2026-02-01"}, [february_id]),
        ({"to": "2026-01-31"}, [january_id]),
    ]
    for params, expected_ids in filters:
        filtered = client.get("/api/runs", params=params)
        assert filtered.status_code == 200
        assert [item["run_id"] for item in filtered.json()] == expected_ids

    detail = client.get(f"/api/runs/{january_id}")
    assert detail.status_code == 200
    assert detail.json()["vehicle"] == VEHICLE
    assert detail.json()["dealership_snapshot"]["nickname"] == "Downtown client"
    assert detail.json()["outputs"]["buyers_guide_version"] == "as_is"


def test_csv_export_header_escaping_filters_and_route_precedence(
    client: TestClient,
) -> None:
    owner_id = _owner_id()
    other_owner_id = _insert_user("csv-owner@example.com")
    exported_id = _insert_run(
        owner_id=owner_id,
        dealership_snapshot={"nickname": "Northstar, Downtown"},
        vehicle={
            "year": "2003",
            "make": "HONDA",
            "model": "Accord, SE",
            "trim": 'EX "Premium"',
        },
        price="7,995",
        exterior_colour="Blue",
        interior_colour="Black",
        photo_order=["one.jpg", "two.jpg"],
        outputs={
            "photos_zip": f"{VIN}_photos.zip",
            "sticker_pdf": f"{VIN}_sticker.pdf",
            "buyers_guide_pdf": f"DRAFT_buyers_guide_{VIN}_as_is.pdf",
            "buyers_guide_version": "as_is",
        },
        status="ready",
    )
    _insert_run(
        owner_id=owner_id,
        vin=SECOND_VIN,
        vehicle={"year": "2013", "make": "FORD", "model": "F-150"},
        status="in_progress",
    )
    _insert_run(owner_id=other_owner_id, status="ready")

    response = client.get("/api/runs/export.csv", params={"status": "ready"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert ".csv" in response.headers["content-disposition"]
    rows = list(csv.reader(StringIO(response.text)))
    assert rows[0] == [
        "run_id",
        "created_utc",
        "updated_utc",
        "status",
        "vin",
        "year",
        "make",
        "model",
        "trim",
        "price",
        "exterior_colour",
        "interior_colour",
        "dealership_nickname",
        "photo_count",
        "has_sticker",
        "has_buyers_guide",
        "buyers_guide_version",
    ]
    assert len(rows) == 2
    exported = dict(zip(rows[0], rows[1], strict=True))
    assert exported == {
        "run_id": exported_id,
        "created_utc": "2026-01-10T12:00:00+00:00",
        "updated_utc": "2026-01-10T12:00:00+00:00",
        "status": "ready",
        "vin": VIN,
        "year": "2003",
        "make": "HONDA",
        "model": "Accord, SE",
        "trim": 'EX "Premium"',
        "price": "7,995",
        "exterior_colour": "Blue",
        "interior_colour": "Black",
        "dealership_nickname": "Northstar, Downtown",
        "photo_count": "2",
        "has_sticker": "true",
        "has_buyers_guide": "true",
        "buyers_guide_version": "as_is",
    }
