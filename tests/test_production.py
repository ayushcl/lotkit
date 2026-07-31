import logging
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api.config
import api.main
from api.main import create_app
from api.runs import InvalidRunDataError, safe_run_directory

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _settings_for(
    monkeypatch: pytest.MonkeyPatch,
    data_root: Path,
    *,
    environment: str,
) -> api.config.Settings:
    monkeypatch.setenv("LOTKIT_ENV", environment)
    monkeypatch.setenv("LOTKIT_DATA_DIR", str(data_root))
    monkeypatch.setenv(
        "LOTKIT_PUBLIC_BASE_URL",
        (
            "https://lotkit.example"
            if environment == "production"
            else "http://127.0.0.1:8000"
        ),
    )
    monkeypatch.setenv(
        "LOTKIT_TRUSTED_HOSTS",
        "testserver,localhost,127.0.0.1",
    )
    monkeypatch.delenv("LOTKIT_DOCS_ENABLED", raising=False)
    api.config.reset_settings_cache()
    return api.config.get_settings()


def test_production_disables_docs_redoc_and_openapi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_for(
        monkeypatch,
        tmp_path / "production",
        environment="production",
    )

    with TestClient(create_app(settings)) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        assert client.get("/openapi.json").status_code == 404


def test_app_factory_rejects_a_noncanonical_settings_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _settings_for(
        monkeypatch,
        tmp_path / "canonical",
        environment="test",
    )
    other = api.config.load_settings(
        {
            "LOTKIT_ENV": "test",
            "LOTKIT_DATA_DIR": str(tmp_path / "other"),
        }
    )

    with pytest.raises(
        api.config.ConfigurationError,
        match="process-wide cached Settings",
    ):
        create_app(other)


def test_development_keeps_documentation_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_for(
        monkeypatch,
        tmp_path / "development",
        environment="development",
    )

    with TestClient(create_app(settings)) as client:
        assert client.get("/docs").status_code == 200
        assert client.get("/openapi.json").status_code == 200


def test_startup_logs_only_the_safe_configuration_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = _settings_for(
        monkeypatch,
        tmp_path / "production",
        environment="production",
    )
    monkeypatch.setenv("STRIPE_SECRET_KEY", "must-not-appear")
    monkeypatch.setenv("LOTKIT_SESSION_SECRET", "also-must-not-appear")
    caplog.set_level(logging.INFO, logger="uvicorn.error")

    with TestClient(create_app(settings)):
        pass

    summary = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("LotKit startup configuration:")
    )
    assert "environment=production" in summary
    assert f"data_dir={settings.data_dir}" in summary
    assert f"database_path={settings.database_path}" in summary
    assert "public_base_host=lotkit.example" in summary
    assert "docs_enabled=False" in summary
    assert "must-not-appear" not in summary


def test_health_is_lightweight_and_does_not_require_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_for(
        monkeypatch,
        tmp_path / "data",
        environment="test",
    )

    with TestClient(create_app(settings)) as client:
        settings.database_path.unlink()
        settings.database_path.mkdir()
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "service": "lotkit"}


def test_ready_reports_healthy_temporary_database_and_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_for(
        monkeypatch,
        tmp_path / "persistent",
        environment="test",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "service": "lotkit",
        "ready": True,
    }
    assert settings.database_path.is_file()
    assert settings.runs_dir.is_dir()
    assert settings.dealership_logos_dir.is_dir()


def test_ready_returns_generic_503_when_database_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_for(
        monkeypatch,
        tmp_path / "persistent",
        environment="test",
    )

    with TestClient(create_app(settings)) as client:
        settings.database_path.unlink()
        settings.database_path.mkdir()
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "service": "lotkit",
        "ready": False,
    }
    body = response.text
    assert str(settings.data_dir) not in body
    assert "unable to open database" not in body.lower()
    assert "operationalerror" not in body.lower()
    assert "traceback" not in body.lower()


def test_ready_returns_503_when_required_schema_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_for(
        monkeypatch,
        tmp_path / "persistent",
        environment="test",
    )

    with TestClient(create_app(settings)) as client:
        settings.database_path.unlink()
        sqlite3.connect(settings.database_path).close()
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "service": "lotkit",
        "ready": False,
    }
    assert str(settings.database_path) not in response.text


def test_ready_returns_generic_503_when_storage_is_unwritable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_for(
        monkeypatch,
        tmp_path / "persistent",
        environment="test",
    )

    def deny_storage_probe(*_args, **_kwargs):
        raise PermissionError(f"{settings.data_dir}: permission denied")

    with TestClient(create_app(settings)) as client:
        monkeypatch.setattr(api.main, "mkstemp", deny_storage_probe)
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "service": "lotkit",
        "ready": False,
    }
    assert str(settings.data_dir) not in response.text
    assert "permission denied" not in response.text.lower()


@pytest.mark.parametrize("missing_path_name", ["runs", "logos"])
def test_ready_returns_generic_503_when_required_storage_is_missing(
    missing_path_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_for(
        monkeypatch,
        tmp_path / "persistent",
        environment="test",
    )

    with TestClient(create_app(settings)) as client:
        target = (
            settings.runs_dir
            if missing_path_name == "runs"
            else settings.dealership_logos_dir
        )
        shutil.rmtree(target)
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "service": "lotkit",
        "ready": False,
    }
    assert str(target) not in response.text


def test_trusted_hosts_reject_an_unconfigured_host_in_production(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_for(
        monkeypatch,
        tmp_path / "production",
        environment="production",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get(
            "/health",
            headers={"host": "attacker.example"},
        )

    assert response.status_code == 400


def test_container_proxy_trust_is_explicit_and_loopback_by_default() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "--proxy-headers" in dockerfile
    assert "--no-access-log" not in dockerfile
    assert '--forwarded-allow-ips=\\"*\\"' not in dockerfile
    assert (
        '--forwarded-allow-ips=\\"${LOTKIT_FORWARDED_ALLOW_IPS:-127.0.0.1}\\"'
        in dockerfile
    )


def test_render_requires_manual_direct_proxy_trust_configuration() -> None:
    blueprint = (PROJECT_ROOT / "render.yaml").read_text(encoding="utf-8")

    declaration = "- key: LOTKIT_FORWARDED_ALLOW_IPS\n        sync: false"
    assert declaration in blueprint
    assert "Direct proxy peers Uvicorn may trust" in blueprint
    assert "LOTKIT_FORWARDED_ALLOW_IPS\n        value: \"*\"" not in blueprint


def test_run_traversal_protection_uses_the_configured_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings_for(
        monkeypatch,
        tmp_path / "persistent",
        environment="test",
    )

    with pytest.raises(InvalidRunDataError):
        safe_run_directory("../../outside", settings.runs_dir)

    valid_id = "57f652bf-67d8-4a94-b1de-2724d0bc0be4"
    safe_path = safe_run_directory(valid_id, settings.runs_dir)
    assert safe_path.parent == settings.runs_dir.resolve()
    assert safe_path.is_relative_to(settings.data_dir)
