from pydantic_settings import BaseSettings, SettingsConfigDict


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


settings = Settings()
