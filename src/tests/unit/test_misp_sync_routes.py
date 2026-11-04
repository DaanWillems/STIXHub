from typing import Any

from fastapi.testclient import TestClient

from models.domain import MispConnectorConfig
from tests.platform_support import make_platform, secret_of

EVENT = "5e8d5c5a-0b1c-4b8e-9d2a-1a2b3c4d5e6f"


def _connector(name: str, url: str = "https://misp.example.org") -> MispConnectorConfig:
    return MispConnectorConfig.model_validate(
        {
            "type": "misp",
            "name": name,
            "url": url,
            "credentials_env": "X",
            "push": {"source_bucket": "refined"},
        }
    )


async def _client() -> TestClient:
    platform = await make_platform(
        "refined", collectors=[_connector("circl"), _connector("other")]
    )
    return platform.client


def _put(client: TestClient, connector: str, body: dict[str, object]) -> Any:
    return client.put(
        f"/misp-sync/{connector}/events/{EVENT}",
        json=body,
        headers={"Authorization": f"Bearer {secret_of(connector)}"},
    )


def _lookup(client: TestClient, connector: str) -> list[dict[str, object]]:
    resp = client.post(
        f"/misp-sync/{connector}/events/lookup",
        json={"event_uuids": [EVENT]},
        headers={"Authorization": f"Bearer {secret_of(connector)}"},
    )
    events: list[dict[str, object]] = resp.json()["events"]
    return events


async def test_ownership_moves_to_misp_but_never_back() -> None:
    client = await _client()
    _put(client, "circl", {"origin": "stixhub", "misp_timestamp": None})

    assert (
        _put(client, "circl", {"origin": "misp", "misp_timestamp": 2}).status_code
        == 200
    )
    assert (
        _put(client, "circl", {"origin": "stixhub", "misp_timestamp": 3}).status_code
        == 409
    )
    assert _lookup(client, "circl")[0]["origin"] == "misp"


async def test_records_need_the_connectors_secret() -> None:
    client = await _client()

    resp = client.put(
        f"/misp-sync/circl/events/{EVENT}",
        json={"origin": "stixhub", "misp_timestamp": 1},
        headers={"Authorization": f"Bearer {secret_of('other')}"},
    )

    assert resp.status_code == 401


async def test_connectors_of_one_misp_instance_share_records() -> None:
    platform = await make_platform(
        "refined",
        collectors=[
            _connector("circl"),
            _connector("circl-out", url="https://MISP.example.org/"),
            _connector("elsewhere", url="https://other.example.org"),
        ],
    )
    _put(platform.client, "circl", {"origin": "misp", "misp_timestamp": 1})

    assert _lookup(platform.client, "circl-out")[0]["origin"] == "misp"
    assert _lookup(platform.client, "elsewhere") == []
