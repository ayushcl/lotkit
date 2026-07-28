import hashlib
import json
import logging
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def _create_link(client: TestClient, run_id: str):
    return client.post(f"/api/runs/{run_id}/delivery")


def _token_from_response(response) -> str:
    public_path = response.json()["public_path"]
    assert public_path.startswith("/d/")
    token = public_path.removeprefix("/d/")
    assert "/" not in token
    return token


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
        assert "\n" not in csp
        for directive in (
            "default-src 'none'",
            "style-src 'unsafe-inline'",
            "form-action 'self'",
            "base-uri 'none'",
            "frame-ancestors 'none'",
        ):
            assert directive in csp


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
        "public_path",
        "expires_utc",
        "state",
        "token_hint",
    }
    assert response.json()["state"] == "active"
    token = _token_from_response(response)
    assert re.fullmatch(r"[A-Za-z0-9_-]{40,64}", token)
    assert token != run_id
    assert token != VIN
    assert VIN not in token
    assert run_id not in token
    assert not re.search(r"\d{8}T\d{6}", token)

    rows = _delivery_rows(run_id)
    assert len(rows) == 1
    link = rows[0]
    assert link["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert link["token_hint"] == token[-6:]
    assert token not in tuple(str(value) for value in link)
    assert response.json()["token_hint"] == token[-6:]
    created = datetime.fromisoformat(link["created_utc"])
    expires = datetime.fromisoformat(link["expires_utc"])
    assert timedelta(days=29, hours=23, minutes=59) < expires - created
    assert expires - created < timedelta(days=30, minutes=1)
    assert _run_row(run_id)["status"] == "ready"

    database_bytes = isolated_persistence["database_path"].read_bytes()
    assert token.encode() not in database_bytes
    assert response.json()["public_path"].encode() not in database_bytes

    public = client.get(response.json()["public_path"])
    assert public.status_code == 200
    owner_status = client.get(f"/api/runs/{run_id}/delivery")
    assert owner_status.status_code == 200
    serialized_status = json.dumps(owner_status.json())
    assert token not in serialized_status
    assert link["token_hash"] not in serialized_status
    assert "token_hash" not in owner_status.json()
    assert "artifact_manifest" not in serialized_status
    assert "relative_path" not in serialized_status
    assert owner_status.json()["token_hint"] == token[-6:]


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
    first_token = _token_from_response(first)

    second = _create_link(client, run_id)
    assert second.status_code == 201
    second_token = _token_from_response(second)
    assert second_token != first_token
    assert _run_row(run_id)["status"] == "ready"

    rows = _delivery_rows(run_id)
    assert len(rows) == 2
    assert rows[0]["revoked_utc"] is not None
    assert rows[0]["revocation_reason"] == "replaced"
    assert rows[1]["revoked_utc"] is None
    assert sum(row["revoked_utc"] is None for row in rows) == 1
    first_unavailable = client.get(f"/d/{first_token}")
    assert first_unavailable.status_code == 404
    assert GENERIC_UNAVAILABLE_COPY in first_unavailable.text
    assert client.get(f"/d/{second_token}").status_code == 200

    connection = connect_db()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO delivery_links (
                    owner_id,
                    run_id,
                    token_hash,
                    token_hint,
                    artifact_manifest_json,
                    created_utc,
                    expires_utc
                )
                VALUES (?, ?, ?, ?, '{}', ?, ?)
                """,
                (
                    _owner_id(),
                    run_id,
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
    assert client.get(f"/d/{second_token}").status_code == 404
    assert client.post(f"/api/runs/{run_id}/delivery/revoke").status_code == 200
    owner_status = client.get(f"/api/runs/{run_id}/delivery").json()
    assert owner_status["state"] == "revoked"
    assert owner_status["revocation_reason"] == "manual"


def test_expiry_is_derived_from_expires_utc_on_every_request(
    client: TestClient,
) -> None:
    run_id, _ = _make_run_with_artifacts()
    created = _create_link(client, run_id)
    token = _token_from_response(created)
    assert client.get(f"/d/{token}").status_code == 200

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

    response = client.get(f"/d/{token}")
    assert response.status_code == 404
    assert GENERIC_UNAVAILABLE_COPY in response.text
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
    token = _token_from_response(created)
    token_hash = _delivery_rows(run_id)[0]["token_hash"]

    page = client.get(f"/d/{token}")

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

    # The bearer is necessarily present in the deliberate form actions, but
    # must not be rendered as visible page text or elsewhere as metadata.
    visible_text = re.sub(r"<[^>]+>", "", page.text)
    assert token not in visible_text
    assert page.text.count(f"/d/{token}/artifact/") == 2
    assert f"/d/{token}/artifact/sticker_pdf" in page.text
    assert f"/d/{token}/artifact/buyers_guide_pdf" in page.text
    assert f"/d/{token}/artifact/photos_zip" not in page.text
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
        f'<form method="post" action="/d/{token}/artifact/'
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
    assert "<script" not in lowered
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
    malformed = client.get("/d/not!a-valid-token")
    nonexistent = client.get("/d/" + "A" * 43)

    revoked_run, _ = _make_run_with_artifacts()
    revoked_token = _token_from_response(_create_link(client, revoked_run))
    assert (
        client.post(f"/api/runs/{revoked_run}/delivery/revoke").status_code
        == 200
    )
    revoked = client.get(f"/d/{revoked_token}")
    revoked_post = client.post(
        f"/d/{revoked_token}/artifact/sticker_pdf"
    )

    expired_run, _ = _make_run_with_artifacts()
    expired_token = _token_from_response(_create_link(client, expired_run))
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
    expired = client.get(f"/d/{expired_token}")
    expired_post = client.post(
        f"/d/{expired_token}/artifact/sticker_pdf"
    )

    active_run, _ = _make_run_with_artifacts()
    active_token = _token_from_response(_create_link(client, active_run))
    nonallowlisted = client.post(
        f"/d/{active_token}/artifact/run_report"
    )
    absent = client.post(
        f"/d/{active_token}/artifact/buyers_guide_pdf"
    )
    get_artifact = client.get(
        f"/d/{active_token}/artifact/sticker_pdf"
    )
    malformed_post = client.post(
        "/d/not!a-valid-token/artifact/sticker_pdf"
    )
    nonexistent_post = client.post(
        f"/d/{'A' * 43}/artifact/sticker_pdf"
    )

    missing_run, _ = _make_run_with_artifacts()
    missing_token = _token_from_response(_create_link(client, missing_run))
    missing_manifest = json.loads(
        _delivery_rows(missing_run)[0]["artifact_manifest_json"]
    )
    _manifest_path(missing_manifest["sticker_pdf"]).unlink()
    missing_file = client.post(
        f"/d/{missing_token}/artifact/sticker_pdf"
    )

    traversal_run, _ = _make_run_with_artifacts()
    traversal_token = _token_from_response(
        _create_link(client, traversal_run)
    )
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
    traversal = client.post(
        f"/d/{traversal_token}/artifact/sticker_pdf"
    )

    responses = [
        malformed,
        nonexistent,
        revoked,
        revoked_post,
        expired,
        expired_post,
        nonallowlisted,
        absent,
        get_artifact,
        malformed_post,
        nonexistent_post,
        missing_file,
        traversal,
    ]
    baseline = responses[0]
    assert baseline.status_code == 404
    assert GENERIC_UNAVAILABLE_COPY in baseline.text
    baseline_headers = _public_header_subset(baseline)
    for response in responses:
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


def test_public_download_requires_post_marks_delivered_once_and_is_safe(
    client: TestClient,
) -> None:
    run_id, _ = _make_run_with_artifacts()
    created = _create_link(client, run_id)
    token = _token_from_response(created)
    artifact_url = f"/d/{token}/artifact/sticker_pdf"

    page = client.get(f"/d/{token}")
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
    token = _token_from_response(created)
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

    public = client.post(f"/d/{token}/artifact/sticker_pdf")
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
    token = _token_from_response(created)
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
    assert client.get(f"/d/{token}").status_code == 404
    owner_status = client.get(f"/api/runs/{run_id}/delivery").json()
    assert owner_status["state"] == "revoked"
    assert owner_status["revocation_reason"] == "outputs_changed"


def test_failed_generation_does_not_revoke_or_mutate_ready_run(
    client: TestClient,
    monkeypatch,
) -> None:
    run_id, _ = _make_run_with_artifacts()
    token = _token_from_response(_create_link(client, run_id))
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
    assert client.get(f"/d/{token}").status_code == 200
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
    token = _token_from_response(_create_link(client, run_id))
    delivered = client.post(f"/d/{token}/artifact/sticker_pdf")
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
    assert client.get(f"/d/{token}").status_code == 404

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
    token = _token_from_response(_create_link(client, public_run_id))

    def fail_if_owner_dependency_is_used():
        raise AssertionError("public route invoked current_owner_id")

    app.dependency_overrides[current_owner_id] = fail_if_owner_dependency_is_used
    try:
        public_page = client.get(f"/d/{token}")
        public_download = client.post(
            f"/d/{token}/artifact/sticker_pdf"
        )
    finally:
        app.dependency_overrides.pop(current_owner_id, None)

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
    first_token = _token_from_response(first)

    second_run, _ = _make_run_with_artifacts(vin=SECOND_VIN)
    replacement_token = "B" * 43
    assert replacement_token != first_token
    candidates = iter((first_token, replacement_token))
    monkeypatch.setattr(
        api.delivery,
        "generate_delivery_token",
        lambda: next(candidates),
    )

    second = _create_link(client, second_run)

    assert second.status_code == 201
    assert _token_from_response(second) == replacement_token
    second_row = _delivery_rows(second_run)[0]
    assert second_row["token_hash"] == hashlib.sha256(
        replacement_token.encode()
    ).hexdigest()
    assert len(_delivery_rows(first_run)) == 1


def test_delivery_token_helpers_and_log_redaction(
    client: TestClient,
) -> None:
    from api.delivery import (
        RedactDeliveryTokenFilter,
        generate_delivery_token,
        hash_delivery_token,
        redact_delivery_tokens,
        validate_delivery_token_format,
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

    plain_path = f"/d/{token}/artifact/sticker_pdf"
    assert redact_delivery_tokens(plain_path) == (
        "/d/[REDACTED]/artifact/sticker_pdf"
    )
    assert redact_delivery_tokens("/api/runs") == "/api/runs"

    redaction_filter = RedactDeliveryTokenFilter()
    plain_record = logging.LogRecord(
        "lotkit",
        logging.INFO,
        __file__,
        1,
        f"serving {plain_path}",
        (),
        None,
    )
    assert redaction_filter.filter(plain_record) is True
    assert token not in plain_record.getMessage()
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
    assert token not in structured_record.getMessage()
    assert "[REDACTED]" in structured_record.getMessage()
    assert token not in structured_record.path
    assert token not in structured_record.scope["path"]
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
        isinstance(item, RedactDeliveryTokenFilter)
        for item in logging.getLogger("uvicorn.access").filters
    )


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
