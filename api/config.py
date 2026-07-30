"""Centralized runtime configuration and persistent-storage paths."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENVIRONMENTS = frozenset({"development", "test", "production"})
DEFAULT_TRUSTED_HOSTS = (
    "localhost",
    "127.0.0.1",
    "[::1]",
    "testserver",
)
DEFAULT_ARTIFACT_RETENTION_DAYS = 30
MAX_ARTIFACT_RETENTION_DAYS = 3650


class ConfigurationError(RuntimeError):
    """Raised when runtime configuration is missing or unsafe."""


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str
    project_root: Path
    data_dir: Path
    database_path: Path
    runs_dir: Path
    dealership_logos_dir: Path
    public_base_url: str
    trusted_hosts: tuple[str, ...]
    docs_enabled: bool
    artifact_retention_days: int
    port: int

    @property
    def public_base_host(self) -> str:
        """Return only the non-sensitive host portion for safe logging."""

        return urlsplit(self.public_base_url).netloc

    @property
    def public_origin(self) -> str:
        """Return the exact configured origin used for CSRF validation."""

        parsed = urlsplit(self.public_base_url)
        return f"{parsed.scheme}://{parsed.netloc}"


def _parse_boolean(name: str, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be a boolean.")


def _canonical_data_dir(
    project_root: Path,
    environment: str,
    raw_value: str | None,
) -> Path:
    if environment == "production":
        if raw_value is None or not raw_value.strip():
            raise ConfigurationError(
                "Production requires an absolute LOTKIT_DATA_DIR."
            )
        candidate = Path(raw_value.strip())
        if not candidate.is_absolute():
            raise ConfigurationError(
                "Production requires an absolute LOTKIT_DATA_DIR."
            )
        return candidate.resolve()

    if raw_value is None or not raw_value.strip():
        return project_root

    candidate = Path(raw_value.strip()).expanduser()
    if not candidate.is_absolute():
        candidate = project_root / candidate
    return candidate.resolve()


def _public_base_url(
    environment: str,
    raw_value: str | None,
) -> str:
    if raw_value is None or not raw_value.strip():
        if environment == "production":
            raise ConfigurationError(
                "Production requires an HTTPS LOTKIT_PUBLIC_BASE_URL."
            )
        value = "http://127.0.0.1:8000"
    else:
        value = raw_value.strip().rstrip("/")

    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path.rstrip("/")
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigurationError("LOTKIT_PUBLIC_BASE_URL is invalid.")
    if environment == "production" and parsed.scheme != "https":
        raise ConfigurationError(
            "Production requires an HTTPS LOTKIT_PUBLIC_BASE_URL."
        )
    return f"{parsed.scheme}://{parsed.netloc}"


def _trusted_hosts(
    environment: str,
    raw_value: str | None,
) -> tuple[str, ...]:
    configured_hosts = (
        ()
        if raw_value is None
        else tuple(
            dict.fromkeys(
                host.strip() for host in raw_value.split(",") if host.strip()
            )
        )
    )
    hosts = (
        configured_hosts
        if configured_hosts or environment == "production"
        else DEFAULT_TRUSTED_HOSTS
    )

    if environment == "production" and (not hosts or "*" in hosts):
        raise ConfigurationError(
            "Production requires explicit LOTKIT_TRUSTED_HOSTS."
        )
    return hosts


def _port(raw_value: str | None) -> int:
    if raw_value is None or not raw_value.strip():
        return 8000
    try:
        port = int(raw_value)
    except ValueError as exc:
        raise ConfigurationError("PORT must be a valid TCP port.") from exc
    if not 1 <= port <= 65535:
        raise ConfigurationError("PORT must be a valid TCP port.")
    return port


def _artifact_retention_days(raw_value: str | None) -> int:
    if raw_value is None or not raw_value.strip():
        return DEFAULT_ARTIFACT_RETENTION_DAYS
    try:
        days = int(raw_value)
    except ValueError as exc:
        raise ConfigurationError(
            "LOTKIT_ARTIFACT_RETENTION_DAYS must be a positive integer."
        ) from exc
    if not 1 <= days <= MAX_ARTIFACT_RETENTION_DAYS:
        raise ConfigurationError(
            "LOTKIT_ARTIFACT_RETENTION_DAYS must be between 1 and "
            f"{MAX_ARTIFACT_RETENTION_DAYS}."
        )
    return days


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Build validated settings from an environment mapping."""

    values = os.environ if environ is None else environ
    environment = values.get("LOTKIT_ENV", "development").strip().lower()
    if environment not in ENVIRONMENTS:
        raise ConfigurationError(
            "LOTKIT_ENV must be development, test, or production."
        )

    project_root = PROJECT_ROOT
    data_dir = _canonical_data_dir(
        project_root,
        environment,
        values.get("LOTKIT_DATA_DIR"),
    )
    public_base_url = _public_base_url(
        environment,
        values.get("LOTKIT_PUBLIC_BASE_URL"),
    )
    trusted_hosts = _trusted_hosts(
        environment,
        values.get("LOTKIT_TRUSTED_HOSTS"),
    )

    docs_override = values.get("LOTKIT_DOCS_ENABLED")
    docs_enabled = (
        environment != "production"
        if docs_override is None
        else _parse_boolean("LOTKIT_DOCS_ENABLED", docs_override)
    )

    database_path = (data_dir / "lotkit.db").resolve()
    runs_dir = (data_dir / "runs").resolve()
    dealership_logos_dir = (data_dir / "storage" / "logos").resolve()
    if any(
        not path.is_relative_to(data_dir)
        for path in (database_path, runs_dir, dealership_logos_dir)
    ):
        raise ConfigurationError(
            "Persistent paths must remain beneath LOTKIT_DATA_DIR."
        )

    return Settings(
        environment=environment,
        project_root=project_root,
        data_dir=data_dir,
        database_path=database_path,
        runs_dir=runs_dir,
        dealership_logos_dir=dealership_logos_dir,
        public_base_url=public_base_url,
        trusted_hosts=trusted_hosts,
        docs_enabled=docs_enabled,
        artifact_retention_days=_artifact_retention_days(
            values.get("LOTKIT_ARTIFACT_RETENTION_DAYS")
        ),
        port=_port(values.get("PORT")),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide validated settings object."""

    return load_settings()


def reset_settings_cache() -> None:
    """Clear cached settings so tests can safely apply environment overrides."""

    get_settings.cache_clear()


def _mkdir_private(directory: Path) -> None:
    missing: list[Path] = []
    candidate = directory
    while not candidate.exists():
        missing.append(candidate)
        candidate = candidate.parent

    for path in reversed(missing):
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass

    if not directory.is_dir():
        raise NotADirectoryError(
            "Configured persistent path is not a directory."
        )


def ensure_persistent_directories(
    settings: Settings | None = None,
) -> None:
    """Create only the canonical persistent directories, with private modes."""

    configured = settings or get_settings()
    data_dir = configured.data_dir.resolve()
    children = (
        configured.runs_dir.resolve(),
        configured.dealership_logos_dir.resolve(),
    )
    if any(not child.is_relative_to(data_dir) for child in children):
        raise ConfigurationError(
            "Persistent child paths must remain beneath LOTKIT_DATA_DIR."
        )

    _mkdir_private(data_dir)
    for directory in children:
        _mkdir_private(directory)
