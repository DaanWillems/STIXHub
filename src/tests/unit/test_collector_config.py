import pytest
from pydantic import SecretStr, TypeAdapter

from collectors.validation import (
    load_credentials,
    load_secret,
    validate_collector_secrets,
    validate_collectors,
)
from config import CollectorCredentials, Settings
from models.domain import (
    BucketConfig,
    BucketMode,
    CollectorConfig,
    MispCollectorConfig,
    PlatformConfig,
    TaxiiCollectorConfig,
)

_ADAPTER: TypeAdapter[CollectorConfig] = TypeAdapter(CollectorConfig)


def _collector(**overrides: object) -> TaxiiCollectorConfig:
    values: dict[str, object] = {
        "name": "feed",
        "remote_url": "https://taxii.example.com/api/",
        "remote_collection_id": "abc",
        "target_bucket": "intel",
        "secret_env": "FEED",
    }
    values.update(overrides)
    config = _ADAPTER.validate_python(values)
    assert isinstance(config, TaxiiCollectorConfig)
    return config


_BUCKETS = [BucketConfig(name="intel", mode=BucketMode.append)]


def test_collector_defaults() -> None:
    config = _collector()
    assert config.poll_interval == 300
    assert config.max_retries == 3
    assert config.shutdown_timeout == 30
    assert config.credentials_env is None


def test_platform_config_collectors_optional() -> None:
    config = PlatformConfig.model_validate(
        {"buckets": [], "collections": [], "roles": [], "pipelines": []}
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


def test_secret_env_is_required() -> None:
    with pytest.raises(ValueError, match="secret_env"):
        _ADAPTER.validate_python(
            {
                "name": "feed",
                "remote_url": "https://taxii.example.com/api/",
                "remote_collection_id": "abc",
                "target_bucket": "intel",
            }
        )


def test_load_secret_matches_key_case_insensitively() -> None:
    assert load_secret(_collector(), {"feed": SecretStr("s")}) == "s"


def test_load_secret_missing_raises() -> None:
    with pytest.raises(RuntimeError, match="STIX_HUB_COLLECTOR_SECRETS__FEED"):
        load_secret(_collector(), {})


def test_load_secret_empty_raises() -> None:
    with pytest.raises(RuntimeError, match="no shared secret"):
        load_secret(_collector(), {"feed": SecretStr("")})


def test_validate_collector_secrets_accepts_distinct_secrets() -> None:
    validate_collector_secrets(
        [_collector(), _collector(name="other", secret_env="OTHER")],
        {"feed": SecretStr("a"), "other": SecretStr("b")},
    )


def test_validate_collector_secrets_rejects_shared_secret() -> None:
    with pytest.raises(RuntimeError, match="same shared secret"):
        validate_collector_secrets(
            [_collector(), _collector(name="other", secret_env="OTHER")],
            {"feed": SecretStr("a"), "other": SecretStr("a")},
        )


def test_settings_reads_collector_secrets_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STIX_HUB_COLLECTOR_SECRETS__MITRE_ATTACK", "s3cret")

    secrets = Settings().COLLECTOR_SECRETS

    assert load_secret(_collector(secret_env="MITRE_ATTACK"), secrets) == "s3cret"


_MISP: dict[str, object] = {
    "name": "circl",
    "type": "misp",
    "misp_url": "https://misp.example.org",
    "api_key_env": "CIRCL",
    "secret_env": "CIRCL",
    "target_bucket": "intel",
}


def test_missing_type_defaults_to_taxii() -> None:
    assert _collector().type == "taxii"


def test_misp_config_defaults() -> None:
    config = _ADAPTER.validate_python(_MISP)
    assert isinstance(config, MispCollectorConfig)
    assert config.verify_tls is True
    assert config.ca_bundle is None
    assert config.timeout == 120
    assert config.initial_lookback_days is None
    assert config.overlap_seconds == 3600
    assert config.tags == []
    assert config.org is None
    assert config.page_size == 100
    assert config.submit_batch_size == 500
    assert config.poll_interval == 300


def test_misp_rejects_ca_bundle_with_verify_tls_false() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        _ADAPTER.validate_python({**_MISP, "ca_bundle": "/ca.pem", "verify_tls": False})


def test_misp_accepts_ca_bundle_with_verify_tls() -> None:
    config = _ADAPTER.validate_python({**_MISP, "ca_bundle": "/ca.pem"})
    assert isinstance(config, MispCollectorConfig)
    assert config.ca_bundle == "/ca.pem"


def test_unknown_key_rejected_for_taxii() -> None:
    with pytest.raises(ValueError, match="extra_forbidden"):
        _collector(bogus=1)


def test_unknown_key_rejected_for_misp() -> None:
    with pytest.raises(ValueError, match="extra_forbidden"):
        _ADAPTER.validate_python({**_MISP, "bogus": 1})


def test_taxii_field_rejected_on_misp() -> None:
    with pytest.raises(ValueError, match="extra_forbidden"):
        _ADAPTER.validate_python({**_MISP, "remote_url": "https://x"})


def test_unknown_type_rejected() -> None:
    with pytest.raises(ValueError, match="union_tag_invalid"):
        _ADAPTER.validate_python({**_MISP, "type": "other"})


def test_platform_config_parses_mixed_collectors() -> None:
    config = PlatformConfig.model_validate(
        {
            "buckets": [],
            "collections": [],
            "roles": [],
            "pipelines": [],
            "collectors": [
                {
                    "name": "feed",
                    "remote_url": "https://t/",
                    "remote_collection_id": "a",
                    "target_bucket": "intel",
                    "secret_env": "FEED",
                },
                _MISP,
            ],
        }
    )
    assert [type(c) for c in config.collectors] == [
        TaxiiCollectorConfig,
        MispCollectorConfig,
    ]
