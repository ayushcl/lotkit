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
        "LOTKIT_ARTIFACT_RETENTION_DAYS",
        "RENDER",
        "RENDER_EXTERNAL_URL",
        "RENDER_EXTERNAL_HOSTNAME",
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
    assert settings.artifact_retention_days == 30
    assert settings.port == 8000


@pytest.mark.parametrize("value", ["0", "-1", "3651", "one month", "1.5"])
def test_artifact_retention_days_must_be_a_positive_bounded_integer(
    value: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOTKIT_ENV", "test")
    monkeypatch.setenv("LOTKIT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOTKIT_ARTIFACT_RETENTION_DAYS", value)

    with pytest.raises(
        api.config.ConfigurationError,
        match="LOTKIT_ARTIFACT_RETENTION_DAYS",
    ):
        _reload_settings()


def test_artifact_retention_days_can_be_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOTKIT_ENV", "test")
    monkeypatch.setenv("LOTKIT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOTKIT_ARTIFACT_RETENTION_DAYS", "45")

    assert _reload_settings().artifact_retention_days == 45


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


def test_explicit_public_url_overrides_render_external_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOTKIT_ENV", "production")
    monkeypatch.setenv("LOTKIT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", "not-a-valid-url")
    monkeypatch.setenv(
        "RENDER_EXTERNAL_HOSTNAME",
        "render-fallback.onrender.com",
    )
    monkeypatch.setenv(
        "LOTKIT_PUBLIC_BASE_URL",
        "https://explicit.example",
    )
    monkeypatch.setenv("LOTKIT_TRUSTED_HOSTS", "explicit.example")

    settings = _reload_settings()

    assert settings.public_base_url == "https://explicit.example"


def test_explicit_trusted_hosts_override_render_external_hostname(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOTKIT_ENV", "production")
    monkeypatch.setenv("LOTKIT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv(
        "RENDER_EXTERNAL_URL",
        "https://render-fallback.onrender.com",
    )
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "malformed/hostname")
    monkeypatch.delenv("LOTKIT_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("LOTKIT_TRUSTED_HOSTS", "explicit.example")

    settings = _reload_settings()

    assert settings.trusted_hosts == ("explicit.example",)


def test_valid_render_production_fallback_resolves_url_and_trusted_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOTKIT_ENV", "production")
    monkeypatch.setenv("LOTKIT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv(
        "RENDER_EXTERNAL_URL",
        "https://Example-LotKit.onrender.com/",
    )
    monkeypatch.setenv(
        "RENDER_EXTERNAL_HOSTNAME",
        "Example-LotKit.onrender.com",
    )
    monkeypatch.setenv("LOTKIT_PUBLIC_BASE_URL", "   ")
    monkeypatch.setenv("LOTKIT_TRUSTED_HOSTS", " , \t")

    settings = _reload_settings()

    assert settings.public_base_url == "https://Example-LotKit.onrender.com"
    assert settings.trusted_hosts == ("example-lotkit.onrender.com",)


@pytest.mark.parametrize(
    "render_url",
    [
        "http://example-lotkit.onrender.com",
        "example-lotkit.onrender.com",
        "https://user@example-lotkit.onrender.com",
        "https://example-lotkit.onrender.com/path",
    ],
)
def test_malformed_render_external_url_fails_production_startup(
    render_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOTKIT_ENV", "production")
    monkeypatch.setenv("LOTKIT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("RENDER_EXTERNAL_URL", render_url)
    monkeypatch.setenv(
        "RENDER_EXTERNAL_HOSTNAME",
        "example-lotkit.onrender.com",
    )
    monkeypatch.delenv("LOTKIT_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("LOTKIT_TRUSTED_HOSTS", raising=False)

    with pytest.raises(
        api.config.ConfigurationError,
        match="RENDER_EXTERNAL_URL",
    ):
        _reload_settings()


@pytest.mark.parametrize(
    "render_hostname",
    [
        "*",
        "https://example-lotkit.onrender.com",
        "example-lotkit.onrender.com/path",
        "example-lotkit.onrender.com:443",
        "example-lotkit..onrender.com",
        "-example-lotkit.onrender.com",
    ],
)
def test_malformed_render_external_hostname_fails_production_startup(
    render_hostname: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOTKIT_ENV", "production")
    monkeypatch.setenv("LOTKIT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv(
        "RENDER_EXTERNAL_URL",
        "https://example-lotkit.onrender.com",
    )
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", render_hostname)
    monkeypatch.delenv("LOTKIT_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("LOTKIT_TRUSTED_HOSTS", raising=False)

    with pytest.raises(
        api.config.ConfigurationError,
        match="RENDER_EXTERNAL_HOSTNAME",
    ):
        _reload_settings()


@pytest.mark.parametrize(
    ("missing_name", "expected_error"),
    [
        ("RENDER_EXTERNAL_URL", "RENDER_EXTERNAL_URL"),
        ("RENDER_EXTERNAL_HOSTNAME", "RENDER_EXTERNAL_HOSTNAME"),
    ],
)
def test_missing_render_fallback_value_fails_production_startup(
    missing_name: str,
    expected_error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOTKIT_ENV", "production")
    monkeypatch.setenv("LOTKIT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv(
        "RENDER_EXTERNAL_URL",
        "https://example-lotkit.onrender.com",
    )
    monkeypatch.setenv(
        "RENDER_EXTERNAL_HOSTNAME",
        "example-lotkit.onrender.com",
    )
    monkeypatch.delenv("LOTKIT_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("LOTKIT_TRUSTED_HOSTS", raising=False)
    monkeypatch.delenv(missing_name, raising=False)

    with pytest.raises(
        api.config.ConfigurationError,
        match=expected_error,
    ):
        _reload_settings()


@pytest.mark.parametrize("render_indicator", [None, "1", "yes", "false"])
def test_render_external_values_are_not_fallbacks_without_exact_indicator(
    render_indicator: str | None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOTKIT_ENV", "production")
    monkeypatch.setenv("LOTKIT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(
        "RENDER_EXTERNAL_URL",
        "https://example-lotkit.onrender.com",
    )
    monkeypatch.setenv(
        "RENDER_EXTERNAL_HOSTNAME",
        "example-lotkit.onrender.com",
    )
    monkeypatch.delenv("LOTKIT_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("LOTKIT_TRUSTED_HOSTS", raising=False)
    if render_indicator is None:
        monkeypatch.delenv("RENDER", raising=False)
    else:
        monkeypatch.setenv("RENDER", render_indicator)

    with pytest.raises(
        api.config.ConfigurationError,
        match="LOTKIT_PUBLIC_BASE_URL",
    ):
        _reload_settings()


def test_development_ignores_render_external_values_without_indicator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_lotkit_environment(monkeypatch)
    monkeypatch.setenv(
        "RENDER_EXTERNAL_URL",
        "https://example-lotkit.onrender.com",
    )
    monkeypatch.setenv(
        "RENDER_EXTERNAL_HOSTNAME",
        "example-lotkit.onrender.com",
    )

    settings = _reload_settings()

    assert settings.environment == "development"
    assert settings.public_base_url == "http://127.0.0.1:8000"
    assert "localhost" in settings.trusted_hosts


def test_render_hostname_is_not_a_trusted_host_without_exact_indicator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOTKIT_ENV", "production")
    monkeypatch.setenv("LOTKIT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LOTKIT_PUBLIC_BASE_URL", "https://explicit.example")
    monkeypatch.delenv("LOTKIT_TRUSTED_HOSTS", raising=False)
    monkeypatch.setenv("RENDER", "yes")
    monkeypatch.setenv(
        "RENDER_EXTERNAL_HOSTNAME",
        "example-lotkit.onrender.com",
    )

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


def test_public_base_url_is_an_origin_without_a_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOTKIT_ENV", "test")
    monkeypatch.setenv("LOTKIT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(
        "LOTKIT_PUBLIC_BASE_URL",
        "https://lotkit.example/application",
    )

    with pytest.raises(
        api.config.ConfigurationError,
        match="LOTKIT_PUBLIC_BASE_URL",
    ):
        _reload_settings()


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
