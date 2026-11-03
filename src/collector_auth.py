"""Who may use the internal collector API, and for which bucket.

Every configured collector has a secret it shares with the platform and sends
as a bearer token. A collector may only register under the names its config
gives it, and each registration gets exactly one bucket with one kind of
access: a TAXII collector and a MISP connector's pull side write their target
bucket, a MISP connector's push side reads its source bucket. Nothing else, so
a collector can never read a bucket its config doesn't name.
"""

import hmac
import logging
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum

from pydantic import SecretStr

from models.domain import CollectorConfig, MispConnectorConfig

logger = logging.getLogger(__name__)


class BucketAccess(str, Enum):
    read = "read"
    write = "write"


@dataclass(frozen=True)
class CollectorGrant:
    """What one registration of a collector may do."""

    registration: str  # the name it registers under
    collector: str  # the configured collector it belongs to (owns the secret)
    bucket: str
    access: BucketAccess
    misp_instance: str | None = None  # MISP connectors: the normalised MISP URL


class CollectorAuthError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def push_registration(connector_name: str) -> str:
    """The name a MISP connector's push side registers under."""
    return f"{connector_name}:push"


def secret_key(collector_name: str) -> str:
    """The ``COLLECTOR_SECRETS`` key of a collector (env var names have no ``-``)."""
    return collector_name.lower().replace("-", "_")


def collector_grants(configs: Sequence[CollectorConfig]) -> dict[str, CollectorGrant]:
    grants: list[CollectorGrant] = []
    for config in configs:
        if not isinstance(config, MispConnectorConfig):
            grants.append(
                CollectorGrant(
                    config.name, config.name, config.target_bucket, BucketAccess.write
                )
            )
            continue
        instance = config.normalized_url
        if config.pull is not None:
            grants.append(
                CollectorGrant(
                    config.name,
                    config.name,
                    config.pull.target_bucket,
                    BucketAccess.write,
                    instance,
                )
            )
        if config.push is not None:
            grants.append(
                CollectorGrant(
                    push_registration(config.name),
                    config.name,
                    config.push.source_bucket,
                    BucketAccess.read,
                    instance,
                )
            )
    return {grant.registration: grant for grant in grants}


def resolve_collector_secrets(
    configs: Sequence[CollectorConfig], configured: Mapping[str, SecretStr]
) -> dict[str, str]:
    """Each collector's secret, keyed by collector name.

    Secrets come from ``STIX_HUB_COLLECTOR_SECRETS__<NAME>`` (``-`` in the name
    becomes ``_``). A collector without one gets a random secret for this run,
    which is enough while collectors run inside the server process; a
    collector running elsewhere needs the configured one.
    """
    resolved: dict[str, str] = {}
    for config in configs:
        found = configured.get(secret_key(config.name))
        if found is None:
            logger.info(
                "No secret configured for collector '%s'; using a random one",
                config.name,
            )
            resolved[config.name] = secrets.token_urlsafe(32)
        else:
            resolved[config.name] = found.get_secret_value()
    return resolved


class CollectorAuth:
    def __init__(
        self,
        grants: Mapping[str, CollectorGrant],
        secrets_by_collector: Mapping[str, str],
    ) -> None:
        self._grants = dict(grants)
        self._secrets = dict(secrets_by_collector)

    @classmethod
    def from_config(
        cls, configs: Sequence[CollectorConfig], secrets_by_collector: Mapping[str, str]
    ) -> "CollectorAuth":
        return cls(collector_grants(configs), secrets_by_collector)

    def _check_secret(self, collector: str, presented: str | None) -> None:
        expected = self._secrets.get(collector)
        if (
            expected is None
            or presented is None
            or not hmac.compare_digest(expected.encode(), presented.encode())
        ):
            raise CollectorAuthError(401, "Invalid collector credentials")

    def authenticate(self, registration: str, presented: str | None) -> CollectorGrant:
        """The grant of ``registration`` if ``presented`` is its collector's secret."""
        grant = self._grants.get(registration)
        if grant is None:
            raise CollectorAuthError(401, "Invalid collector credentials")
        self._check_secret(grant.collector, presented)
        return grant

    def misp_instance(self, connector: str, presented: str | None) -> str:
        """The MISP instance of MISP connector ``connector``, after authenticating it."""
        grant = next(
            (g for g in self._grants.values() if g.collector == connector), None
        )
        if grant is None or grant.misp_instance is None:
            raise CollectorAuthError(401, "Invalid collector credentials")
        self._check_secret(connector, presented)
        return grant.misp_instance
