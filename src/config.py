from typing import Literal

from models.domain import PlatformConfig
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, SecretStr, ValidationError


class CollectorCredentials(BaseModel):
    """HTTP Basic credentials for a remote TAXII server."""

    user: str
    password: SecretStr = Field(alias="pass")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        env_prefix="stix_hub_",
        env_nested_delimiter="__",
    )
    DATABASE_HOST: str
    DATABASE_PORT: int
    DATABASE_NAME: str
    DATABASE_USER: str
    DATABASE_PASS: str
    DATABASE_USE_NULLPOOL: bool = False
    DATABASE_ENGINE_ECHO: bool = True
    BUCKET_REPO_BACKEND: Literal["memory", "database"] = "database"
    ADMIN_API_KEY: str
    PLATFORM_CONFIG: str = "platform_config.yaml"
    # Base URL collectors use to reach this platform's own REST API.
    PLATFORM_INTERNAL_URL: str = "http://127.0.0.1:8000"
    # Remote TAXII credentials keyed by a collector's `credentials_env`, e.g.
    # STIX_HUB_COLLECTOR_CREDENTIALS__MITRE_TAXII__USER / ...__PASS.
    # Keys are lower-cased by pydantic-settings.
    COLLECTOR_CREDENTIALS: dict[str, CollectorCredentials] = Field(default_factory=dict)


settings = Settings()


def load_platform_config() -> PlatformConfig:
    config_path = Path(settings.PLATFORM_CONFIG)
    try:
        raw = yaml.safe_load(config_path.read_text())
    except FileNotFoundError:
        raise RuntimeError(f"Platform config file not found: {config_path.resolve()}")
    except yaml.YAMLError as exc:
        raise RuntimeError(
            f"Invalid YAML in platform config at {config_path.resolve()}: {exc}"
        )
    try:
        return PlatformConfig.model_validate(raw)
    except ValidationError as exc:
        raise RuntimeError(
            f"Invalid platform config at {config_path.resolve()}:\n{exc}"
        )
