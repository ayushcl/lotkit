import json
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image

SUPPORTED_EXTENSIONS = {
    ".jpeg": ".jpg",
    ".jpg": ".jpg",
    ".png": ".png",
    ".webp": ".webp",
}


def safe_ext(filename: str) -> str:
    extension = Path(filename).suffix.lower()
    try:
        return SUPPORTED_EXTENSIONS[extension]
    except KeyError as exc:
        raise ValueError(f"Unsupported image extension: {extension or '(none)'}") from exc


def sequenced_name(vin: str, index: int, ext: str) -> str:
    width = 3 if index > 99 else 2
    return f"{vin}_{index:0{width}d}{ext}"


def _is_valid_image(file_bytes: bytes) -> bool:
    try:
        with Image.open(BytesIO(file_bytes)) as image:
            image.verify()
    except Exception:
        return False
    return True


def build_run(
    vin: str,
    vehicle: dict,
    ordered_files: list[tuple[str, bytes]],
    runs_root: str,
) -> dict:
    created_utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{vin}_{created_utc}"
    root = Path(runs_root)
    run_directory = root / run_id
    run_directory.mkdir(parents=True, exist_ok=False, mode=0o700)

    filenames: list[str] = []
    skipped: list[str] = []

    for original_filename, file_bytes in ordered_files:
        try:
            extension = safe_ext(original_filename)
        except ValueError:
            skipped.append(original_filename)
            continue

        if not _is_valid_image(file_bytes):
            skipped.append(original_filename)
            continue

        new_filename = sequenced_name(vin, len(filenames) + 1, extension)
        photo_path = run_directory / new_filename
        photo_path.write_bytes(file_bytes)
        try:
            photo_path.chmod(0o600)
        except OSError:
            pass
        filenames.append(new_filename)

    zip_filename = f"{vin}_photos.zip"
    zip_file = run_directory / zip_filename
    with ZipFile(zip_file, "w", compression=ZIP_DEFLATED) as archive:
        for filename in filenames:
            archive.write(run_directory / filename, arcname=filename)
    try:
        zip_file.chmod(0o600)
    except OSError:
        pass

    report = {
        "vin": vin,
        "vehicle": vehicle,
        "created_utc": created_utc,
        "photo_count": len(filenames),
        "filenames": filenames,
        "skipped": skipped,
        "zip_filename": zip_filename,
    }
    report_file = run_directory / "run_report.json"
    report_file.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    try:
        report_file.chmod(0o600)
    except OSError:
        pass

    return {
        "run_id": run_id,
        "photo_count": len(filenames),
        "filenames": filenames,
        "skipped": skipped,
        "zip_path": (Path(run_id) / zip_filename).as_posix(),
        "report_path": (Path(run_id) / report_file.name).as_posix(),
    }
