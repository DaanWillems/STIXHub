import pytest

from collectors.validation import load_credentials, validate_collectors
from config import CollectorCredentials, Settings
from models.domain import BucketConfig, BucketMode, CollectorConfig, PlatformConfig


def _collector(**overrides: object) -> CollectorConfig:
    values: dict[str, object] = {
        "name": "feed",
        "remote_url": "https://taxii.example.com/api/",
        "remote_collection_id": "abc",
        "target_bucket": "intel",
    }
    values.update(overrides)
    return CollectorConfig.model_validate(values)


_BUCKETS = [BucketConfig(name="intel", mode=BucketMode.append)]


def test_collector_defaults() -> None:
    config = _collector()
    assert config.poll_interval == 300
    assert config.max_retries == 3
    assert config.shutdown_timeout == 30
    assert config.credentials_env is None


def test_platform_config_collectors_optional() -> None:
    config = PlatformConfig.model_validate(
        {"buckets": [], "collections": [], "roles": []}
    )
    assert config.collectors == []


def test_validate_collectors_accepts_known_bucket() -> None:
    validate_collectors(_BUCKETS, [_collector()])


def test_validate_collectors_rejects_unknown_bucket() -> None:
    with pytest.raises(RuntimeError, match="unknown bucket 'missing'"):
        validate_collectors(_BUCKETS, [_collector(target_bucket="missing")])


def test_validate_collectors_rejects_duplicate_names() -> None:
    with pytest.raises(RuntimeError, match="Duplicate collector name"):
        validate_collectors(_BUCKETS, [_collector(), _collector()])


def test_load_credentials_none_without_prefix() -> None:
    assert load_credentials(_collector(), credentials={}) is None


def test_load_credentials_matches_key_case_insensitively() -> None:
    creds = {"feed": CollectorCredentials.model_validate({"user": "a", "pass": "s"})}
    assert load_credentials(_collector(credentials_env="FEED"), creds) == ("a", "s")


def test_load_credentials_missing_raises() -> None:
    with pytest.raises(RuntimeError, match="COLLECTOR_CREDENTIALS__FEED__PASS"):
        load_credentials(_collector(credentials_env="FEED"), credentials={})


def test_settings_reads_collector_credentials_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STIX_HUB_COLLECTOR_CREDENTIALS__MITRE_TAXII__USER", "alice")
    monkeypatch.setenv("STIX_HUB_COLLECTOR_CREDENTIALS__MITRE_TAXII__PASS", "s3cret")

    creds = Settings().COLLECTOR_CREDENTIALS

    assert load_credentials(_collector(credentials_env="MITRE_TAXII"), creds) == (
        "alice",
        "s3cret",
    )
