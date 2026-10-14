import base64
import hashlib

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings

# base64url of 32 deterministic bytes, the shape META_TOKEN_ENCRYPTION_KEY must have.
_VALID_KEY = base64.urlsafe_b64encode(hashlib.sha256(b"config-test-key").digest()).decode()


def test_requires_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_reads_values_from_environment(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/app")
    # Production refuses to boot without these; see the fail-closed tests below.
    monkeypatch.setenv("META_TOKEN_ENCRYPTION_KEY", _VALID_KEY)
    monkeypatch.setenv("META_APP_ID", "1234567890")
    monkeypatch.setenv("META_APP_SECRET", "secret")
    monkeypatch.setenv("META_OAUTH_REDIRECT_URI", "https://app.example.test/cb")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.app_env == "production"
    assert settings.log_level == "WARNING"
    assert settings.is_production is True


# -- Meta configuration ------------------------------------------------------
# base64url of 32 bytes, the shape META_TOKEN_ENCRYPTION_KEY must have.


def _production_env(monkeypatch) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/app")
    monkeypatch.setenv("META_TOKEN_ENCRYPTION_KEY", _VALID_KEY)
    monkeypatch.setenv("META_APP_ID", "1234567890")
    monkeypatch.setenv("META_APP_SECRET", "secret")
    monkeypatch.setenv("META_OAUTH_REDIRECT_URI", "https://app.example.test/cb")


def test_production_refuses_to_boot_without_a_token_encryption_key(monkeypatch):
    """Fail closed: no key must mean no startup, never a plaintext token."""
    _production_env(monkeypatch)
    monkeypatch.delenv("META_TOKEN_ENCRYPTION_KEY")

    with pytest.raises(ValidationError, match="META_TOKEN_ENCRYPTION_KEY"):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize("missing", ["META_APP_ID", "META_APP_SECRET", "META_OAUTH_REDIRECT_URI"])
def test_production_refuses_to_boot_with_incomplete_oauth_config(monkeypatch, missing):
    _production_env(monkeypatch)
    monkeypatch.delenv(missing)

    with pytest.raises(ValidationError, match="required in production"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_development_does_not_require_meta_secrets(monkeypatch):
    """Local development and the test suite must be able to run without a Meta app."""
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/app")
    for name in (
        "META_TOKEN_ENCRYPTION_KEY",
        "META_APP_ID",
        "META_APP_SECRET",
        "META_OAUTH_REDIRECT_URI",
    ):
        monkeypatch.delenv(name, raising=False)

    assert Settings(_env_file=None).meta_token_encryption_key is None  # type: ignore[call-arg]


def test_the_graph_api_version_is_configurable_and_validated(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/app")
    monkeypatch.setenv("META_GRAPH_API_VERSION", "v25.0")

    assert Settings(_env_file=None).meta_graph_api_version == "v25.0"  # type: ignore[call-arg]


@pytest.mark.parametrize("bad", ["26.0", "latest", "", "vv26.0"])
def test_a_malformed_graph_api_version_is_rejected(monkeypatch, bad):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/app")
    monkeypatch.setenv("META_GRAPH_API_VERSION", bad)

    with pytest.raises(ValidationError, match="META_GRAPH_API_VERSION"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_the_default_graph_version_matches_the_verified_current_release():
    """v26.0 was verified against Meta's changelog on 2026-09-28. Bump it deliberately."""
    monkeypatch_free = Settings.model_fields["meta_graph_api_version"].default
    assert monkeypatch_free == "v26.0"


def test_comma_separated_cors_origins(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/app")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000, http://localhost:8080")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.cors_allowed_origins == ["http://localhost:3000", "http://localhost:8080"]


def test_json_cors_origins(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/app")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", '["https://app.example.com"]')

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.cors_allowed_origins == ["https://app.example.com"]


def test_wildcard_cors_origin_is_rejected(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/app")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "*")

    with pytest.raises(ValidationError, match="CORS_ALLOWED_ORIGINS"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_default_cors_origins_are_not_wildcard(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db:5432/app")
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)

    assert Settings(_env_file=None).cors_allowed_origins == []  # type: ignore[call-arg]


def test_get_settings_is_cached():
    assert get_settings() is get_settings()


def test_env_file_is_configured():
    # Secrets must come from the environment / root .env, never from source.
    assert Settings.model_config["env_file"] is not None
