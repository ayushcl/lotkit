from pathlib import Path

import pytest

import api.db
import api.dealerships
import api.main
from api.auth import current_owner_id


@pytest.fixture(autouse=True)
def isolated_persistence(tmp_path_factory, monkeypatch):
    persistence_root: Path = tmp_path_factory.mktemp("lotkit-persistence")
    database_path = persistence_root / "lotkit.db"
    logo_root = persistence_root / "storage" / "logos"

    monkeypatch.setattr(api.db, "DB_PATH", database_path)
    monkeypatch.setattr(api.dealerships, "BASE_DIR", persistence_root)
    monkeypatch.setattr(api.dealerships, "LOGO_ROOT", logo_root)
    monkeypatch.setattr(api.main, "RUNS_ROOT", persistence_root / "runs")
    api.db.init_db()

    yield {
        "database_path": database_path,
        "logo_root": logo_root,
    }

    api.main.app.dependency_overrides.pop(current_owner_id, None)
