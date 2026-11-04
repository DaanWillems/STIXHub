from collections.abc import Mapping, Sequence

from collector_auth import secret_key
from config import CollectorCredentials, MispCredentials, settings
from models.domain import (
    BucketConfig,
    CollectorConfig,
    MispConnectorConfig,
    TaxiiCollectorConfig,
)


def _validate_misp_connector(
    connector: MispConnectorConfig, known_buckets: set[str]
) -> None:
    name = connector.name
    if connector.pull is None and connector.push is None:
        raise RuntimeError(
            f"MISP connector '{name}' has neither pull nor push configured"
        )
    buckets: list[str] = []
    if connector.pull is not None:
        buckets.append(connector.pull.target_bucket)
    if connector.push is not None:
        buckets.append(connector.push.source_bucket)
    for bucket in buckets:
        if bucket not in known_buckets:
            raise RuntimeError(
                f"MISP connector '{name}' references unknown bucket '{bucket}'"
            )
    if (
        connector.pull is not None
        and connector.push is not None
        and connector.pull.target_bucket == connector.push.source_bucket
    ):
        raise RuntimeError(
            f"MISP connector '{name}' pulls into and pushes from the same bucket "
            f"'{connector.pull.target_bucket}'"
        )
    if (
        connector.push is not None
        and connector.push.distribution == 4
        and connector.push.sharing_group_id is None
    ):
        raise RuntimeError(
            f"MISP connector '{name}': distribution 4 requires sharing_group_id"
        )


def validate_collectors(
    bucket_configs: list[BucketConfig],
    collector_configs: Sequence[CollectorConfig],
) -> None:
    """Fail fast on collector config errors: unknown buckets, duplicate names
    and inconsistent MISP connectors."""
    known_buckets = {bucket.name for bucket in bucket_configs}
    seen_keys: set[str] = set()
    for collector in collector_configs:
        # Names that only differ in case or "-" vs "_" share a secret key.
        if secret_key(collector.name) in seen_keys:
            raise RuntimeError(f"Duplicate collector name '{collector.name}'")
        seen_keys.add(secret_key(collector.name))
        if isinstance(collector, MispConnectorConfig):
            _validate_misp_connector(collector, known_buckets)
        elif collector.target_bucket not in known_buckets:
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


def load_misp_api_key(
    config: MispConnectorConfig,
    credentials: Mapping[str, MispCredentials] | None = None,
) -> str:
    """Look up the API key for ``config.credentials_env``.

    Keys come from ``Settings.MISP_CREDENTIALS`` (env var
    ``STIX_HUB_MISP_CREDENTIALS__<credentials_env>__API_KEY``).
    """
    available = settings.MISP_CREDENTIALS if credentials is None else credentials
    found = available.get(config.credentials_env.lower())
    if found is None:
        raise RuntimeError(
            f"MISP connector '{config.name}' has no API key configured; set "
            f"STIX_HUB_MISP_CREDENTIALS__{config.credentials_env.upper()}__API_KEY"
        )
    return found.api_key.get_secret_value()
