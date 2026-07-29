from pathlib import Path

import pytest

import api.config
import api.db
import api.main
from api.auth import current_owner_id, get_or_create_default_owner


@pytest.fixture(autouse=True)
def isolated_persistence(tmp_path_factory, monkeypatch):
    persistence_root: Path = tmp_path_factory.mktemp("lotkit-persistence")
    monkeypatch.setenv("LOTKIT_ENV", "test")
    monkeypatch.setenv("LOTKIT_DATA_DIR", str(persistence_root))
    monkeypatch.setenv(
        "LOTKIT_PUBLIC_BASE_URL",
        "http://127.0.0.1:8000",
    )
    monkeypatch.setenv(
        "LOTKIT_TRUSTED_HOSTS",
        "testserver,localhost,127.0.0.1",
    )
    api.config.reset_settings_cache()
    settings = api.config.get_settings()

    # Existing endpoint tests reference this compatibility alias directly.
    monkeypatch.setattr(api.main, "RUNS_ROOT", settings.runs_dir)
    api.db.init_db()
    connection = api.db.connect_db()
    try:
        owner_id = get_or_create_default_owner(connection)
    finally:
        connection.close()
    api.main.app.dependency_overrides[current_owner_id] = lambda: owner_id

    yield {
        "database_path": settings.database_path,
        "logo_root": settings.dealership_logos_dir,
        "owner_id": owner_id,
        "runs_root": settings.runs_dir,
        "settings": settings,
    }

    api.main.app.dependency_overrides.pop(current_owner_id, None)
    api.config.reset_settings_cache()
