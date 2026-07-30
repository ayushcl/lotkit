"""Explicit storage reporting, planning, and cleanup CLI."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

from api.config import get_settings
from api.db import connect_db, init_db
from api.storage import (
    StoragePlan,
    StorageReport,
    apply_storage_lifecycle,
    storage_plan,
    storage_report,
)


def _read_only_connection(database_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{database_path.resolve().as_uri()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _measurement(label: str, files: int, size: int) -> str:
    return f"{label}: files={files} bytes={size}"


def format_report(report: StorageReport) -> str:
    lines = [
        "LotKit storage report (read-only)",
        _measurement("Run-root total", report.total_files, report.total_bytes),
        _measurement(
            "Current referenced Run artifacts",
            report.current_artifact_files,
            report.current_artifact_bytes,
        ),
        _measurement(
            "Active delivery snapshots",
            report.active_snapshot_files,
            report.active_snapshot_bytes,
        ),
        _measurement(
            "Revoked/expired delivery snapshots",
            report.revoked_snapshot_files,
            report.revoked_snapshot_bytes,
        ),
        _measurement(
            "Pending cleanup",
            report.pending_cleanup_files,
            report.pending_cleanup_bytes,
        ),
        _measurement(
            "Loose photos in current UUID Runs",
            report.loose_photo_files,
            report.loose_photo_bytes,
        ),
        _measurement(
            "Unmanaged files",
            report.unmanaged_files,
            report.unmanaged_bytes,
        ),
        _measurement(
            "Legacy files",
            report.legacy_files,
            report.legacy_bytes,
        ),
        "Largest current Runs:",
    ]
    if report.largest_runs:
        lines.extend(
            f"  {run_id}: files={files} bytes={size}"
            for run_id, size, files in report.largest_runs
        )
    else:
        lines.append("  none")
    return "\n".join(lines)


def format_plan(plan: StoragePlan) -> str:
    lines = [
        "LotKit storage plan (read-only)",
        f"Links that would be expired: {len(plan.links_to_expire)}",
    ]
    lines.extend(
        f"  link_id={link_id} run_id={run_id} expires_utc={expires_utc}"
        for link_id, run_id, expires_utc in plan.links_to_expire
    )
    lines.append(f"Runs whose artifacts would be retired: {len(plan.runs_to_retire)}")
    lines.extend(
        f"  run_id={candidate.run_id} "
        f"latest_download_utc={candidate.latest_download_utc}"
        for candidate in plan.runs_to_retire
    )
    lines.append(f"Files that would be queued: {len(plan.files_to_queue)}")
    lines.extend(f"  {relative_path}" for relative_path in plan.files_to_queue)
    lines.extend(
        [
            f"Pending jobs that would be attempted: "
            f"{len(plan.pending_job_ids)}",
            f"Estimated existing files: {plan.estimated_files}",
            f"Estimated bytes: {plan.estimated_bytes}",
        ]
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m api.cleanup_storage",
        description=(
            "Measure or explicitly apply LotKit's exact-path storage "
            "lifecycle."
        ),
    )
    subparsers = parser.add_subparsers(
        dest="command",
        metavar="{report,plan,apply}",
    )
    for command, help_text in (
        ("report", "Measure storage without modifying files or SQLite."),
        ("plan", "Preview expiry, retention, and pending cleanup."),
        ("apply", "Apply lifecycle changes and attempt queued deletions."),
    ):
        subparsers.add_parser(
            command,
            help=help_text,
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command is None:
        parser.print_help()
        return 0

    settings = get_settings()
    connection: sqlite3.Connection | None = None
    try:
        if arguments.command in {"report", "plan"}:
            connection = _read_only_connection(settings.database_path)
            if arguments.command == "report":
                print(storage_report_output(connection))
            else:
                print(storage_plan_output(connection))
            return 0

        init_db(settings.database_path)
        connection = connect_db(settings.database_path)
        result = apply_storage_lifecycle(
            connection,
            settings.runs_dir,
            retention_days=settings.artifact_retention_days,
        )
        print("LotKit storage apply")
        print(f"Links expired: {result.links_expired}")
        print(f"Runs retired: {result.runs_retired}")
        print(f"Files deleted: {result.files_deleted}")
        print(f"Files already missing: {result.files_already_missing}")
        print(f"Bytes reclaimed: {result.bytes_reclaimed}")
        print(f"Failures still pending: {result.failures_pending}")
        return 0
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(
            f"cleanup_storage: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1
    finally:
        if connection is not None:
            connection.close()


def storage_report_output(connection: sqlite3.Connection) -> str:
    settings = get_settings()
    return format_report(storage_report(connection, settings.runs_dir))


def storage_plan_output(connection: sqlite3.Connection) -> str:
    settings = get_settings()
    return format_plan(
        storage_plan(
            connection,
            settings.runs_dir,
            retention_days=settings.artifact_retention_days,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
