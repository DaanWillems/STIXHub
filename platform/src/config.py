from typing import Literal

from models.domain import PlatformConfig
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

import yaml
from pydantic import ValidationError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_ignore_empty=True, extra="ignore", env_prefix="stix_hub_"
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