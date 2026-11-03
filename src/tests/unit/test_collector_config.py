import pytest
from pydantic import ValidationError

from collectors.validation import (
    load_credentials,
    load_misp_api_key,
    validate_collectors,
)
from config import CollectorCredentials, Settings
from models.domain import (
    BucketConfig,
    BucketMode,
    MispConnectorConfig,
    PlatformConfig,
    TaxiiCollectorConfig,
)


def _collector(**overrides: object) -> TaxiiCollectorConfig:
    values: dict[str, object] = {
        "name": "feed",
        "remote_url": "https://taxii.example.com/api/",
        "remote_collection_id": "abc",
        "target_bucket": "intel",
    }
    values.update(overrides)
    return TaxiiCollectorConfig.model_validate(values)


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


# --- MISP connectors ---

_MISP_BUCKETS = [
    BucketConfig(name=name, mode=BucketMode.append) for name in ("raw", "refined")
]


def _misp(**overrides: object) -> MispConnectorConfig:
    values: dict[str, object] = {
        "type": "misp",
        "name": "circl",
        "url": "https://MISP.example.org/",
        "credentials_env": "CIRCL",
        "pull": {"target_bucket": "raw"},
        "push": {"source_bucket": "refined"},
    }
    values.update(overrides)
    return MispConnectorConfig.model_validate(values)


def test_collectors_are_told_apart_by_type() -> None:
    config = PlatformConfig.model_validate(
        {
            "buckets": [],
            "collections": [],
            "roles": [],
            "pipelines": [],
            "collectors": [
                _collector().model_dump(exclude={"type"}),
                _misp().model_dump(),
            ],
        }
    )
    assert [type(c) for c in config.collectors] == [
        TaxiiCollectorConfig,
        MispConnectorConfig,
    ]


def test_misp_defaults() -> None:
    config = _misp()
    assert config.verify_tls is True
    assert config.pull is not None and config.pull.published_only
    assert config.pull.initial_lookback_days == 30
    assert config.push is not None and config.push.distribution == 0
    assert not config.push.publish
    assert config.normalized_url == "https://misp.example.org"


def test_valid_misp_connector_passes() -> None:
    validate_collectors(_MISP_BUCKETS, [_misp(), _misp(name="pull-only", push=None)])


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"pull": None, "push": None}, "neither pull nor push"),
        ({"pull": {"target_bucket": "missing"}}, "unknown bucket 'missing'"),
        ({"push": {"source_bucket": "raw"}}, "same bucket 'raw'"),
        (
            {"push": {"source_bucket": "refined", "distribution": 4}},
            "distribution 4 requires sharing_group_id",
        ),
    ],
)
def test_invalid_misp_connector_is_rejected(
    overrides: dict[str, object], error: str
) -> None:
    with pytest.raises(RuntimeError, match=error):
        validate_collectors(_MISP_BUCKETS, [_misp(**overrides)])


@pytest.mark.parametrize("name", ["", "has:colon", "has space"])
def test_collector_names_are_restricted(name: str) -> None:
    with pytest.raises(ValidationError):
        _misp(name=name)
    with pytest.raises(ValidationError):
        _collector(name=name)


def test_misp_api_key_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STIX_HUB_MISP_CREDENTIALS__CIRCL__API_KEY", "k3y")

    settings = Settings()  # type: ignore[call-arg]  # values come from the env
    assert load_misp_api_key(_misp(), settings.MISP_CREDENTIALS) == "k3y"
    with pytest.raises(RuntimeError, match="MISP_CREDENTIALS__CIRCL__API_KEY"):
        load_misp_api_key(_misp(), credentials={})
