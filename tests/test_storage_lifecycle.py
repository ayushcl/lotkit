import hashlib
import json
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.main
import api.storage
from api.cleanup_storage import main as cleanup_main
from api.db import connect_db, init_db
from api.delivery import revoke_owner_delivery_link
from api.runs import (
    RunTarget,
    discard_run,
    get_run_detail,
    record_successful_output,
)
from api.storage import (
    StorageSafetyError,
    apply_storage_lifecycle,
    enqueue_cleanup_job,
    expire_delivery_links,
    process_cleanup_jobs,
    retire_eligible_runs,
    revoke_run_delivery_links,
    safe_manifest_paths,
    select_retention_candidates,
    storage_plan,
    storage_report,
    validate_cleanup_path,
)


VIN = "1HGCM82633A004352"
NOW = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)


def _owner_id() -> int:
    connection = connect_db()
    try:
        return int(connection.execute("SELECT id FROM users LIMIT 1").fetchone()[0])
    finally:
        connection.close()


def _insert_run(
    *,
    status: str = "in_progress",
    outputs: dict[str, str] | None = None,
    run_id: str | None = None,
) -> tuple[str, Path]:
    identifier = run_id or str(uuid.uuid4())
    output_values = outputs or {}
    connection = connect_db()
    try:
        connection.execute(
            """
            INSERT INTO runs (
                run_id,
                owner_id,
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
            VALUES (?, ?, '{}', ?, ?, '9000', 'Blue', 'Black', ?, ?, ?, ?, ?)
            """,
            (
                identifier,
                _owner_id(),
                VIN,
                json.dumps({"year": "2003", "make": "HONDA"}),
                json.dumps([f"{VIN}_01.jpg"]),
                json.dumps(output_values),
                status,
                (NOW - timedelta(days=90)).isoformat(),
                (NOW - timedelta(days=45)).isoformat(),
            ),
        )
        connection.commit()
    finally:
        connection.close()

    root = api.storage.get_settings().runs_dir
    directory = root / identifier
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run_report.json").write_text(
        '{"preserved":true}\n',
        encoding="utf-8",
    )
    for artifact_type, filename in output_values.items():
        if artifact_type in api.storage.ARTIFACT_TYPES:
            (directory / filename).write_bytes(
                f"{artifact_type}-bytes".encode()
            )
    return identifier, directory


def _manifest_entry(
    run_id: str,
    artifact_type: str,
    *,
    contents: bytes = b"snapshot",
) -> tuple[dict[str, str], Path]:
    policy = api.storage.MANIFEST_POLICIES[artifact_type]
    filename = (
        f"{artifact_type}_{uuid.uuid4().hex}{policy['suffix']}"
    )
    path = api.storage.get_settings().runs_dir / run_id / filename
    path.write_bytes(contents)
    return (
        {
            "relative_path": f"{run_id}/{filename}",
            "download_name": policy["download_name"],
            "content_type": policy["content_type"],
        },
        path,
    )


def _insert_link(
    run_id: str,
    *,
    expires: datetime,
    revoked: bool = False,
    first_download: datetime | None = None,
    artifact_type: str | None = None,
) -> tuple[int, Path | None]:
    manifest: dict[str, dict[str, str]] = {}
    snapshot_path = None
    if artifact_type is not None:
        entry, snapshot_path = _manifest_entry(run_id, artifact_type)
        manifest[artifact_type] = entry
    connection = connect_db()
    try:
        created = NOW - timedelta(days=60)
        cursor = connection.execute(
            """
            INSERT INTO delivery_links (
                owner_id,
                run_id,
                public_id,
                token_hash,
                token_hint,
                artifact_manifest_json,
                created_utc,
                expires_utc,
                first_opened_utc,
                first_download_started_utc,
                revoked_utc,
                revocation_reason
            )
            VALUES (?, ?, ?, ?, 'abcdef', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _owner_id(),
                run_id,
                uuid.uuid4().hex[:22],
                uuid.uuid4().hex + uuid.uuid4().hex,
                json.dumps(manifest),
                created.isoformat(),
                expires.isoformat(),
                created.isoformat() if first_download else None,
                first_download.isoformat() if first_download else None,
                created.isoformat() if revoked else None,
                "manual" if revoked else None,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid), snapshot_path
    finally:
        connection.close()


def _job_rows(run_id: str) -> list[sqlite3.Row]:
    connection = connect_db()
    try:
        return connection.execute(
            """
            SELECT *
            FROM storage_cleanup_jobs
            WHERE run_id = ?
            ORDER BY id
            """,
            (run_id,),
        ).fetchall()
    finally:
        connection.close()


def test_storage_schema_is_additive_idempotent_and_indexed() -> None:
    init_db()
    init_db()
    connection = connect_db()
    try:
        run_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(runs)")
        }
        assert "artifacts_purged_utc" in run_columns
        job_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(storage_cleanup_jobs)"
            )
        }
        assert job_columns == {
            "id",
            "run_id",
            "relative_path",
            "reason",
            "created_utc",
            "attempt_count",
            "last_attempt_utc",
            "last_error",
            "completed_utc",
        }
        indexes = {
            row["name"]: row
            for row in connection.execute(
                "PRAGMA index_list(storage_cleanup_jobs)"
            )
        }
        assert indexes["storage_cleanup_jobs_pending_path_idx"]["unique"] == 1
        assert indexes["storage_cleanup_jobs_pending_path_idx"]["partial"] == 1
        assert {
            "storage_cleanup_jobs_pending_idx",
            "storage_cleanup_jobs_run_id_idx",
            "storage_cleanup_jobs_created_utc_idx",
        } <= set(indexes)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


@pytest.mark.parametrize(
    "relative_path",
    [
        "/tmp/outside.pdf",
        "../outside.pdf",
        "run/../outside.pdf",
        "run//outside.pdf",
        "",
        "one-component.pdf",
    ],
)
def test_cleanup_paths_reject_absolute_traversal_and_empty_components(
    relative_path: str,
) -> None:
    run_id = str(uuid.uuid4())
    with pytest.raises(StorageSafetyError):
        validate_cleanup_path(run_id, relative_path)


def test_cleanup_paths_reject_legacy_directories_symlinks_and_directories(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "1HGCM82633A004352_20260101T000000Z"
    legacy.mkdir()
    (legacy / "old.zip").write_bytes(b"legacy")
    with pytest.raises(StorageSafetyError):
        validate_cleanup_path(
            legacy.name,
            f"{legacy.name}/old.zip",
            tmp_path,
        )

    run_id = str(uuid.uuid4())
    run_directory = tmp_path / run_id
    run_directory.mkdir()
    (run_directory / "directory.pdf").mkdir()
    with pytest.raises(StorageSafetyError):
        validate_cleanup_path(
            run_id,
            f"{run_id}/directory.pdf",
            tmp_path,
        )

    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    (run_directory / "link.pdf").symlink_to(outside)
    with pytest.raises(StorageSafetyError):
        validate_cleanup_path(run_id, f"{run_id}/link.pdf", tmp_path)
    assert outside.read_bytes() == b"outside"


def test_cleanup_success_missing_retry_and_idempotence(monkeypatch) -> None:
    run_id, directory = _insert_run()
    disposable = directory / "old.pdf"
    disposable.write_bytes(b"old artifact")
    missing_relative = f"{run_id}/already-missing.pdf"

    connection = connect_db()
    try:
        first = enqueue_cleanup_job(
            connection,
            run_id,
            f"{run_id}/old.pdf",
            "superseded_sticker_pdf",
        )
        duplicate = enqueue_cleanup_job(
            connection,
            run_id,
            f"{run_id}/old.pdf",
            "superseded_sticker_pdf",
        )
        enqueue_cleanup_job(
            connection,
            run_id,
            missing_relative,
            "superseded_sticker_pdf",
        )
        connection.commit()
        assert duplicate.job_id == first.job_id
        assert duplicate.newly_queued is False

        original_unlink = api.storage._unlink_exact_file
        calls = 0

        def fail_once(validated):
            nonlocal calls
            calls += 1
            if validated.filename == "old.pdf" and calls == 1:
                raise PermissionError("simulated unlink denial")
            return original_unlink(validated)

        monkeypatch.setattr(api.storage, "_unlink_exact_file", fail_once)
        failed = process_cleanup_jobs(connection, limit=10)
        assert failed.failures == 1
        assert failed.already_missing == 1
        assert disposable.is_file()

        monkeypatch.setattr(
            api.storage,
            "_unlink_exact_file",
            original_unlink,
        )
        retried = process_cleanup_jobs(connection, limit=10)
        assert retried.deleted == 1
        assert retried.bytes_reclaimed == len(b"old artifact")
        assert not disposable.exists()
        assert process_cleanup_jobs(connection, limit=10).attempted == 0
    finally:
        connection.close()

    jobs = _job_rows(run_id)
    assert len(jobs) == 2
    assert all(row["completed_utc"] for row in jobs)
    assert all(row["last_error"] is None for row in jobs)
    assert max(row["attempt_count"] for row in jobs) == 2


def test_concurrent_queue_insertion_creates_one_pending_job() -> None:
    run_id, directory = _insert_run()
    (directory / "concurrent.pdf").write_bytes(b"one job")
    barrier = threading.Barrier(2)

    def enqueue() -> int:
        connection = connect_db()
        try:
            barrier.wait(timeout=5)
            queued = enqueue_cleanup_job(
                connection,
                run_id,
                f"{run_id}/concurrent.pdf",
                "superseded_sticker_pdf",
            )
            connection.commit()
            return queued.job_id
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        job_ids = list(executor.map(lambda _item: enqueue(), range(2)))

    assert job_ids[0] == job_ids[1]
    jobs = _job_rows(run_id)
    assert len(jobs) == 1
    assert jobs[0]["completed_utc"] is None


def test_active_snapshot_and_current_output_are_protected_until_retired() -> None:
    outputs = {"sticker_pdf": "current.pdf"}
    run_id, directory = _insert_run(status="ready", outputs=outputs)
    link_id, snapshot = _insert_link(
        run_id,
        expires=NOW + timedelta(days=1),
        artifact_type="sticker_pdf",
    )
    assert snapshot is not None

    connection = connect_db()
    try:
        enqueue_cleanup_job(
            connection,
            run_id,
            f"{run_id}/current.pdf",
            "test_protected",
        )
        manifest = connection.execute(
            "SELECT artifact_manifest_json FROM delivery_links WHERE id = ?",
            (link_id,),
        ).fetchone()[0]
        snapshot_relative = json.loads(manifest)["sticker_pdf"][
            "relative_path"
        ]
        enqueue_cleanup_job(
            connection,
            run_id,
            snapshot_relative,
            "test_protected",
        )
        connection.commit()

        protected = process_cleanup_jobs(connection, now=NOW, limit=10)
        assert protected.failures == 2
        assert (directory / "current.pdf").is_file()
        assert snapshot.is_file()

        connection.execute(
            "UPDATE runs SET outputs_json = '{}' WHERE run_id = ?",
            (run_id,),
        )
        revoked = revoke_run_delivery_links(
            connection,
            run_id,
            "manual",
            now=NOW,
        )
        assert revoked.links_revoked == 1
        connection.commit()
        cleaned = process_cleanup_jobs(connection, now=NOW, limit=10)
        assert cleaned.deleted == 2
        assert not snapshot.exists()
    finally:
        connection.close()


def test_malformed_or_unbounded_manifest_cannot_delete_an_outside_file(
    tmp_path: Path,
) -> None:
    run_id, _directory = _insert_run()
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"do not delete")
    malformed_values = [
        "{not-json",
        "[" * 2000 + "]" * 2000,
        '{"sticker_pdf":{"relative_path":"../../outside.pdf"}}',
        "\ud800",
        {
            "sticker_pdf": {
                "relative_path": f"{run_id}/not-an-immutable-name.pdf",
                "download_name": "window_sticker.pdf",
                "content_type": "application/pdf",
            }
        },
    ]
    for malformed in malformed_values:
        assert safe_manifest_paths(malformed, run_id) == ()
    assert outside.read_bytes() == b"do not delete"


def test_output_replacement_is_generic_and_unchanged_names_are_not_queued() -> None:
    outputs = {
        "photos_zip": "old.zip",
        "sticker_pdf": "old-sticker.pdf",
        "buyers_guide_pdf": "old-guide.pdf",
    }
    run_id, directory = _insert_run(outputs=outputs)
    connection = connect_db()
    try:
        for artifact_type in api.storage.ARTIFACT_TYPES:
            suffix = ".zip" if artifact_type == "photos_zip" else ".pdf"
            new_filename = f"new-{artifact_type}{suffix}"
            (directory / new_filename).write_bytes(b"new")
            record_successful_output(
                connection,
                _owner_id(),
                RunTarget(run_id=run_id, is_new=False),
                vin=VIN,
                dealership_id=None,
                output_updates={artifact_type: new_filename},
            )
            assert not (directory / outputs[artifact_type]).exists()
            assert (directory / new_filename).is_file()
            outputs[artifact_type] = new_filename

        before_jobs = len(_job_rows(run_id))
        record_successful_output(
            connection,
            _owner_id(),
            RunTarget(run_id=run_id, is_new=False),
            vin=VIN,
            dealership_id=None,
            output_updates={"sticker_pdf": outputs["sticker_pdf"]},
        )
        assert len(_job_rows(run_id)) == before_jobs
    finally:
        connection.close()


def test_output_cleanup_failure_does_not_fail_committed_replacement(
    monkeypatch,
) -> None:
    run_id, directory = _insert_run(
        outputs={"sticker_pdf": "old.pdf"},
    )
    (directory / "new.pdf").write_bytes(b"new")

    def fail_unlink(_validated):
        raise PermissionError("simulated")

    monkeypatch.setattr(api.storage, "_unlink_exact_file", fail_unlink)
    connection = connect_db()
    try:
        detail = record_successful_output(
            connection,
            _owner_id(),
            RunTarget(run_id=run_id, is_new=False),
            vin=VIN,
            dealership_id=None,
            output_updates={"sticker_pdf": "new.pdf"},
        )
        assert detail["outputs"]["sticker_pdf"] == "new.pdf"
        assert (directory / "new.pdf").is_file()
        assert (directory / "old.pdf").is_file()
    finally:
        connection.close()
    job = _job_rows(run_id)[0]
    assert job["completed_utc"] is None
    assert job["attempt_count"] == 1


def test_failed_database_update_keeps_old_reference_and_removes_new_file(
    monkeypatch,
) -> None:
    run_id, directory = _insert_run(
        outputs={"sticker_pdf": "old.pdf"},
    )
    monkeypatch.setattr(
        api.main,
        "build_sticker_pdf",
        lambda *_args, **_kwargs: b"new sticker",
    )
    connection = connect_db()
    try:
        connection.execute(
            """
            CREATE TRIGGER fail_run_update
            BEFORE UPDATE ON runs
            BEGIN
                SELECT RAISE(ABORT, 'simulated database failure');
            END
            """
        )
        connection.commit()
    finally:
        connection.close()

    with TestClient(api.main.app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/sticker",
            json={
                "vin": VIN,
                "vehicle": {"year": "2003", "make": "HONDA"},
                "run_id": run_id,
            },
        )
    assert response.status_code == 500
    connection = connect_db()
    try:
        outputs = json.loads(
            connection.execute(
                "SELECT outputs_json FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
        assert outputs["sticker_pdf"] == "old.pdf"
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM storage_cleanup_jobs WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()
    assert (directory / "old.pdf").is_file()
    assert sorted(path.name for path in directory.glob("*.pdf")) == [
        "old.pdf"
    ]


def test_revocation_commit_survives_unlink_failure_and_retry(monkeypatch) -> None:
    run_id, _directory = _insert_run(status="ready")
    link_id, snapshot = _insert_link(
        run_id,
        expires=NOW + timedelta(days=1),
        artifact_type="sticker_pdf",
    )
    assert snapshot is not None
    original_unlink = api.storage._unlink_exact_file

    def fail_unlink(_validated):
        raise PermissionError("simulated")

    monkeypatch.setattr(api.storage, "_unlink_exact_file", fail_unlink)
    connection = connect_db()
    try:
        result = revoke_owner_delivery_link(
            connection,
            _owner_id(),
            run_id,
            NOW,
        )
        assert result["revoked"] is True
        link = connection.execute(
            "SELECT * FROM delivery_links WHERE id = ?",
            (link_id,),
        ).fetchone()
        assert link["revocation_reason"] == "manual"
        assert link["artifact_manifest_json"]
        assert snapshot.is_file()
        pending = _job_rows(run_id)[0]
        assert pending["completed_utc"] is None

        monkeypatch.setattr(
            api.storage,
            "_unlink_exact_file",
            original_unlink,
        )
        assert process_cleanup_jobs(connection, now=NOW).deleted == 1
        assert not snapshot.exists()
    finally:
        connection.close()


def test_expiry_revokes_preserves_history_and_deletes_only_expired_snapshot() -> None:
    expired_run, _ = _insert_run(status="ready")
    expired_id, expired_snapshot = _insert_link(
        expired_run,
        expires=NOW,
        first_download=NOW - timedelta(days=40),
        artifact_type="sticker_pdf",
    )
    active_run, _ = _insert_run(status="ready")
    active_id, active_snapshot = _insert_link(
        active_run,
        expires=NOW + timedelta(seconds=1),
        artifact_type="sticker_pdf",
    )

    connection = connect_db()
    try:
        result = expire_delivery_links(connection, now=NOW)
        assert result.links_expired == 1
        assert result.link_ids == (expired_id,)
        assert result.cleanup.deleted == 1
        expired = connection.execute(
            "SELECT * FROM delivery_links WHERE id = ?",
            (expired_id,),
        ).fetchone()
        assert expired["revocation_reason"] == "expired"
        assert expired["artifact_manifest_json"]
        assert expired["first_opened_utc"]
        assert expired["first_download_started_utc"]
        active = connection.execute(
            "SELECT * FROM delivery_links WHERE id = ?",
            (active_id,),
        ).fetchone()
        assert active["revoked_utc"] is None
        assert active_snapshot is not None and active_snapshot.is_file()
        assert expired_snapshot is not None and not expired_snapshot.exists()
        assert expire_delivery_links(connection, now=NOW).links_expired == 0
    finally:
        connection.close()


def test_transport_migration_queues_and_cleans_a_valid_manifest() -> None:
    run_id, _ = _insert_run(status="ready")
    link_id, snapshot = _insert_link(
        run_id,
        expires=NOW + timedelta(days=1),
        artifact_type="sticker_pdf",
    )
    assert snapshot is not None
    connection = connect_db()
    try:
        connection.execute(
            "UPDATE delivery_links SET public_id = NULL WHERE id = ?",
            (link_id,),
        )
        connection.commit()
    finally:
        connection.close()

    init_db()
    connection = connect_db()
    try:
        link = connection.execute(
            "SELECT * FROM delivery_links WHERE id = ?",
            (link_id,),
        ).fetchone()
        assert link["revocation_reason"] == "transport_migrated"
        assert link["artifact_manifest_json"]
        job = connection.execute(
            """
            SELECT *
            FROM storage_cleanup_jobs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        assert job["reason"] == "delivery_transport_migrated"
        assert job["completed_utc"]
        assert not snapshot.exists()
    finally:
        connection.close()


def test_retention_uses_latest_download_and_all_eligibility_guards() -> None:
    old_run, old_directory = _insert_run(
        status="delivered",
        outputs={"sticker_pdf": "old.pdf"},
    )
    _insert_link(
        old_run,
        expires=NOW - timedelta(days=39),
        revoked=True,
        first_download=NOW - timedelta(days=40),
    )

    recent_run, _ = _insert_run(
        status="delivered",
        outputs={"sticker_pdf": "recent.pdf"},
    )
    _insert_link(
        recent_run,
        expires=NOW - timedelta(days=28),
        revoked=True,
        first_download=NOW - timedelta(days=29),
    )

    active_run, _ = _insert_run(
        status="delivered",
        outputs={"sticker_pdf": "active.pdf"},
    )
    _insert_link(
        active_run,
        expires=NOW + timedelta(days=1),
        first_download=NOW - timedelta(days=40),
    )

    redelivered_run, _ = _insert_run(
        status="delivered",
        outputs={"sticker_pdf": "redelivered.pdf"},
    )
    _insert_link(
        redelivered_run,
        expires=NOW - timedelta(days=49),
        revoked=True,
        first_download=NOW - timedelta(days=50),
    )
    _insert_link(
        redelivered_run,
        expires=NOW - timedelta(days=9),
        revoked=True,
        first_download=NOW - timedelta(days=10),
    )

    for status in ("in_progress", "ready"):
        guarded_run, _ = _insert_run(
            status=status,
            outputs={"sticker_pdf": f"{status}.pdf"},
        )
        _insert_link(
            guarded_run,
            expires=NOW - timedelta(days=39),
            revoked=True,
            first_download=NOW - timedelta(days=40),
        )

    connection = connect_db()
    try:
        candidates = select_retention_candidates(connection, now=NOW)
        assert [candidate.run_id for candidate in candidates] == [old_run]
        retired = retire_eligible_runs(connection, now=NOW)
        assert retired.run_ids == (old_run,)
        row = connection.execute(
            "SELECT * FROM runs WHERE run_id = ?",
            (old_run,),
        ).fetchone()
        assert row["status"] == "delivered"
        assert row["vin"] == VIN
        assert row["vehicle_json"]
        assert row["photo_order_json"]
        assert row["artifacts_purged_utc"] == NOW.isoformat()
        assert "sticker_pdf" not in json.loads(row["outputs_json"])
        assert not (old_directory / "old.pdf").exists()
        assert (old_directory / "run_report.json").is_file()
        detail = get_run_detail(connection, _owner_id(), old_run)
        assert detail is not None
        assert detail["artifacts_purged_utc"] == NOW.isoformat()
        assert retire_eligible_runs(connection, now=NOW).runs_retired == 0
    finally:
        connection.close()


def test_retention_commit_survives_unlink_failure_and_retry(monkeypatch) -> None:
    run_id, directory = _insert_run(
        status="delivered",
        outputs={"sticker_pdf": "retained.pdf"},
    )
    _insert_link(
        run_id,
        expires=NOW - timedelta(days=39),
        revoked=True,
        first_download=NOW - timedelta(days=40),
    )

    original_unlink = api.storage._unlink_exact_file

    def fail_unlink(_validated):
        raise PermissionError("simulated")

    monkeypatch.setattr(api.storage, "_unlink_exact_file", fail_unlink)
    connection = connect_db()
    try:
        retired = retire_eligible_runs(connection, now=NOW)
        assert retired.runs_retired == 1
        row = connection.execute(
            "SELECT outputs_json, artifacts_purged_utc FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        assert "sticker_pdf" not in json.loads(row["outputs_json"])
        assert row["artifacts_purged_utc"] == NOW.isoformat()
        assert (directory / "retained.pdf").is_file()
        pending = _job_rows(run_id)[0]
        assert pending["completed_utc"] is None
        assert pending["attempt_count"] == 1

        monkeypatch.setattr(
            api.storage,
            "_unlink_exact_file",
            original_unlink,
        )
        assert process_cleanup_jobs(connection, now=NOW).deleted == 1
        assert not (directory / "retained.pdf").exists()
    finally:
        connection.close()


def test_report_plan_and_apply_counts_are_safe_and_legacy_is_untouched() -> None:
    run_id, directory = _insert_run(
        status="delivered",
        outputs={"sticker_pdf": "current.pdf"},
    )
    current_file = directory / "current.pdf"
    current_file.write_bytes(b"c" * 11)
    (directory / f"{VIN}_01.jpg").write_bytes(b"p" * 3)
    link_id, snapshot = _insert_link(
        run_id,
        expires=NOW,
        first_download=NOW - timedelta(days=40),
        artifact_type="sticker_pdf",
    )
    assert snapshot is not None
    snapshot.write_bytes(b"s" * 7)
    legacy = api.storage.get_settings().runs_dir / "20260101T000000Z"
    legacy.mkdir()
    legacy_file = legacy / "legacy.zip"
    legacy_file.write_bytes(b"l" * 13)

    connection = connect_db()
    try:
        report = storage_report(connection, now=NOW)
        assert report.current_artifact_bytes == 11
        assert report.revoked_snapshot_bytes == 7
        assert report.loose_photo_bytes == 3
        assert report.legacy_bytes == 13
        plan = storage_plan(connection, now=NOW)
        assert [row[0] for row in plan.links_to_expire] == [link_id]
        assert [run.run_id for run in plan.runs_to_retire] == [run_id]
        assert plan.estimated_bytes == 18

        applied = apply_storage_lifecycle(connection, now=NOW)
        assert applied.links_expired == 1
        assert applied.runs_retired == 1
        assert applied.files_deleted == 2
        assert applied.bytes_reclaimed == 18
        assert applied.failures_pending == 0
        assert legacy_file.read_bytes() == b"l" * 13
        assert (directory / f"{VIN}_01.jpg").read_bytes() == b"p" * 3
        second = apply_storage_lifecycle(connection, now=NOW)
        assert second.links_expired == 0
        assert second.runs_retired == 0
        assert second.files_deleted == 0
    finally:
        connection.close()


def test_report_plan_and_no_argument_cli_are_read_only(capsys) -> None:
    run_id, directory = _insert_run(outputs={"sticker_pdf": "current.pdf"})
    before_database = hashlib.sha256(
        api.storage.get_settings().database_path.read_bytes()
    ).digest()
    before_files = {
        path.name: path.read_bytes()
        for path in directory.iterdir()
        if path.is_file()
    }

    assert cleanup_main([]) == 0
    assert "usage:" in capsys.readouterr().out
    assert cleanup_main(["report"]) == 0
    report_output = capsys.readouterr().out
    assert "LotKit storage report (read-only)" in report_output
    assert cleanup_main(["plan"]) == 0
    plan_output = capsys.readouterr().out
    assert "LotKit storage plan (read-only)" in plan_output

    after_database = hashlib.sha256(
        api.storage.get_settings().database_path.read_bytes()
    ).digest()
    after_files = {
        path.name: path.read_bytes()
        for path in directory.iterdir()
        if path.is_file()
    }
    assert after_database == before_database
    assert after_files == before_files
    assert _job_rows(run_id) == []


def test_apply_cli_performs_expiry_retention_and_cleanup(capsys) -> None:
    run_id, directory = _insert_run(
        status="delivered",
        outputs={"sticker_pdf": "current.pdf"},
    )
    _insert_link(
        run_id,
        expires=NOW - timedelta(days=40),
        revoked=True,
        first_download=NOW - timedelta(days=40),
    )
    assert cleanup_main(["apply"]) == 0
    output = capsys.readouterr().out
    assert "Runs retired: 1" in output
    assert "Files deleted: 1" in output
    assert "Failures still pending: 0" in output
    assert not (directory / "current.pdf").exists()


def test_discard_cascades_pending_cleanup_jobs() -> None:
    run_id, directory = _insert_run(status="in_progress")
    (directory / "pending.pdf").write_bytes(b"pending")
    connection = connect_db()
    try:
        enqueue_cleanup_job(
            connection,
            run_id,
            f"{run_id}/pending.pdf",
            "superseded_sticker_pdf",
        )
        connection.commit()
        discard_run(connection, _owner_id(), run_id)
        assert not directory.exists()
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM storage_cleanup_jobs WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()
