import json
import sqlite3
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import api.main
from api.auth import current_owner_id, get_or_create_default_owner
from api.db import connect_db, init_db
from api.main import app

VIN = "1HGCM82633A004352"


def image_bytes(image_format: str, color: str = "navy") -> bytes:
    output = BytesIO()
    Image.new("RGB", (24, 12), color=color).save(output, format=image_format)
    return output.getvalue()


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def create_dealership(
    client: TestClient,
    *,
    nickname: str = "Downtown client",
    logo: tuple[str, bytes, str] | None = None,
) -> dict:
    files = None
    if logo is not None:
        filename, file_bytes, media_type = logo
        files = {"logo": (filename, file_bytes, media_type)}

    response = client.post(
        "/api/dealerships",
        data={
            "nickname": nickname,
            "dealership_name": "Northstar Motors",
            "address": "100 Market Street",
            "phone": "(555) 010-2040",
            "email": "sales@northstar.example",
            "complaints_contact": "Sales Manager",
            "sticker_footer_text": "Ask us for full details.",
            "notes": "Preferred photo client.",
        },
        files=files,
    )
    assert response.status_code == 200
    return response.json()


def test_init_db_creates_schema_owner_and_enforces_foreign_keys(
    isolated_persistence,
) -> None:
    database_path = isolated_persistence["database_path"]
    init_db()
    init_db()

    connection = connect_db()
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        tables = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                """
            )
        }
        assert {"users", "dealership_profiles"} <= tables
        assert isinstance(
            connection.execute("SELECT 1 AS value").fetchone(),
            sqlite3.Row,
        )

        first_owner = get_or_create_default_owner(connection)
        second_owner = get_or_create_default_owner(connection)
        assert first_owner == second_owner
        assert connection.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO dealership_profiles (owner_id, nickname)
                VALUES (?, ?)
                """,
                (999_999, "Invalid owner"),
            )
        connection.rollback()
    finally:
        connection.close()

    assert database_path.is_file()


def test_dealership_crud_and_timestamps(client: TestClient) -> None:
    created = create_dealership(client)
    profile_id = created["id"]
    assert created["nickname"] == "Downtown client"
    assert created["dealership_name"] == "Northstar Motors"
    assert created["complaints_contact"] == "Sales Manager"
    assert created["has_logo"] is False

    list_response = client.get("/api/dealerships")
    assert list_response.status_code == 200
    assert list_response.json() == [
        {
            "id": profile_id,
            "nickname": "Downtown client",
            "dealership_name": "Northstar Motors",
            "has_logo": False,
        }
    ]

    get_response = client.get(f"/api/dealerships/{profile_id}")
    assert get_response.status_code == 200
    assert get_response.json() == created

    update_response = client.put(
        f"/api/dealerships/{profile_id}",
        data={
            "nickname": "Airport client",
            "dealership_name": "Northstar Airport",
            "address": "200 Runway Road",
            "phone": "(555) 010-9999",
            "email": "airport@northstar.example",
            "complaints_contact": "General Manager",
            "sticker_footer_text": "Airport location inventory.",
            "notes": "Updated notes.",
        },
    )
    assert update_response.status_code == 200
    updated = update_response.json()
    assert updated["nickname"] == "Airport client"
    assert updated["address"] == "200 Runway Road"
    assert updated["created_utc"] == created["created_utc"]
    assert updated["updated_utc"] != created["updated_utc"]

    delete_response = client.delete(f"/api/dealerships/{profile_id}")
    assert delete_response.status_code == 200
    assert delete_response.json() == {"ok": True}
    assert client.get(f"/api/dealerships/{profile_id}").status_code == 404
    assert client.get("/api/dealerships").json() == []


@pytest.mark.parametrize("nickname", [None, "", "   "])
def test_dealership_requires_nonempty_nickname(
    client: TestClient,
    nickname: str | None,
) -> None:
    data = {"dealership_name": "No Nickname Motors"}
    if nickname is not None:
        data["nickname"] = nickname

    response = client.post("/api/dealerships", data=data)

    assert response.status_code == 422
    assert response.json()["detail"] == "Nickname is required."
    assert client.get("/api/dealerships").json() == []


def test_logo_upload_replace_serve_and_delete(
    client: TestClient,
    isolated_persistence,
) -> None:
    png_bytes = image_bytes("PNG", "blue")
    profile = create_dealership(
        client,
        logo=("logo.png", png_bytes, "image/png"),
    )
    profile_id = profile["id"]
    assert profile["logo_path"] == f"storage/logos/profile_{profile_id}.png"
    assert profile["has_logo"] is True
    png_file = (
        isolated_persistence["database_path"].parent / profile["logo_path"]
    )
    assert png_file.read_bytes() == png_bytes

    logo_response = client.get(f"/api/dealerships/{profile_id}/logo")
    assert logo_response.status_code == 200
    assert logo_response.headers["content-type"] == "image/png"
    assert logo_response.content == png_bytes

    jpeg_bytes = image_bytes("JPEG", "orange")
    update_response = client.put(
        f"/api/dealerships/{profile_id}",
        data={
            "nickname": "Downtown client",
            "dealership_name": "Northstar Motors",
        },
        files={"logo": ("replacement.jpg", jpeg_bytes, "image/jpeg")},
    )
    assert update_response.status_code == 200
    updated = update_response.json()
    assert updated["logo_path"] == f"storage/logos/profile_{profile_id}.jpg"
    assert not png_file.exists()
    jpeg_file = (
        isolated_persistence["database_path"].parent / updated["logo_path"]
    )
    assert jpeg_file.read_bytes() == jpeg_bytes
    assert client.get(f"/api/dealerships/{profile_id}/logo").content == jpeg_bytes

    assert client.delete(f"/api/dealerships/{profile_id}").status_code == 200
    assert not jpeg_file.exists()


def test_non_image_logo_is_rejected_without_residue(
    client: TestClient,
    isolated_persistence,
) -> None:
    response = client.post(
        "/api/dealerships",
        data={"nickname": "Invalid logo"},
        files={"logo": ("logo.png", b"not an image", "image/png")},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Logo must be a valid image file."
    assert client.get("/api/dealerships").json() == []
    logo_root = isolated_persistence["logo_root"]
    assert not logo_root.exists() or list(logo_root.iterdir()) == []


def test_profile_queries_are_scoped_to_current_owner(client: TestClient) -> None:
    owned = create_dealership(client, nickname="Owner profile")

    connection = connect_db()
    try:
        cursor = connection.execute(
            """
            INSERT INTO users (email, display_name, created_utc)
            VALUES (?, ?, ?)
            """,
            ("second@example.com", "Second owner", "2026-01-01T00:00:00+00:00"),
        )
        second_owner_id = int(cursor.lastrowid)
        cursor = connection.execute(
            """
            INSERT INTO dealership_profiles (
                owner_id,
                nickname,
                dealership_name,
                created_utc,
                updated_utc
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                second_owner_id,
                "Other owner's profile",
                "Private Motors",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        other_profile_id = int(cursor.lastrowid)
        connection.commit()
    finally:
        connection.close()

    summaries = client.get("/api/dealerships").json()
    assert [profile["id"] for profile in summaries] == [owned["id"]]
    assert client.get(f"/api/dealerships/{other_profile_id}").status_code == 404
    assert (
        client.put(
            f"/api/dealerships/{other_profile_id}",
            data={"nickname": "Attempted update"},
        ).status_code
        == 404
    )
    assert (
        client.get(f"/api/dealerships/{other_profile_id}/logo").status_code
        == 404
    )
    assert client.delete(f"/api/dealerships/{other_profile_id}").status_code == 404

    app.dependency_overrides[current_owner_id] = lambda: second_owner_id
    try:
        second_owner_profiles = client.get("/api/dealerships").json()
        assert [profile["id"] for profile in second_owner_profiles] == [
            other_profile_id
        ]
        created_for_second_owner = client.post(
            "/api/dealerships",
            data={"nickname": "Second owner's new profile"},
        )
        assert created_for_second_owner.status_code == 200
        created_id = created_for_second_owner.json()["id"]
    finally:
        app.dependency_overrides.pop(current_owner_id, None)

    connection = connect_db()
    try:
        owner_id = connection.execute(
            "SELECT owner_id FROM dealership_profiles WHERE id = ?",
            (created_id,),
        ).fetchone()["owner_id"]
    finally:
        connection.close()
    assert owner_id == second_owner_id


def test_logo_path_traversal_is_not_served_or_deleted(
    client: TestClient,
    isolated_persistence,
) -> None:
    profile = create_dealership(client)
    sentinel = isolated_persistence["database_path"].parent / "sentinel.png"
    sentinel.write_bytes(image_bytes("PNG"))

    connection = connect_db()
    try:
        connection.execute(
            "UPDATE dealership_profiles SET logo_path = ? WHERE id = ?",
            ("storage/logos/../../sentinel.png", profile["id"]),
        )
        connection.commit()
    finally:
        connection.close()

    response = client.get(f"/api/dealerships/{profile['id']}/logo")
    assert response.status_code == 404
    assert client.delete(f"/api/dealerships/{profile['id']}").status_code == 200
    assert sentinel.is_file()


def test_sticker_uses_owned_profile_branding_and_logo(
    client: TestClient,
    monkeypatch,
) -> None:
    stored_logo = image_bytes("PNG", "green")
    profile = create_dealership(
        client,
        logo=("logo.png", stored_logo, "image/png"),
    )
    captured = {}

    def capture_sticker(vin, vehicle, dealer, price, extras):
        captured.update(
            {
                "vin": vin,
                "vehicle": vehicle,
                "dealer": dealer,
                "price": price,
                "extras": extras,
            }
        )
        return b"%PDF-1.4\n%%EOF\n"

    monkeypatch.setattr(api.main, "build_sticker_pdf", capture_sticker)

    response = client.post(
        "/api/sticker",
        json={
            "vin": VIN,
            "vehicle": {"year": "2003", "make": "HONDA", "model": "Accord"},
            "dealership_id": profile["id"],
        },
    )

    assert response.status_code == 200
    assert captured["dealer"] == {
        "name": "Northstar Motors",
        "address": "100 Market Street",
        "phone": "(555) 010-2040",
        "footer_text": "Ask us for full details.",
        "logo_bytes": stored_logo,
    }


def test_sticker_inline_branding_and_logo_override_profile(
    client: TestClient,
    monkeypatch,
) -> None:
    profile = create_dealership(
        client,
        logo=("saved.png", image_bytes("PNG", "green"), "image/png"),
    )
    inline_logo = image_bytes("PNG", "red")
    captured = {}

    def capture_sticker(vin, vehicle, dealer, price, extras):
        captured["dealer"] = dealer
        return b"%PDF-1.4\n%%EOF\n"

    monkeypatch.setattr(api.main, "build_sticker_pdf", capture_sticker)

    response = client.post(
        "/api/sticker",
        data={
            "vin": VIN,
            "vehicle": json.dumps(
                {"year": "2003", "make": "HONDA", "model": "Accord"}
            ),
            "dealer": json.dumps(
                {
                    "name": "Explicit Motors",
                    "phone": "",
                }
            ),
            "dealership_id": str(profile["id"]),
        },
        files={"logo": ("override.png", inline_logo, "image/png")},
    )

    assert response.status_code == 200
    assert captured["dealer"] == {
        "name": "Explicit Motors",
        "address": "100 Market Street",
        "phone": "",
        "footer_text": "Ask us for full details.",
        "logo_bytes": inline_logo,
    }


def test_sticker_rejects_invalid_or_unowned_dealership(
    client: TestClient,
    monkeypatch,
) -> None:
    calls = []

    def capture_sticker(*args):
        calls.append(args)
        return b"%PDF-1.4\n%%EOF\n"

    monkeypatch.setattr(api.main, "build_sticker_pdf", capture_sticker)

    invalid_response = client.post(
        "/api/sticker",
        json={
            "vin": VIN,
            "vehicle": {"year": "2003", "make": "HONDA", "model": "Accord"},
            "dealership_id": 999_999,
        },
    )
    assert invalid_response.status_code == 422
    assert invalid_response.json()["error"] == "invalid_dealership"

    connection = connect_db()
    try:
        second_owner = connection.execute(
            """
            INSERT INTO users (email, display_name, created_utc)
            VALUES (?, ?, ?)
            """,
            ("private@example.com", "Private", "2026-01-01T00:00:00+00:00"),
        )
        private_profile = connection.execute(
            """
            INSERT INTO dealership_profiles (owner_id, nickname)
            VALUES (?, ?)
            """,
            (int(second_owner.lastrowid), "Private profile"),
        )
        connection.commit()
        private_profile_id = int(private_profile.lastrowid)
    finally:
        connection.close()

    unowned_response = client.post(
        "/api/sticker",
        json={
            "vin": VIN,
            "vehicle": {"year": "2003", "make": "HONDA", "model": "Accord"},
            "dealership_id": private_profile_id,
        },
    )
    assert unowned_response.status_code == 422
    assert unowned_response.json()["error"] == "invalid_dealership"
    assert calls == []
