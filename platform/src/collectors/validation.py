import os
from collections.abc import Mapping

from models.domain import BucketConfig, CollectorConfig


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
    config: CollectorConfig, environ: Mapping[str, str] | None = None
) -> tuple[str, str] | None:
    """Read HTTP Basic credentials from ``<credentials_env>_USER`` / ``_PASS``.

    Returns ``None`` for unauthenticated feeds (no ``credentials_env``).
    """
    if config.credentials_env is None:
        return None
    env = os.environ if environ is None else environ
    user_var = f"{config.credentials_env}_USER"
    pass_var = f"{config.credentials_env}_PASS"
    missing = [var for var in (user_var, pass_var) if not env.get(var)]
    if missing:
        raise RuntimeError(
            f"Collector '{config.name}' is missing credential environment "
            f"variable(s): {', '.join(missing)}"
        )
    return env[user_var], env[pass_var]
