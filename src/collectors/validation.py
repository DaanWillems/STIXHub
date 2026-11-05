import logging
from collections.abc import Mapping

from pydantic import SecretStr

from config import CollectorCredentials, settings
from models.domain import (
    BaseCollectorConfig,
    BucketConfig,
    BucketMode,
    CollectorConfig,
    MispCollectorConfig,
    TaxiiCollectorConfig,
)

logger = logging.getLogger(__name__)


def validate_collectors(
    bucket_configs: list[BucketConfig],
    collector_configs: list[CollectorConfig],
) -> None:
    """Fail fast on collector config errors: unknown buckets or duplicate names."""
    known_buckets = {bucket.name for bucket in bucket_configs}
    seen_names: set[str] = set()
    for collector in collector_configs:
        if collector.name in seen_names:
            raise RuntimeError(f"Duplicate collector name '{collector.name}'")
        seen_names.add(collector.name)
        if collector.target_bucket not in known_buckets:
            raise RuntimeError(
                f"Collector '{collector.name}' targets unknown bucket "
                f"'{collector.target_bucket}'; declare it in the buckets section"
            )


def load_credentials(
    config: TaxiiCollectorConfig,
    credentials: Mapping[str, CollectorCredentials] | None = None,
) -> tuple[str, str] | None:
    """Look up HTTP Basic credentials for ``config.credentials_env``.

    Credentials come from ``Settings.COLLECTOR_CREDENTIALS`` (env vars
    ``STIX_HUB_COLLECTOR_CREDENTIALS__<credentials_env>__USER`` / ``__PASS``).
    Returns ``None`` for unauthenticated feeds (no ``credentials_env``).
    """
    if config.credentials_env is None:
        return None
    available = settings.COLLECTOR_CREDENTIALS if credentials is None else credentials
    # pydantic-settings lower-cases the dict keys it reads from the environment.
    found = available.get(config.credentials_env.lower())
    if found is None:
        prefix = f"STIX_HUB_COLLECTOR_CREDENTIALS__{config.credentials_env.upper()}"
        raise RuntimeError(
            f"Collector '{config.name}' has no credentials configured; set "
            f"{prefix}__USER and {prefix}__PASS"
        )
    return found.user, found.password.get_secret_value()


def secret_env_var(config: BaseCollectorConfig) -> str:
    return f"STIX_HUB_COLLECTOR_SECRETS__{config.secret_env.upper()}"


def find_secret(
    config: BaseCollectorConfig,
    secrets: Mapping[str, SecretStr] | None = None,
) -> str | None:
    """Return the shared secret for ``config``, or ``None`` if it is unset/empty.

    Secrets come from ``Settings.COLLECTOR_SECRETS`` (env var
    ``STIX_HUB_COLLECTOR_SECRETS__<secret_env>``).
    """
    available = settings.COLLECTOR_SECRETS if secrets is None else secrets
    # pydantic-settings lower-cases the dict keys it reads from the environment.
    found = available.get(config.secret_env.lower())
    if found is None or not found.get_secret_value():
        return None
    return found.get_secret_value()


def load_secret(
    config: BaseCollectorConfig,
    secrets: Mapping[str, SecretStr] | None = None,
) -> str:
    """Like :func:`find_secret`, but raises if the secret is not configured."""
    secret = find_secret(config, secrets)
    if secret is None:
        raise RuntimeError(
            f"Collector '{config.name}' has no shared secret configured; set "
            f"{secret_env_var(config)}"
        )
    return secret


def validate_collector_secrets(
    collector_configs: list[CollectorConfig],
    secrets: Mapping[str, SecretStr] | None = None,
) -> None:
    """Fail fast unless every collector has its own, non-empty shared secret.

    Secrets identify the collector making a request, so two collectors may not
    share one.
    """
    owners: dict[str, str] = {}
    for config in collector_configs:
        secret = load_secret(config, secrets)
        other = owners.get(secret)
        if other is not None:
            raise RuntimeError(
                f"Collectors '{other}' and '{config.name}' use the same shared "
                "secret; each collector needs its own"
            )
        owners[secret] = config.name


def api_key_env_var(config: MispCollectorConfig) -> str:
    return f"STIX_HUB_COLLECTOR_API_KEYS__{config.api_key_env.upper()}"


def load_api_key(
    config: MispCollectorConfig,
    keys: Mapping[str, SecretStr] | None = None,
) -> str:
    """Return the MISP API key for ``config``, raising if unset or empty.

    Keys come from ``Settings.COLLECTOR_API_KEYS`` (env var
    ``STIX_HUB_COLLECTOR_API_KEYS__<api_key_env>``).
    """
    available = settings.COLLECTOR_API_KEYS if keys is None else keys
    # pydantic-settings lower-cases the dict keys it reads from the environment.
    found = available.get(config.api_key_env.lower())
    if found is None or not found.get_secret_value():
        raise RuntimeError(
            f"Collector '{config.name}' has no MISP API key configured; set "
            f"{api_key_env_var(config)}"
        )
    return found.get_secret_value()


def validate_collector_api_keys(
    collector_configs: list[CollectorConfig],
    keys: Mapping[str, SecretStr] | None = None,
) -> None:
    """Fail fast unless every MISP collector has a non-empty API key."""
    for config in collector_configs:
        if isinstance(config, MispCollectorConfig):
            load_api_key(config, keys)


def warn_misp_append_buckets(
    bucket_configs: list[BucketConfig],
    collector_configs: list[CollectorConfig],
) -> None:
    """Warn about MISP collectors targeting append buckets.

    Republished events are re-submitted in full, which duplicates entities in
    an append bucket; merge mode is recommended.
    """
    modes = {bucket.name: bucket.mode for bucket in bucket_configs}
    for config in collector_configs:
        if (
            isinstance(config, MispCollectorConfig)
            and modes.get(config.target_bucket) == BucketMode.append
        ):
            logger.warning(
                "MISP collector '%s' targets append-mode bucket '%s'; republished "
                "events will create duplicates. Merge mode is recommended.",
                config.name,
                config.target_bucket,
            )
