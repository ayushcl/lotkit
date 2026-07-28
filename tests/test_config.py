import hashlib
from pathlib import Path

import pytest

import api.config


def _reload_settings() -> api.config.Settings:
    api.config.reset_settings_cache()
    return api.config.get_settings()


def _clear_lotkit_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "LOTKIT_ENV",
        "LOTKIT_DATA_DIR",
        "LOTKIT_PUBLIC_BASE_URL",
        "LOTKIT_TRUSTED_HOSTS",
        "LOTKIT_DOCS_ENABLED",
        "PORT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_development_defaults_preserve_existing_local_layout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_lotkit_environment(monkeypatch)

    settings = _reload_settings()
    project_root = Path(api.config.__file__).resolve().parent.parent

    assert settings.environment == "development"
    assert settings.project_root == project_root
    assert settings.data_dir == project_root
    assert settings.database_path == project_root / "lotkit.db"
    assert settings.runs_dir == project_root / "runs"
    assert settings.dealership_logos_dir == project_root / "storage" / "logos"
    assert settings.public_base_url == "http://127.0.0.1:8000"
    assert {"localhost", "127.0.0.1"} <= set(settings.trusted_hosts)
    assert settings.docs_enabled is True
    assert settings.port == 8000


def test_paths_are_absolute_and_do_not_depend_on_process_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_lotkit_environment(monkeypatch)
    expected_root = Path(api.config.__file__).resolve().parent.parent
    monkeypatch.chdir(tmp_path)

    settings = _reload_settings()

    assert settings.project_root == expected_root
    for path in (
        settings.project_root,
        settings.data_dir,
        settings.database_path,
        settings.runs_dir,
        settings.dealership_logos_dir,
    ):
        assert path.is_absolute()


def test_data_dir_redirects_every_persistent_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_root = tmp_path / "persistent-data"
    monkeypatch.setenv("LOTKIT_ENV", "test")
    monkeypatch.setenv("LOTKIT_DATA_DIR", str(data_root))

    settings = _reload_settings()
    canonical_root = data_root.resolve()

    assert settings.data_dir == canonical_root
    assert settings.database_path == canonical_root / "lotkit.db"
    assert settings.runs_dir == canonical_root / "runs"
    assert (
        settings.dealership_logos_dir
        == canonical_root / "storage" / "logos"
    )
    for path in (
        settings.database_path,
        settings.runs_dir,
        settings.dealership_logos_dir,
    ):
        assert path.is_relative_to(canonical_root)


@pytest.mark.parametrize("value", [None, "relative/data"])
def test_production_rejects_missing_or_relative_data_directory(
    value: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOTKIT_ENV", "production")
    monkeypatch.setenv(
        "LOTKIT_PUBLIC_BASE_URL",
        "https://lotkit.example",
    )
    monkeypatch.setenv("LOTKIT_TRUSTED_HOSTS", "lotkit.example")
    if value is None:
        monkeypatch.delenv("LOTKIT_DATA_DIR", raising=False)
    else:
        monkeypatch.setenv("LOTKIT_DATA_DIR", value)

    with pytest.raises(api.config.ConfigurationError, match="LOTKIT_DATA_DIR"):
        _reload_settings()


@pytest.mark.parametrize(
    "public_url",
    [None, "", "http://lotkit.example", "lotkit.example"],
)
def test_production_requires_an_explicit_https_public_url(
    public_url: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOTKIT_ENV", "production")
    monkeypatch.setenv("LOTKIT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOTKIT_TRUSTED_HOSTS", "lotkit.example")
    if public_url is None:
        monkeypatch.delenv("LOTKIT_PUBLIC_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("LOTKIT_PUBLIC_BASE_URL", public_url)

    with pytest.raises(
        api.config.ConfigurationError,
        match="LOTKIT_PUBLIC_BASE_URL",
    ):
        _reload_settings()


@pytest.mark.parametrize("hosts", [None, "", " , "])
def test_production_requires_trusted_hosts(
    hosts: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOTKIT_ENV", "production")
    monkeypatch.setenv("LOTKIT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(
        "LOTKIT_PUBLIC_BASE_URL",
        "https://lotkit.example",
    )
    if hosts is None:
        monkeypatch.delenv("LOTKIT_TRUSTED_HOSTS", raising=False)
    else:
        monkeypatch.setenv("LOTKIT_TRUSTED_HOSTS", hosts)

    with pytest.raises(
        api.config.ConfigurationError,
        match="LOTKIT_TRUSTED_HOSTS",
    ):
        _reload_settings()


def test_production_does_not_accept_a_wildcard_trusted_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOTKIT_ENV", "production")
    monkeypatch.setenv("LOTKIT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(
        "LOTKIT_PUBLIC_BASE_URL",
        "https://lotkit.example",
    )
    monkeypatch.setenv("LOTKIT_TRUSTED_HOSTS", "*")

    with pytest.raises(
        api.config.ConfigurationError,
        match="LOTKIT_TRUSTED_HOSTS",
    ):
        _reload_settings()


def test_public_base_url_has_a_consistent_trailing_slash_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOTKIT_ENV", "test")
    monkeypatch.setenv("LOTKIT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(
        "LOTKIT_PUBLIC_BASE_URL",
        "https://lotkit.example///",
    )

    assert _reload_settings().public_base_url == "https://lotkit.example"


def test_settings_cache_can_be_reset_without_environment_leaks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    monkeypatch.setenv("LOTKIT_ENV", "test")
    monkeypatch.setenv("LOTKIT_DATA_DIR", str(first_root))
    first = _reload_settings()

    monkeypatch.setenv("LOTKIT_DATA_DIR", str(second_root))
    assert api.config.get_settings() is first

    second = _reload_settings()
    assert second is not first
    assert second.data_dir == second_root.resolve()


def test_loading_development_settings_does_not_modify_local_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_lotkit_environment(monkeypatch)
    project_root = Path(api.config.__file__).resolve().parent.parent
    local_database = project_root / "lotkit.db"
    before = (
        (
            local_database.stat().st_mtime_ns,
            local_database.stat().st_size,
            hashlib.sha256(local_database.read_bytes()).digest(),
        )
        if local_database.is_file()
        else None
    )

    settings = _reload_settings()

    after = (
        (
            local_database.stat().st_mtime_ns,
            local_database.stat().st_size,
            hashlib.sha256(local_database.read_bytes()).digest(),
        )
        if local_database.is_file()
        else None
    )
    assert settings.database_path == local_database
    assert after == before
