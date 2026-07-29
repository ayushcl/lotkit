import hashlib
import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

import api.main
from api.auth import current_owner_id, get_or_create_default_owner
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
}
INTERNAL_NICKNAME = "Photographer-only client label"
INTERNAL_PRICE = "7995"
STICKER_BYTES = b"%PDF-1.4\noriginal sticker\n%%EOF\n"
NEW_STICKER_BYTES = b"%PDF-1.4\nregenerated sticker\n%%EOF\n"
BUYERS_GUIDE_BYTES = b"%PDF-1.4\noriginal Buyers Guide\n%%EOF\n"
NEW_BUYERS_GUIDE_BYTES = b"%PDF-1.4\nregenerated Buyers Guide\n%%EOF\n"
PHOTOS_BYTES = b"PK original photo archive"
NEW_PHOTOS_BYTES = b"PK regenerated photo archive"

GENERIC_UNAVAILABLE_COPY = (
    "This delivery link is unavailable. It may have expired or been "
    "withdrawn. Ask the person who sent it to create a new link."
)
COMMON_PUBLIC_HEADERS = {
    "cache-control": "no-store, private",
    "pragma": "no-cache",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "cross-origin-resource-policy": "same-origin",
}
ARTIFACT_CONTENT = {
    "photos_zip": PHOTOS_BYTES,
    "sticker_pdf": STICKER_BYTES,
    "buyers_guide_pdf": BUYERS_GUIDE_BYTES,
}
ARTIFACT_FILENAMES = {
    "photos_zip": f"{VIN}_photos.zip",
    "sticker_pdf": f"{VIN}_sticker.pdf",
    "buyers_guide_pdf": f"DRAFT_buyers_guide_{VIN}_as_is.pdf",
}


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


def _owner_id() -> int:
    connection = connect_db()
    try:
        return get_or_create_default_owner(connection)
    finally:
        connection.close()


def _insert_user(email: str = "other-delivery-owner@example.com") -> int:
    connection = connect_db()
    try:
        cursor = connection.execute(
            """
            INSERT INTO users (email, display_name, created_utc)
            VALUES (?, ?, ?)
            """,
            (email, "Other photographer", "2026-01-01T00:00:00+00:00"),
        )
        connection.commit()
        return int(cursor.lastrowid)
    finally:
        connection.close()


def _insert_run(
    *,
    owner_id: int | None = None,
    run_id: str | None = None,
    vin: str = VIN,
    status: str = "ready",
    vehicle: dict | None = None,
    price: str = INTERNAL_PRICE,
    outputs: dict[str, str] | None = None,
    dealership_snapshot: dict | None = None,
    created_utc: str = "2026-07-01T12:00:00+00:00",
) -> str:
    resolved_owner_id = owner_id if owner_id is not None else _owner_id()
    stored_run_id = run_id or str(uuid.uuid4())
    timestamp = created_utc
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
            VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stored_run_id,
                resolved_owner_id,
                json.dumps(
                    dealership_snapshot
                    or {
                        "nickname": INTERNAL_NICKNAME,
                        "dealership_name": "Northstar Motors",
                        "address": "100 Internal Street",
                        "phone": "(555) 010-2040",
                        "email": "private@northstar.example",
                        "sticker_footer_text": "Internal footer",
                        "logo_path": None,
                    }
                ),
                vin,
                json.dumps(vehicle or VEHICLE),
                price,
                "Blue",
                "Black",
                json.dumps(
                    [f"{vin}_01.jpg", f"{vin}_02.jpg"]
                    if outputs and "photos_zip" in outputs
                    else []
                ),
                json.dumps(outputs or {}),
                status,
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    return stored_run_id


def _make_run_with_artifacts(
    *,
    artifact_types: tuple[str, ...] = ("sticker_pdf",),
    owner_id: int | None = None,
    status: str = "ready",
    vehicle: dict | None = None,
    vin: str = VIN,
) -> tuple[str, dict[str, str]]:
    filenames = {
        artifact_type: (
            ARTIFACT_FILENAMES[artifact_type]
            if vin == VIN
            else ARTIFACT_FILENAMES[artifact_type].replace(VIN, vin)
        )
        for artifact_type in artifact_types
    }
    if "buyers_guide_pdf" in filenames:
        filenames["buyers_guide_version"] = "as_is"
    run_id = _insert_run(
        owner_id=owner_id,
        vin=vin,
        status=status,
        vehicle=vehicle,
        outputs=filenames,
    )
    run_directory = api.main.RUNS_ROOT / run_id
    run_directory.mkdir(parents=True, exist_ok=True)
    for artifact_type in artifact_types:
        (run_directory / filenames[artifact_type]).write_bytes(
            ARTIFACT_CONTENT[artifact_type]
        )
    return run_id, filenames


def _run_row(run_id: str) -> sqlite3.Row:
    connection = connect_db()
    try:
        row = connection.execute(
            "SELECT * FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert row is not None
        return row
    finally:
        connection.close()


def _delivery_rows(run_id: str) -> list[sqlite3.Row]:
    connection = connect_db()
    try:
        return connection.execute(
            """
            SELECT *
            FROM delivery_links
            WHERE run_id = ?
            ORDER BY id
            """,
            (run_id,),
        ).fetchall()
    finally:
        connection.close()


def _delivery_session_rows() -> list[sqlite3.Row]:
    connection = connect_db()
    try:
        return connection.execute(
            """
            SELECT *
            FROM delivery_sessions
            ORDER BY id
            """
        ).fetchall()
    finally:
        connection.close()


def _create_link(client: TestClient, run_id: str):
    return client.post(f"/api/runs/{run_id}/delivery")


def _credentials_from_response(response) -> tuple[str, str]:
    share_url = response.json()["share_url"]
    parsed = urlsplit(share_url)
    assert parsed.scheme in {"http", "https"}
    assert parsed.netloc
    assert parsed.query == ""
    assert parsed.path.startswith("/d/")
    public_id = parsed.path.removeprefix("/d/")
    assert "/" not in public_id
    assert re.fullmatch(r"[A-Za-z0-9_-]{20,64}", public_id)
    assert re.fullmatch(r"[A-Za-z0-9_-]{40,64}", parsed.fragment)
    return public_id, parsed.fragment


def _exchange(
    client: TestClient,
    public_id: str,
    delivery_secret: str,
):
    return client.post(
        f"/d/{public_id}/exchange",
        json={"secret": delivery_secret},
    )


def _manifest_path(entry: dict) -> Path:
    relative_path = Path(entry["relative_path"])
    assert not relative_path.is_absolute()
    candidate = (api.main.RUNS_ROOT / relative_path).resolve()
    assert candidate.is_relative_to(api.main.RUNS_ROOT.resolve())
    return candidate


def _public_header_subset(response) -> dict[str, str]:
    return {
        name: response.headers[name]
        for name in (*COMMON_PUBLIC_HEADERS, "content-security-policy")
    }


def _assert_common_public_headers(response, *, html_response: bool) -> None:
    for name, value in COMMON_PUBLIC_HEADERS.items():
        assert response.headers[name] == value
    if html_response:
        csp = response.headers["content-security-policy"]
        assert csp == (
            "default-src 'none'; script-src 'self'; connect-src 'self'; "
            "style-src 'unsafe-inline'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'"
        )


def _uuid_in_text(value: str) -> bool:
    candidates = re.findall(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-"
        r"[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        value,
        flags=re.IGNORECASE,
    )
    candidates.extend(
        re.findall(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", value, re.I)
    )
    for candidate in candidates:
        if uuid.UUID(candidate).version == 4:
            return True
    return False


def test_init_db_creates_delivery_schema_indexes_and_cascading_foreign_keys(
    isolated_persistence,
) -> None:
    init_db()
    init_db()
    connection = connect_db()
    try:
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(delivery_links)"
            )
        }
        assert columns == {
            "id",
            "owner_id",
            "run_id",
            "public_id",
            "token_hash",
            "token_hint",
            "artifact_manifest_json",
            "created_utc",
            "expires_utc",
            "first_opened_utc",
            "first_download_started_utc",
            "revoked_utc",
            "revocation_reason",
        }
        assert "status" not in columns

        indexes = {
            row["name"]: row
            for row in connection.execute("PRAGMA index_list(delivery_links)")
        }
        assert "delivery_links_token_hash_idx" in indexes
        assert "delivery_links_run_id_idx" in indexes
        assert "delivery_links_owner_id_idx" in indexes
        public_id_index = indexes["delivery_links_public_id_idx"]
        assert public_id_index["unique"] == 1
        assert public_id_index["partial"] == 1
        partial = indexes["ux_delivery_links_one_unrevoked"]
        assert partial["unique"] == 1
        assert partial["partial"] == 1

        foreign_keys = {
            row["from"]: (row["table"], row["to"], row["on_delete"])
            for row in connection.execute(
                "PRAGMA foreign_key_list(delivery_links)"
            )
        }
        assert foreign_keys["owner_id"] == ("users", "id", "NO ACTION")
        assert foreign_keys["run_id"] == ("runs", "run_id", "CASCADE")

        session_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(delivery_sessions)"
            )
        }
        assert session_columns == {
            "id",
            "delivery_link_id",
            "session_hash",
            "created_utc",
            "expires_utc",
        }
        session_indexes = {
            row["name"]
            for row in connection.execute(
                "PRAGMA index_list(delivery_sessions)"
            )
        }
        assert "delivery_sessions_session_hash_idx" in session_indexes
        assert (
            "delivery_sessions_delivery_link_id_idx"
            in session_indexes
        )
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()
    assert isolated_persistence["database_path"].is_file()


def test_create_link_uses_high_entropy_hashed_token_and_returns_raw_once(
    client: TestClient,
    isolated_persistence,
) -> None:
    run_id, _outputs = _make_run_with_artifacts()

    response = _create_link(client, run_id)

    assert response.status_code == 201
    assert set(response.json()) == {
        "share_url",
        "expires_utc",
        "state",
        "token_hint",
    }
    assert response.json()["share_url"].startswith(
        "http://127.0.0.1:8000/d/"
    )
    assert response.headers["cache-control"] == "no-store, private"
    assert response.headers["pragma"] == "no-cache"
    assert response.json()["state"] == "active"
    public_id, delivery_secret = _credentials_from_response(response)
    assert delivery_secret != run_id
    assert delivery_secret != VIN
    assert VIN not in delivery_secret
    assert run_id not in delivery_secret
    assert not re.search(r"\d{8}T\d{6}", delivery_secret)
    assert public_id != run_id
    assert public_id != VIN
    assert VIN not in public_id
    assert run_id not in public_id
    parsed_share_url = urlsplit(response.json()["share_url"])
    assert delivery_secret not in parsed_share_url.path
    assert delivery_secret not in parsed_share_url.query

    rows = _delivery_rows(run_id)
    assert len(rows) == 1
    link = rows[0]
    assert link["public_id"] == public_id
    assert link["token_hash"] == hashlib.sha256(
        delivery_secret.encode()
    ).hexdigest()
    assert link["token_hint"] == delivery_secret[-6:]
    assert delivery_secret not in tuple(str(value) for value in link)
    assert response.json()["token_hint"] == delivery_secret[-6:]
    created = datetime.fromisoformat(link["created_utc"])
    expires = datetime.fromisoformat(link["expires_utc"])
    assert timedelta(days=29, hours=23, minutes=59) < expires - created
    assert expires - created < timedelta(days=30, minutes=1)
    assert _run_row(run_id)["status"] == "ready"

    database_bytes = isolated_persistence["database_path"].read_bytes()
    assert delivery_secret.encode() not in database_bytes
    assert response.json()["share_url"].encode() not in database_bytes

    bootstrap = client.get(f"/d/{public_id}")
    assert bootstrap.status_code == 200
    assert "Opening secure delivery" in bootstrap.text
    assert VIN not in bootstrap.text
    assert delivery_secret not in bootstrap.text

    exchanged = _exchange(client, public_id, delivery_secret)
    assert exchanged.status_code == 204
    public = client.get(f"/d/{public_id}")
    assert public.status_code == 200
    assert "Vehicle files" in public.text
    owner_status = client.get(f"/api/runs/{run_id}/delivery")
    assert owner_status.status_code == 200
    serialized_status = json.dumps(owner_status.json())
    assert delivery_secret not in serialized_status
    assert link["token_hash"] not in serialized_status
    assert "token_hash" not in owner_status.json()
    assert "artifact_manifest" not in serialized_status
    assert "relative_path" not in serialized_status
    assert owner_status.json()["token_hint"] == delivery_secret[-6:]

    owner_page = client.get("/")
    assert "Original share link (shown once)" in owner_page.text
    assert "part after #." in owner_page.text
    assert "copying the cleaned address afterward" in owner_page.text
    assert "if it is lost, create a replacement" in owner_page.text


def test_link_eligibility_no_artifacts_and_owner_none_state(
    client: TestClient,
) -> None:
    in_progress_id, _ = _make_run_with_artifacts(status="in_progress")
    response = _create_link(client, in_progress_id)
    assert response.status_code == 409
    assert response.json()["error"] == "run_not_ready"
    assert _delivery_rows(in_progress_id) == []

    no_artifacts_id = _insert_run(status="ready", outputs={})
    no_artifacts = _create_link(client, no_artifacts_id)
    assert no_artifacts.status_code == 409
    assert no_artifacts.json()["error"] == "run_has_no_artifacts"
    assert _delivery_rows(no_artifacts_id) == []

    missing_id = _insert_run(
        status="ready",
        outputs={"sticker_pdf": "missing.pdf"},
    )
    missing = _create_link(client, missing_id)
    assert missing.status_code == 409
    assert missing.json()["error"] == "run_has_no_artifacts"

    owner_status = client.get(f"/api/runs/{no_artifacts_id}/delivery")
    assert owner_status.status_code == 200
    assert owner_status.json()["state"] == "none"
    assert "token_hash" not in owner_status.json()


def test_replacement_manual_revocation_and_partial_unique_index(
    client: TestClient,
) -> None:
    run_id, _ = _make_run_with_artifacts()
    first = _create_link(client, run_id)
    assert first.status_code == 201
    first_public_id, first_secret = _credentials_from_response(first)
    assert _exchange(client, first_public_id, first_secret).status_code == 204

    second = _create_link(client, run_id)
    assert second.status_code == 201
    second_public_id, second_secret = _credentials_from_response(second)
    assert second_public_id != first_public_id
    assert second_secret != first_secret
    assert _run_row(run_id)["status"] == "ready"

    rows = _delivery_rows(run_id)
    assert len(rows) == 2
    assert rows[0]["revoked_utc"] is not None
    assert rows[0]["revocation_reason"] == "replaced"
    assert rows[1]["revoked_utc"] is None
    assert sum(row["revoked_utc"] is None for row in rows) == 1
    first_unavailable = client.get(f"/d/{first_public_id}")
    assert first_unavailable.status_code == 200
    assert "Opening secure delivery" in first_unavailable.text
    assert VIN not in first_unavailable.text
    old_session_download = client.post(
        f"/d/{first_public_id}/artifact/sticker_pdf"
    )
    assert old_session_download.status_code == 404
    assert GENERIC_UNAVAILABLE_COPY in old_session_download.text
    assert _exchange(
        client,
        second_public_id,
        second_secret,
    ).status_code == 204
    assert "Vehicle files" in client.get(f"/d/{second_public_id}").text

    connection = connect_db()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO delivery_links (
                    owner_id,
                    run_id,
                    public_id,
                    token_hash,
                    token_hint,
                    artifact_manifest_json,
                    created_utc,
                    expires_utc
                )
                VALUES (?, ?, ?, ?, ?, '{}', ?, ?)
                """,
                (
                    _owner_id(),
                    run_id,
                    "C" * 22,
                    "f" * 64,
                    "ffffff",
                    "2026-07-01T00:00:00+00:00",
                    "2099-07-31T00:00:00+00:00",
                ),
            )
        connection.rollback()
    finally:
        connection.close()

    revoked = client.post(f"/api/runs/{run_id}/delivery/revoke")
    assert revoked.status_code == 200
    assert _run_row(run_id)["status"] == "ready"
    latest = _delivery_rows(run_id)[-1]
    assert latest["revoked_utc"] is not None
    assert latest["revocation_reason"] == "manual"
    revoked_page = client.get(f"/d/{second_public_id}")
    assert revoked_page.status_code == 200
    assert VIN not in revoked_page.text
    assert client.post(
        f"/d/{second_public_id}/artifact/sticker_pdf"
    ).status_code == 404
    assert client.post(f"/api/runs/{run_id}/delivery/revoke").status_code == 200
    owner_status = client.get(f"/api/runs/{run_id}/delivery").json()
    assert owner_status["state"] == "revoked"
    assert owner_status["revocation_reason"] == "manual"


def test_expiry_is_derived_from_expires_utc_on_every_request(
    client: TestClient,
) -> None:
    run_id, _ = _make_run_with_artifacts()
    created = _create_link(client, run_id)
    public_id, delivery_secret = _credentials_from_response(created)
    assert _exchange(client, public_id, delivery_secret).status_code == 204
    assert "VIN ending" in client.get(f"/d/{public_id}").text

    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    connection = connect_db()
    try:
        connection.execute(
            """
            UPDATE delivery_links
            SET expires_utc = ?, revoked_utc = NULL
            WHERE run_id = ?
            """,
            (expired_at, run_id),
        )
        connection.commit()
    finally:
        connection.close()

    response = client.get(f"/d/{public_id}")
    assert response.status_code == 200
    assert "Opening secure delivery" in response.text
    assert VIN not in response.text
    unavailable_download = client.post(
        f"/d/{public_id}/artifact/sticker_pdf"
    )
    assert unavailable_download.status_code == 404
    assert GENERIC_UNAVAILABLE_COPY in unavailable_download.text
    status = client.get(f"/api/runs/{run_id}/delivery").json()
    assert status["state"] == "expired"
    assert status["revoked_utc"] is None
    assert _run_row(run_id)["status"] == "ready"


def test_public_page_is_minimal_escaped_masked_and_manifest_limited(
    client: TestClient,
) -> None:
    malicious_vehicle = {
        "year": "<script>alert(1)</script>",
        "make": "<b>HONDA & Sons</b>",
        "model": 'Accord "EX" <img src=x onerror=alert(2)>',
        "trim": "Photographer secret trim",
    }
    run_id, outputs = _make_run_with_artifacts(
        artifact_types=("sticker_pdf", "buyers_guide_pdf"),
        vehicle=malicious_vehicle,
    )
    created = _create_link(client, run_id)
    public_id, delivery_secret = _credentials_from_response(created)
    token_hash = _delivery_rows(run_id)[0]["token_hash"]
    assert _exchange(client, public_id, delivery_secret).status_code == 204

    page = client.get(f"/d/{public_id}")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    _assert_common_public_headers(page, html_response=True)
    assert "Vehicle files" in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    assert "&lt;b&gt;HONDA &amp; Sons&lt;/b&gt;" in page.text
    assert (
        "Accord &quot;EX&quot; &lt;img src=x "
        "onerror=alert(2)&gt;"
    ) in page.text
    assert "<script>alert(1)</script>" not in page.text
    assert "<b>HONDA & Sons</b>" not in page.text
    assert "<img src=x" not in page.text

    assert re.search(r"VIN ending\s+(?:4352|004352)", page.text)
    assert VIN not in page.text
    for forbidden in (
        INTERNAL_PRICE,
        INTERNAL_NICKNAME,
        "owner@local",
        "Photographer",
        "Northstar Motors",
        "private@northstar.example",
        run_id,
        token_hash,
        outputs["sticker_pdf"],
        outputs["buyers_guide_pdf"],
        "Photographer secret trim",
    ):
        assert forbidden not in page.text
    for internal_key in (
        "owner_id",
        "run_id",
        "dealership_id",
        "token_hash",
        "artifact_manifest",
        "expires_utc",
    ):
        assert internal_key not in page.text

    # Form actions contain only the non-secret public identifier.
    visible_text = re.sub(r"<[^>]+>", "", page.text)
    assert delivery_secret not in visible_text
    assert delivery_secret not in page.text
    assert page.text.count(f"/d/{public_id}/artifact/") == 2
    assert f"/d/{public_id}/artifact/sticker_pdf" in page.text
    assert f"/d/{public_id}/artifact/buyers_guide_pdf" in page.text
    assert f"/d/{public_id}/artifact/photos_zip" not in page.text
    assert 'method="post"' in page.text.lower()
    manifest = json.loads(
        _delivery_rows(run_id)[0]["artifact_manifest_json"]
    )
    assert all(
        entry["relative_path"] not in page.text
        for entry in manifest.values()
    )
    buyers_guide_warning = (
        "Draft Buyers Guide — the dealership must complete all applicable "
        "warranty and dealer-contact fields before display."
    )
    assert buyers_guide_warning in page.text
    buyers_guide_form = (
        f'<form method="post" action="/d/{public_id}/artifact/'
        'buyers_guide_pdf">'
    )
    assert (
        f'<h2>Draft Buyers Guide</h2><p class="warning">'
        f"{buyers_guide_warning}</p>{buyers_guide_form}"
    ) in page.text
    assert (
        f"{buyers_guide_form}<button type=\"submit\">"
        "Download Draft Buyers Guide</button>"
    ) in page.text

    lowered = page.text.lower()
    assert lowered.count("<script") == 1
    assert '<script src="/delivery-bootstrap.js" defer></script>' in lowered
    assert re.search(r"<script(?![^>]*\bsrc=)", lowered) is None
    assert "<img" not in lowered
    assert "<link" not in lowered
    assert "http://" not in lowered
    assert "https://" not in lowered
    assert "@import" not in lowered

    link = _delivery_rows(run_id)[0]
    assert link["first_opened_utc"] is not None
    assert link["first_download_started_utc"] is None
    assert _run_row(run_id)["status"] == "ready"


def test_manifest_snapshots_only_existing_allowlisted_immutable_artifacts(
    client: TestClient,
) -> None:
    run_id, outputs = _make_run_with_artifacts(
        artifact_types=("photos_zip", "sticker_pdf", "buyers_guide_pdf")
    )

    response = _create_link(client, run_id)

    assert response.status_code == 201
    link = _delivery_rows(run_id)[0]
    manifest = json.loads(link["artifact_manifest_json"])
    assert set(manifest) == {
        "photos_zip",
        "sticker_pdf",
        "buyers_guide_pdf",
    }
    expected_types = {
        "photos_zip": "application/zip",
        "sticker_pdf": "application/pdf",
        "buyers_guide_pdf": "application/pdf",
    }
    for artifact_type, entry in manifest.items():
        assert set(entry) == {
            "relative_path",
            "download_name",
            "content_type",
        }
        assert entry["content_type"] == expected_types[artifact_type]
        assert Path(entry["download_name"]).name == entry["download_name"]
        assert VIN not in entry["download_name"]
        assert run_id not in entry["download_name"]
        assert not Path(entry["relative_path"]).is_absolute()
        immutable_path = _manifest_path(entry)
        assert immutable_path.is_file()
        assert immutable_path.read_bytes() == ARTIFACT_CONTENT[artifact_type]
        assert immutable_path.name != outputs[artifact_type]
        assert _uuid_in_text(immutable_path.name)

    # Issuance snapshots copies and does not repoint the owner's latest output.
    assert json.loads(_run_row(run_id)["outputs_json"]) == outputs


def test_public_unavailable_cases_are_byte_identical_and_non_disclosing(
    client: TestClient,
    isolated_persistence,
) -> None:
    revoked_run, _ = _make_run_with_artifacts()
    revoked_public_id, revoked_secret = _credentials_from_response(
        _create_link(client, revoked_run)
    )
    assert _exchange(
        client,
        revoked_public_id,
        revoked_secret,
    ).status_code == 204
    assert (
        client.post(f"/api/runs/{revoked_run}/delivery/revoke").status_code
        == 200
    )
    revoked_exchange = _exchange(
        client,
        revoked_public_id,
        revoked_secret,
    )
    revoked_post = client.post(
        f"/d/{revoked_public_id}/artifact/sticker_pdf"
    )

    expired_run, _ = _make_run_with_artifacts()
    expired_public_id, expired_secret = _credentials_from_response(
        _create_link(client, expired_run)
    )
    assert _exchange(
        client,
        expired_public_id,
        expired_secret,
    ).status_code == 204
    connection = connect_db()
    try:
        connection.execute(
            """
            UPDATE delivery_links
            SET expires_utc = ?
            WHERE run_id = ?
            """,
            (
                (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
                expired_run,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    expired_exchange = _exchange(
        client,
        expired_public_id,
        expired_secret,
    )
    expired_post = client.post(
        f"/d/{expired_public_id}/artifact/sticker_pdf"
    )

    active_run, _ = _make_run_with_artifacts()
    active_public_id, active_secret = _credentials_from_response(
        _create_link(client, active_run)
    )
    nonallowlisted = client.post(
        f"/d/{active_public_id}/artifact/run_report"
    )
    absent = client.post(
        f"/d/{active_public_id}/artifact/buyers_guide_pdf"
    )
    get_artifact = client.get(
        f"/d/{active_public_id}/artifact/sticker_pdf"
    )
    malformed_post = client.post(
        "/d/not!a-valid-token/artifact/sticker_pdf"
    )
    nonexistent_post = client.post(
        f"/d/{'A' * 43}/artifact/sticker_pdf"
    )

    missing_run, _ = _make_run_with_artifacts()
    missing_public_id, missing_secret = _credentials_from_response(
        _create_link(client, missing_run)
    )
    assert _exchange(
        client,
        missing_public_id,
        missing_secret,
    ).status_code == 204
    missing_manifest = json.loads(
        _delivery_rows(missing_run)[0]["artifact_manifest_json"]
    )
    _manifest_path(missing_manifest["sticker_pdf"]).unlink()
    missing_file = client.post(
        f"/d/{missing_public_id}/artifact/sticker_pdf"
    )

    traversal_run, _ = _make_run_with_artifacts()
    traversal_public_id, traversal_secret = _credentials_from_response(
        _create_link(client, traversal_run)
    )
    assert _exchange(
        client,
        traversal_public_id,
        traversal_secret,
    ).status_code == 204
    sentinel = isolated_persistence["database_path"].parent / "sentinel.pdf"
    sentinel.write_bytes(b"must never be public")
    connection = connect_db()
    try:
        row = connection.execute(
            """
            SELECT id, artifact_manifest_json
            FROM delivery_links
            WHERE run_id = ?
            """,
            (traversal_run,),
        ).fetchone()
        manifest = json.loads(row["artifact_manifest_json"])
        manifest["sticker_pdf"]["relative_path"] = "../sentinel.pdf"
        connection.execute(
            """
            UPDATE delivery_links
            SET artifact_manifest_json = ?
            WHERE id = ?
            """,
            (json.dumps(manifest), row["id"]),
        )
        connection.commit()
    finally:
        connection.close()
    traversal_exchange = _exchange(
        client,
        traversal_public_id,
        traversal_secret,
    )
    traversal = client.post(
        f"/d/{traversal_public_id}/artifact/sticker_pdf"
    )

    empty_manifest_run, _ = _make_run_with_artifacts()
    empty_public_id, empty_secret = _credentials_from_response(
        _create_link(client, empty_manifest_run)
    )
    connection = connect_db()
    try:
        connection.execute(
            """
            UPDATE delivery_links
            SET artifact_manifest_json = '{}'
            WHERE run_id = ?
            """,
            (empty_manifest_run,),
        )
        connection.commit()
    finally:
        connection.close()
    empty_manifest_exchange = _exchange(
        client,
        empty_public_id,
        empty_secret,
    )

    overlong_manifest_run, _ = _make_run_with_artifacts()
    overlong_public_id, overlong_secret = _credentials_from_response(
        _create_link(client, overlong_manifest_run)
    )
    connection = connect_db()
    try:
        row = connection.execute(
            """
            SELECT id, artifact_manifest_json
            FROM delivery_links
            WHERE run_id = ?
            """,
            (overlong_manifest_run,),
        ).fetchone()
        overlong_manifest = json.loads(row["artifact_manifest_json"])
        overlong_manifest["sticker_pdf"]["relative_path"] = (
            f"{overlong_manifest_run}/{'x' * 300}.pdf"
        )
        connection.execute(
            """
            UPDATE delivery_links
            SET artifact_manifest_json = ?
            WHERE id = ?
            """,
            (json.dumps(overlong_manifest), row["id"]),
        )
        connection.commit()
    finally:
        connection.close()
    overlong_manifest_exchange = _exchange(
        client,
        overlong_public_id,
        overlong_secret,
    )

    invalid_unicode_run, _ = _make_run_with_artifacts()
    invalid_unicode_public_id, invalid_unicode_secret = (
        _credentials_from_response(
            _create_link(client, invalid_unicode_run)
        )
    )
    connection = connect_db()
    try:
        row = connection.execute(
            """
            SELECT id, artifact_manifest_json
            FROM delivery_links
            WHERE run_id = ?
            """,
            (invalid_unicode_run,),
        ).fetchone()
        invalid_unicode_manifest = json.loads(row["artifact_manifest_json"])
        invalid_unicode_manifest["sticker_pdf"]["relative_path"] = (
            f"{invalid_unicode_run}/\ud800.pdf"
        )
        connection.execute(
            """
            UPDATE delivery_links
            SET artifact_manifest_json = ?
            WHERE id = ?
            """,
            (json.dumps(invalid_unicode_manifest), row["id"]),
        )
        connection.commit()
    finally:
        connection.close()
    invalid_unicode_exchange = _exchange(
        client,
        invalid_unicode_public_id,
        invalid_unicode_secret,
    )

    bootstrap_responses = [
        client.get("/d/not!a-valid-public-id"),
        client.get("/d/" + "A" * 22),
        client.get(f"/d/{active_public_id}"),
        client.get(f"/d/{revoked_public_id}"),
        client.get(f"/d/{expired_public_id}"),
    ]
    bootstrap = bootstrap_responses[0]
    assert bootstrap.status_code == 200
    assert "Opening secure delivery" in bootstrap.text
    assert GENERIC_UNAVAILABLE_COPY in bootstrap.text
    bootstrap_headers = _public_header_subset(bootstrap)
    for response in bootstrap_responses:
        assert response.status_code == 200
        assert response.content == bootstrap.content
        assert _public_header_subset(response) == bootstrap_headers
        _assert_common_public_headers(response, html_response=True)
        assert VIN.encode() not in response.content

    generic_responses = [
        revoked_exchange,
        revoked_post,
        expired_exchange,
        expired_post,
        nonallowlisted,
        absent,
        get_artifact,
        malformed_post,
        nonexistent_post,
        missing_file,
        traversal_exchange,
        traversal,
        empty_manifest_exchange,
        overlong_manifest_exchange,
        invalid_unicode_exchange,
        _exchange(client, "A" * 22, "B" * 43),
        _exchange(client, active_public_id, "B" * 43),
        client.post(
            f"/d/{active_public_id}/exchange",
            content=b"{}",
            headers={"content-type": "application/json"},
        ),
        client.post(
            f"/d/{active_public_id}/exchange",
            content=b"x" * 513,
            headers={"content-type": "application/json"},
        ),
    ]
    baseline = generic_responses[0]
    assert baseline.status_code == 404
    assert GENERIC_UNAVAILABLE_COPY in baseline.text
    baseline_headers = _public_header_subset(baseline)
    for response in generic_responses:
        assert response.status_code == 404
        assert response.content == baseline.content
        assert response.headers["content-type"] == baseline.headers["content-type"]
        assert _public_header_subset(response) == baseline_headers
        _assert_common_public_headers(response, html_response=True)
        for forbidden in (
            VIN,
            INTERNAL_NICKNAME,
            "owner@local",
            "Northstar Motors",
            "private@northstar.example",
            "sentinel.pdf",
        ):
            assert forbidden.encode() not in response.content

    assert sentinel.read_bytes() == b"must never be public"
    assert _run_row(active_run)["status"] == "ready"
    assert _delivery_rows(active_run)[0]["first_download_started_utc"] is None
    assert active_secret.encode() not in baseline.content


def test_public_download_requires_post_marks_delivered_once_and_is_safe(
    client: TestClient,
) -> None:
    run_id, _ = _make_run_with_artifacts()
    created = _create_link(client, run_id)
    public_id, delivery_secret = _credentials_from_response(created)
    artifact_url = f"/d/{public_id}/artifact/sticker_pdf"

    bootstrap = client.get(f"/d/{public_id}")
    assert bootstrap.status_code == 200
    assert "Opening secure delivery" in bootstrap.text
    assert _delivery_rows(run_id)[0]["first_opened_utc"] is None
    assert _exchange(client, public_id, delivery_secret).status_code == 204
    assert _run_row(run_id)["status"] == "ready"
    assert _delivery_rows(run_id)[0]["first_opened_utc"] is None
    assert _delivery_rows(run_id)[0]["first_download_started_utc"] is None

    page = client.get(f"/d/{public_id}")
    assert page.status_code == 200
    assert _run_row(run_id)["status"] == "ready"
    opened_link = _delivery_rows(run_id)[0]
    assert opened_link["first_opened_utc"] is not None
    assert opened_link["first_download_started_utc"] is None

    get_download = client.get(artifact_url)
    assert get_download.status_code != 200
    assert _run_row(run_id)["status"] == "ready"
    assert _delivery_rows(run_id)[0]["first_download_started_utc"] is None

    first = client.post(artifact_url)
    assert first.status_code == 200
    assert first.content == STICKER_BYTES
    assert first.headers["content-type"] == "application/pdf"
    assert first.headers["content-disposition"].startswith("attachment;")
    assert "window_sticker.pdf" in first.headers["content-disposition"]
    assert VIN not in first.headers["content-disposition"]
    assert run_id not in first.headers["content-disposition"]
    _assert_common_public_headers(first, html_response=False)

    first_started = _delivery_rows(run_id)[0][
        "first_download_started_utc"
    ]
    assert first_started is not None
    assert _run_row(run_id)["status"] == "delivered"

    second = client.post(artifact_url)
    assert second.status_code == 200
    assert second.content == STICKER_BYTES
    assert (
        _delivery_rows(run_id)[0]["first_download_started_utc"]
        == first_started
    )
    assert _run_row(run_id)["status"] == "delivered"


def test_public_download_uses_manifest_not_latest_run_outputs(
    client: TestClient,
) -> None:
    run_id, outputs = _make_run_with_artifacts()
    created = _create_link(client, run_id)
    public_id, delivery_secret = _credentials_from_response(created)
    assert _exchange(client, public_id, delivery_secret).status_code == 204
    manifest = json.loads(
        _delivery_rows(run_id)[0]["artifact_manifest_json"]
    )
    immutable_old = _manifest_path(manifest["sticker_pdf"])
    assert immutable_old.read_bytes() == STICKER_BYTES

    new_filename = "newest_owner_sticker.pdf"
    (api.main.RUNS_ROOT / run_id / new_filename).write_bytes(
        NEW_STICKER_BYTES
    )
    connection = connect_db()
    try:
        latest_outputs = dict(outputs)
        latest_outputs["sticker_pdf"] = new_filename
        connection.execute(
            "UPDATE runs SET outputs_json = ? WHERE run_id = ?",
            (json.dumps(latest_outputs), run_id),
        )
        connection.commit()
    finally:
        connection.close()

    public = client.post(f"/d/{public_id}/artifact/sticker_pdf")
    assert public.status_code == 200
    assert public.content == STICKER_BYTES
    assert public.content != NEW_STICKER_BYTES
    assert immutable_old.read_bytes() == STICKER_BYTES


def _fake_photo_builder(vin, _vehicle, files, output_root):
    legacy_run_id = f"{vin}_20260101T000000Z"
    directory = Path(output_root) / legacy_run_id
    directory.mkdir(parents=True, exist_ok=True)
    filenames = [
        f"{vin}_{index:02d}{Path(name).suffix.lower()}"
        for index, (name, _contents) in enumerate(files, start=1)
    ]
    (directory / f"{vin}_photos.zip").write_bytes(NEW_PHOTOS_BYTES)
    (directory / "run_report.json").write_text("{}", encoding="utf-8")
    return {
        "run_id": legacy_run_id,
        "photo_count": len(filenames),
        "filenames": filenames,
        "skipped": [],
        "zip_path": f"{legacy_run_id}/{vin}_photos.zip",
        "report_path": f"{legacy_run_id}/run_report.json",
    }


def _regenerate_artifact(
    client: TestClient,
    monkeypatch,
    artifact_type: str,
    run_id: str,
):
    if artifact_type == "sticker_pdf":
        monkeypatch.setattr(
            api.main,
            "build_sticker_pdf",
            lambda *_args, **_kwargs: NEW_STICKER_BYTES,
        )
        return client.post(
            "/api/sticker",
            json={
                "vin": VIN,
                "vehicle": {**VEHICLE, "trim": "Changed snapshot"},
                "price": "8250",
                "run_id": run_id,
            },
        )
    if artifact_type == "buyers_guide_pdf":
        monkeypatch.setattr(
            api.main,
            "render_buyers_guide",
            lambda *_args, **_kwargs: NEW_BUYERS_GUIDE_BYTES,
        )
        return client.post(
            "/api/buyers-guide",
            json={
                "vin": VIN,
                "make": VEHICLE["make"],
                "model": VEHICLE["model"],
                "year": VEHICLE["year"],
                "version": "as_is",
                "vehicle": {**VEHICLE, "trim": "Changed snapshot"},
                "price": "8250",
                "run_id": run_id,
            },
        )
    assert artifact_type == "photos_zip"
    monkeypatch.setattr(api.main, "build_run", _fake_photo_builder)
    return client.post(
        "/api/photos/package",
        data={
            "vin": VIN,
            "vehicle": json.dumps(
                {
                    **VEHICLE,
                    "trim": "Changed snapshot",
                    "price": "8250",
                }
            ),
            "order": json.dumps(["front.jpg"]),
            "run_id": run_id,
        },
        files=[
            ("photos", ("front.jpg", b"mock photo", "image/jpeg")),
        ],
    )


@pytest.mark.parametrize(
    "artifact_type",
    ["sticker_pdf", "photos_zip", "buyers_guide_pdf"],
)
def test_successful_regeneration_versions_output_revokes_link_and_snapshot(
    client: TestClient,
    monkeypatch,
    artifact_type: str,
) -> None:
    run_id, original_outputs = _make_run_with_artifacts(
        artifact_types=(artifact_type,)
    )
    created = _create_link(client, run_id)
    public_id, delivery_secret = _credentials_from_response(created)
    assert _exchange(client, public_id, delivery_secret).status_code == 204
    original_manifest = json.loads(
        _delivery_rows(run_id)[0]["artifact_manifest_json"]
    )
    immutable_old = _manifest_path(original_manifest[artifact_type])
    old_bytes = immutable_old.read_bytes()

    regenerated = _regenerate_artifact(
        client,
        monkeypatch,
        artifact_type,
        run_id,
    )

    assert regenerated.status_code == 200
    assert regenerated.headers["X-LotKit-Run-ID"] == run_id
    row = _run_row(run_id)
    assert row["status"] == "in_progress"
    latest_outputs = json.loads(row["outputs_json"])
    latest_filename = latest_outputs[artifact_type]
    assert latest_filename != original_outputs[artifact_type]
    assert _uuid_in_text(latest_filename)
    latest_file = api.main.RUNS_ROOT / run_id / latest_filename
    assert latest_file.is_file()
    assert immutable_old.is_file()
    assert immutable_old.read_bytes() == old_bytes
    assert json.loads(row["vehicle_json"])["trim"] == "Changed snapshot"
    assert row["price"] == "8250"

    link = _delivery_rows(run_id)[0]
    assert link["revoked_utc"] is not None
    assert link["revocation_reason"] == "outputs_changed"
    revoked_page = client.get(f"/d/{public_id}")
    assert revoked_page.status_code == 200
    assert "Opening secure delivery" in revoked_page.text
    assert VIN not in revoked_page.text
    assert client.post(
        f"/d/{public_id}/artifact/{artifact_type}"
    ).status_code == 404
    owner_status = client.get(f"/api/runs/{run_id}/delivery").json()
    assert owner_status["state"] == "revoked"
    assert owner_status["revocation_reason"] == "outputs_changed"


def test_failed_generation_does_not_revoke_or_mutate_ready_run(
    client: TestClient,
    monkeypatch,
) -> None:
    run_id, _ = _make_run_with_artifacts()
    public_id, delivery_secret = _credentials_from_response(
        _create_link(client, run_id)
    )
    assert _exchange(client, public_id, delivery_secret).status_code == 204
    before_run = dict(_run_row(run_id))
    before_link = dict(_delivery_rows(run_id)[0])
    before_files = {
        path.name: path.read_bytes()
        for path in (api.main.RUNS_ROOT / run_id).iterdir()
        if path.is_file()
    }

    def fail_generation(*_args, **_kwargs):
        raise RuntimeError("simulated rendering failure")

    monkeypatch.setattr(api.main, "build_sticker_pdf", fail_generation)
    with TestClient(app, raise_server_exceptions=False) as failure_client:
        response = failure_client.post(
            "/api/sticker",
            json={
                "vin": VIN,
                "vehicle": {**VEHICLE, "trim": "Must not persist"},
                "price": "9999",
                "run_id": run_id,
            },
        )

    assert response.status_code == 500
    after_run = dict(_run_row(run_id))
    after_link = dict(_delivery_rows(run_id)[0])
    assert after_run == before_run
    assert after_link == before_link
    assert after_link["revoked_utc"] is None
    assert len(_delivery_rows(run_id)) == 1
    still_active = client.get(f"/d/{public_id}")
    assert still_active.status_code == 200
    assert "VIN ending" in still_active.text
    after_files = {
        path.name: path.read_bytes()
        for path in (api.main.RUNS_ROOT / run_id).iterdir()
        if path.is_file()
    }
    assert after_files == before_files


def test_delivered_run_is_locked_until_explicit_reopen(
    client: TestClient,
    monkeypatch,
) -> None:
    run_id, original_outputs = _make_run_with_artifacts(
        artifact_types=("photos_zip", "sticker_pdf", "buyers_guide_pdf")
    )
    public_id, delivery_secret = _credentials_from_response(
        _create_link(client, run_id)
    )
    assert _exchange(client, public_id, delivery_secret).status_code == 204
    delivered = client.post(f"/d/{public_id}/artifact/sticker_pdf")
    assert delivered.status_code == 200
    assert _run_row(run_id)["status"] == "delivered"
    snapshot_before = _run_row(run_id)["vehicle_json"]

    def must_not_generate(*_args, **_kwargs):
        raise AssertionError("delivered Run reached an artifact generator")

    monkeypatch.setattr(api.main, "build_sticker_pdf", must_not_generate)
    monkeypatch.setattr(api.main, "render_buyers_guide", must_not_generate)
    monkeypatch.setattr(api.main, "build_run", must_not_generate)

    sticker = client.post(
        "/api/sticker",
        json={
            "vin": VIN,
            "vehicle": {**VEHICLE, "trim": "Forbidden"},
            "run_id": run_id,
        },
    )
    photos = client.post(
        "/api/photos/package",
        data={
            "vin": VIN,
            "vehicle": json.dumps({**VEHICLE, "trim": "Forbidden"}),
            "order": json.dumps(["front.jpg"]),
            "run_id": run_id,
        },
        files=[("photos", ("front.jpg", b"photo", "image/jpeg"))],
    )
    buyers = client.post(
        "/api/buyers-guide",
        json={
            "vin": VIN,
            "make": VEHICLE["make"],
            "model": VEHICLE["model"],
            "year": VEHICLE["year"],
            "version": "as_is",
            "vehicle": {**VEHICLE, "trim": "Forbidden"},
            "run_id": run_id,
        },
    )
    for response in (sticker, photos, buyers):
        assert response.status_code == 409
        assert response.json() == {"error": "run_delivered"}
    locked = _run_row(run_id)
    assert locked["status"] == "delivered"
    assert locked["vehicle_json"] == snapshot_before
    assert json.loads(locked["outputs_json"]) == original_outputs
    assert _delivery_rows(run_id)[0]["revoked_utc"] is None

    reopened = client.post(f"/api/runs/{run_id}/reopen")
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "in_progress"
    assert _run_row(run_id)["status"] == "in_progress"
    link = _delivery_rows(run_id)[0]
    assert link["revoked_utc"] is not None
    assert link["revocation_reason"] == "reopened"
    reopened_page = client.get(f"/d/{public_id}")
    assert reopened_page.status_code == 200
    assert "Opening secure delivery" in reopened_page.text
    assert VIN not in reopened_page.text
    assert client.post(
        f"/d/{public_id}/artifact/sticker_pdf"
    ).status_code == 404

    monkeypatch.setattr(
        api.main,
        "build_sticker_pdf",
        lambda *_args, **_kwargs: NEW_STICKER_BYTES,
    )
    allowed = client.post(
        "/api/sticker",
        json={
            "vin": VIN,
            "vehicle": {**VEHICLE, "trim": "Allowed after reopen"},
            "run_id": run_id,
        },
    )
    assert allowed.status_code == 200
    assert _run_row(run_id)["status"] == "in_progress"
    assert json.loads(_run_row(run_id)["vehicle_json"])["trim"] == (
        "Allowed after reopen"
    )


def test_reopen_requires_delivered_status(
    client: TestClient,
) -> None:
    ready_id, _ = _make_run_with_artifacts(status="ready")
    in_progress_id, _ = _make_run_with_artifacts(status="in_progress")
    for run_id in (ready_id, in_progress_id):
        response = client.post(f"/api/runs/{run_id}/reopen")
        assert response.status_code == 409
        assert response.json()["error"] == "run_not_delivered"


def test_delivery_owner_endpoints_are_scoped_and_public_routes_are_not(
    client: TestClient,
) -> None:
    other_owner_id = _insert_user()
    other_run_id, _ = _make_run_with_artifacts(
        owner_id=other_owner_id,
        status="delivered",
    )
    owner_routes = (
        ("post", f"/api/runs/{other_run_id}/delivery"),
        ("get", f"/api/runs/{other_run_id}/delivery"),
        ("post", f"/api/runs/{other_run_id}/delivery/revoke"),
        ("post", f"/api/runs/{other_run_id}/reopen"),
    )
    for method, route in owner_routes:
        response = getattr(client, method)(route)
        assert response.status_code == 404
    assert _delivery_rows(other_run_id) == []
    assert _run_row(other_run_id)["status"] == "delivered"

    public_run_id, _ = _make_run_with_artifacts()
    public_id, delivery_secret = _credentials_from_response(
        _create_link(client, public_run_id)
    )

    def fail_if_owner_dependency_is_used():
        raise AssertionError("public route invoked current_owner_id")

    app.dependency_overrides[current_owner_id] = fail_if_owner_dependency_is_used
    try:
        exchanged = _exchange(client, public_id, delivery_secret)
        public_page = client.get(f"/d/{public_id}")
        public_download = client.post(
            f"/d/{public_id}/artifact/sticker_pdf"
        )
    finally:
        app.dependency_overrides.pop(current_owner_id, None)

    assert exchanged.status_code == 204
    assert public_page.status_code == 200
    assert public_download.status_code == 200
    assert public_download.content == STICKER_BYTES
    assert client.get("/d").status_code == 404


def test_token_hash_collision_is_retried_without_reusing_credential(
    client: TestClient,
    monkeypatch,
) -> None:
    import api.delivery

    first_run, _ = _make_run_with_artifacts()
    first = _create_link(client, first_run)
    _first_public_id, first_secret = _credentials_from_response(first)

    second_run, _ = _make_run_with_artifacts(vin=SECOND_VIN)
    replacement_secret = "B" * 43
    assert replacement_secret != first_secret
    candidates = iter((first_secret, replacement_secret))
    monkeypatch.setattr(
        api.delivery,
        "generate_delivery_token",
        lambda: next(candidates),
    )

    second = _create_link(client, second_run)

    assert second.status_code == 201
    _second_public_id, second_secret = _credentials_from_response(second)
    assert second_secret == replacement_secret
    second_row = _delivery_rows(second_run)[0]
    assert second_row["token_hash"] == hashlib.sha256(
        replacement_secret.encode()
    ).hexdigest()
    assert len(_delivery_rows(first_run)) == 1


def test_delivery_secret_helpers_and_defensive_fragment_log_redaction(
    client: TestClient,
    monkeypatch,
) -> None:
    import api.delivery

    from api.delivery import (
        RedactDeliverySecretFilter,
        generate_delivery_token,
        generate_public_id,
        hash_delivery_token,
        redact_delivery_secrets,
        validate_delivery_token_format,
        validate_public_id_format,
    )

    tokens = {generate_delivery_token() for _ in range(64)}
    assert len(tokens) == 64
    assert all(validate_delivery_token_format(token) for token in tokens)
    assert not validate_delivery_token_format("short")
    assert not validate_delivery_token_format("!" * 43)
    assert not validate_delivery_token_format("A" * 200)
    token = next(iter(tokens))
    token_hash = hash_delivery_token(token)
    assert token_hash == hashlib.sha256(token.encode()).hexdigest()
    public_id = generate_public_id()
    assert validate_public_id_format(public_id)
    assert len(public_id) == 22
    assert not validate_public_id_format("A" * 21)
    assert not validate_public_id_format("A" * 23)

    requested_random_bytes: list[int] = []

    def deterministic_urlsafe(byte_count: int) -> str:
        requested_random_bytes.append(byte_count)
        return "P" * 22

    monkeypatch.setattr(
        api.delivery.secrets,
        "token_urlsafe",
        deterministic_urlsafe,
    )
    assert generate_public_id() == "P" * 22
    assert requested_random_bytes == [16]

    plain_path = f"/d/{public_id}/artifact/sticker_pdf"
    assert redact_delivery_secrets(plain_path) == plain_path
    share_url = f"https://lotkit.example/d/{public_id}#{token}"
    assert redact_delivery_secrets(share_url) == (
        f"https://lotkit.example/d/{public_id}#[REDACTED]"
    )
    assert redact_delivery_secrets(f"/d/{token}") == "/d/[REDACTED]"
    assert redact_delivery_secrets("/api/runs") == "/api/runs"

    redaction_filter = RedactDeliverySecretFilter()
    plain_record = logging.LogRecord(
        "lotkit",
        logging.INFO,
        __file__,
        1,
        f"owner accidentally logged {share_url}",
        (),
        None,
    )
    assert redaction_filter.filter(plain_record) is True
    assert token not in plain_record.getMessage()
    assert public_id in plain_record.getMessage()
    assert "[REDACTED]" in plain_record.getMessage()

    structured_record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "127.0.0.1:1234",
            "POST",
            plain_path,
            "1.1",
            200,
        ),
        None,
    )
    structured_record.path = plain_path
    structured_record.scope = {"path": plain_path}
    assert redaction_filter.filter(structured_record) is True
    assert public_id in structured_record.getMessage()
    assert structured_record.path == plain_path
    assert structured_record.scope["path"] == plain_path
    assert token_hash not in structured_record.getMessage()

    ordinary_record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        "GET /health",
        (),
        None,
    )
    redaction_filter.filter(ordinary_record)
    assert ordinary_record.getMessage() == "GET /health"

    # Lifespan startup installs the filter on Uvicorn's access logger.
    assert any(
        isinstance(item, RedactDeliverySecretFilter)
        for item in logging.getLogger("uvicorn.access").filters
    )


def test_fragment_bootstrap_script_cleans_before_body_exchange_and_fails_closed(
    client: TestClient,
) -> None:
    run_id, _ = _make_run_with_artifacts()
    created = _create_link(client, run_id)
    public_id, delivery_secret = _credentials_from_response(created)

    bootstrap = client.get(f"/d/{public_id}")
    assert bootstrap.status_code == 200
    _assert_common_public_headers(bootstrap, html_response=True)
    assert bootstrap.text.count("<script") == 1
    assert (
        '<script src="/delivery-bootstrap.js" defer></script>'
        in bootstrap.text
    )
    assert re.search(
        r"<script(?![^>]*\bsrc=)",
        bootstrap.text,
        flags=re.IGNORECASE,
    ) is None
    assert "<noscript>" in bootstrap.text
    assert GENERIC_UNAVAILABLE_COPY in bootstrap.text
    for forbidden in (
        VIN,
        INTERNAL_NICKNAME,
        "Northstar Motors",
        run_id,
        delivery_secret,
        "sticker_pdf",
        "window_sticker.pdf",
    ):
        assert forbidden not in bootstrap.text

    script_response = client.get("/delivery-bootstrap.js")
    assert script_response.status_code == 200
    _assert_common_public_headers(script_response, html_response=False)
    script = script_response.text
    fragment_read = script.index("window.location.hash")
    fragment_removed = script.index("window.history.replaceState")
    exchange_fetch = script.index("window.fetch")
    assert fragment_read < fragment_removed < exchange_fetch
    assert 'method: "POST"' in script
    assert 'credentials: "same-origin"' in script
    assert "JSON.stringify({secret: deliverySecret})" in script
    assert "`${publicPath}/exchange`" in script
    assert "window.location.replace(publicPath)" in script
    assert "window.location.reload()" in script
    assert "This delivery link is unavailable." in script
    assert "Ask the person who sent it to create a new link." in script
    for forbidden_api in (
        "location.search",
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "console.",
        "Authorization",
        'type="hidden"',
    ):
        assert forbidden_api not in script


def test_exchange_sets_hashed_scoped_session_without_mutating_run(
    client: TestClient,
    isolated_persistence,
    caplog,
) -> None:
    from api.delivery import DELIVERY_SESSION_COOKIE

    run_id, _ = _make_run_with_artifacts()
    created = _create_link(client, run_id)
    public_id, delivery_secret = _credentials_from_response(created)
    capped_link_expiry = (
        datetime.now(timezone.utc) + timedelta(minutes=30)
    ).isoformat()
    connection = connect_db()
    try:
        connection.execute(
            """
            UPDATE delivery_links
            SET expires_utc = ?
            WHERE run_id = ?
            """,
            (capped_link_expiry, run_id),
        )
        connection.commit()
    finally:
        connection.close()
    link_before = _delivery_rows(run_id)[0]
    assert link_before["first_opened_utc"] is None
    assert link_before["first_download_started_utc"] is None

    expired_session_hash = "e" * 64
    connection = connect_db()
    try:
        connection.execute(
            """
            INSERT INTO delivery_sessions (
                delivery_link_id,
                session_hash,
                created_utc,
                expires_utc
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                link_before["id"],
                expired_session_hash,
                (
                    datetime.now(timezone.utc) - timedelta(hours=2)
                ).isoformat(),
                (
                    datetime.now(timezone.utc) - timedelta(hours=1)
                ).isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    caplog.clear()
    response = _exchange(client, public_id, delivery_secret)

    assert response.status_code == 204
    assert response.content == b""
    _assert_common_public_headers(response, html_response=False)
    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith(f"{DELIVERY_SESSION_COOKIE}=")
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie
    assert f"Path=/d/{public_id}" in set_cookie
    assert "Max-Age=" in set_cookie
    assert "Secure" not in set_cookie
    assert "Domain=" not in set_cookie

    session_credential = response.cookies.get(DELIVERY_SESSION_COOKIE)
    assert session_credential
    assert session_credential != delivery_secret
    sessions = _delivery_session_rows()
    assert len(sessions) == 1
    session = sessions[0]
    assert session["session_hash"] != expired_session_hash
    assert session["delivery_link_id"] == link_before["id"]
    assert session["session_hash"] == hashlib.sha256(
        session_credential.encode()
    ).hexdigest()
    assert session_credential not in tuple(str(value) for value in session)
    assert delivery_secret not in tuple(str(value) for value in session)

    created_utc = datetime.fromisoformat(session["created_utc"])
    expires_utc = datetime.fromisoformat(session["expires_utc"])
    link_expires_utc = datetime.fromisoformat(link_before["expires_utc"])
    assert expires_utc <= created_utc + timedelta(hours=4)
    assert expires_utc <= link_expires_utc
    max_age = int(
        re.search(r"Max-Age=(\d+)", set_cookie, re.IGNORECASE).group(1)
    )
    assert 0 < max_age <= 30 * 60

    database_bytes = isolated_persistence["database_path"].read_bytes()
    assert session_credential.encode() not in database_bytes
    assert delivery_secret.encode() not in database_bytes
    assert session_credential not in caplog.text
    assert delivery_secret not in caplog.text
    assert session["session_hash"] not in caplog.text
    assert link_before["token_hash"] not in caplog.text
    link_after = _delivery_rows(run_id)[0]
    assert link_after["first_opened_utc"] is None
    assert link_after["first_download_started_utc"] is None
    assert _run_row(run_id)["status"] == "ready"


def test_expired_session_and_legacy_path_secret_cannot_authenticate(
    client: TestClient,
) -> None:
    from api.delivery import DELIVERY_SESSION_COOKIE

    run_id, _ = _make_run_with_artifacts()
    created = _create_link(client, run_id)
    public_id, delivery_secret = _credentials_from_response(created)

    # The former bearer-path shape is now interpreted only as a public ID.
    legacy_path = client.get(f"/d/{delivery_secret}")
    assert legacy_path.status_code == 200
    assert "Opening secure delivery" in legacy_path.text
    assert VIN not in legacy_path.text
    assert delivery_secret not in legacy_path.text
    assert _exchange(
        client,
        delivery_secret,
        delivery_secret,
    ).status_code == 404
    assert client.post(
        f"/d/{delivery_secret}/artifact/sticker_pdf",
        headers={"Authorization": f"Bearer {delivery_secret}"},
        params={"secret": delivery_secret},
        json={"secret": delivery_secret},
    ).status_code == 404

    exchanged = _exchange(client, public_id, delivery_secret)
    assert exchanged.status_code == 204
    session_credential = exchanged.cookies.get(DELIVERY_SESSION_COOKIE)
    session_hash = hashlib.sha256(session_credential.encode()).hexdigest()
    connection = connect_db()
    try:
        connection.execute(
            """
            UPDATE delivery_sessions
            SET expires_utc = ?
            WHERE session_hash = ?
            """,
            (
                (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                session_hash,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    expired_page = client.get(f"/d/{public_id}")
    assert expired_page.status_code == 200
    assert "Opening secure delivery" in expired_page.text
    assert VIN not in expired_page.text
    assert client.post(
        f"/d/{public_id}/artifact/sticker_pdf"
    ).status_code == 404
    assert _run_row(run_id)["status"] == "ready"
    assert _delivery_rows(run_id)[0]["first_opened_utc"] is None


def test_public_id_and_session_hash_collisions_are_retried_independently(
    client: TestClient,
    monkeypatch,
) -> None:
    import api.delivery

    first_run, _ = _make_run_with_artifacts()
    first_created = _create_link(client, first_run)
    first_public_id, first_secret = _credentials_from_response(first_created)
    first_exchange = _exchange(client, first_public_id, first_secret)
    first_session = first_exchange.cookies.get(
        api.delivery.DELIVERY_SESSION_COOKIE
    )

    second_run, _ = _make_run_with_artifacts(vin=SECOND_VIN)
    replacement_public_id = "P" * 22
    replacement_secret = "S" * 43
    public_candidates = iter((first_public_id, replacement_public_id))
    secret_candidates = iter(("R" * 43, replacement_secret))
    monkeypatch.setattr(
        api.delivery,
        "generate_public_id",
        lambda: next(public_candidates),
    )
    monkeypatch.setattr(
        api.delivery,
        "generate_delivery_token",
        lambda: next(secret_candidates),
    )

    second_created = _create_link(client, second_run)
    second_public_id, second_secret = _credentials_from_response(
        second_created
    )
    assert second_public_id == replacement_public_id
    assert second_secret == replacement_secret

    replacement_session = "T" * 43
    session_candidates = iter((first_session, replacement_session))
    monkeypatch.setattr(
        api.delivery,
        "generate_delivery_session_credential",
        lambda: next(session_candidates),
    )
    second_exchange = _exchange(
        client,
        second_public_id,
        second_secret,
    )
    assert second_exchange.status_code == 204
    assert (
        second_exchange.cookies.get(api.delivery.DELIVERY_SESSION_COOKIE)
        == replacement_session
    )
    second_session_hash = hashlib.sha256(
        replacement_session.encode()
    ).hexdigest()
    assert any(
        row["session_hash"] == second_session_hash
        for row in _delivery_session_rows()
    )


def test_delivery_session_cookie_is_secure_in_production(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import api.config

    production_data = (tmp_path / "production-data").resolve()
    monkeypatch.setenv("LOTKIT_ENV", "production")
    monkeypatch.setenv("LOTKIT_DATA_DIR", str(production_data))
    monkeypatch.setenv(
        "LOTKIT_PUBLIC_BASE_URL",
        "https://lotkit.example",
    )
    monkeypatch.setenv("LOTKIT_TRUSTED_HOSTS", "lotkit.example")
    api.config.reset_settings_cache()
    settings = api.config.get_settings()
    monkeypatch.setattr(api.main, "RUNS_ROOT", settings.runs_dir)
    production_app = api.main.create_app(settings)

    with TestClient(
        production_app,
        base_url="https://lotkit.example",
    ) as production_client:
        owner_id = _owner_id()
        production_app.dependency_overrides[current_owner_id] = (
            lambda: owner_id
        )
        run_id, _ = _make_run_with_artifacts(owner_id=owner_id)
        created = _create_link(production_client, run_id)
        public_id, delivery_secret = _credentials_from_response(created)
        assert created.json()["share_url"].startswith(
            "https://lotkit.example/d/"
        )

        exchanged = _exchange(
            production_client,
            public_id,
            delivery_secret,
        )

    assert exchanged.status_code == 204
    set_cookie = exchanged.headers["set-cookie"]
    assert "Secure" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie
    assert f"Path=/d/{public_id}" in set_cookie
    assert "Domain=" not in set_cookie


def test_delivery_preserves_run_history_csv_and_owner_artifact_access(
    client: TestClient,
) -> None:
    run_id, _ = _make_run_with_artifacts(
        artifact_types=("photos_zip", "sticker_pdf", "buyers_guide_pdf")
    )

    created = _create_link(client, run_id)

    assert created.status_code == 201
    assert _run_row(run_id)["status"] == "ready"
    connection = connect_db()
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM runs"
        ).fetchone()[0] == 1
    finally:
        connection.close()
    history = client.get("/api/runs")
    assert history.status_code == 200
    assert [item["run_id"] for item in history.json()] == [run_id]
    csv_export = client.get("/api/runs/export.csv")
    assert csv_export.status_code == 200
    assert run_id in csv_export.text

    expected = {
        "photos_zip": PHOTOS_BYTES,
        "sticker_pdf": STICKER_BYTES,
        "buyers_guide_pdf": BUYERS_GUIDE_BYTES,
    }
    for artifact_type, contents in expected.items():
        owner_download = client.get(
            f"/api/runs/{run_id}/artifacts/{artifact_type}"
        )
        assert owner_download.status_code == 200
        assert owner_download.content == contents
