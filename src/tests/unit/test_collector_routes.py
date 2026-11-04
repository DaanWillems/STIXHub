from typing import Any

from fastapi.testclient import TestClient

from models.domain import MispConnectorConfig, TaxiiCollectorConfig
from tests.platform_support import Platform, make_platform, secret_of

BUCKET = "intel"

INDICATOR = {
    "type": "indicator",
    "spec_version": "2.1",
    "id": "indicator--8e2e2d2b-17d4-4cbf-938f-98ee46b3cd3f",
    "pattern": "[ipv4-addr:value = '198.51.100.1']",
    "pattern_type": "stix",
    "valid_from": "2026-01-01T00:00:00Z",
}

# A TAXII collector that writes "intel", and a MISP connector that pulls into
# "raw" and pushes (reads) "intel".
FEED = TaxiiCollectorConfig(
    name="feed", remote_url="https://t", remote_collection_id="c", target_bucket=BUCKET
)
MISP = MispConnectorConfig.model_validate(
    {
        "type": "misp",
        "name": "misp",
        "url": "https://misp",
        "credentials_env": "M",
        "pull": {"target_bucket": "raw"},
        "push": {"source_bucket": BUCKET},
    }
)


async def _platform() -> Platform:
    return await make_platform(BUCKET, "raw", collectors=[FEED, MISP])


def _auth(collector: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret_of(collector)}"}


def _register(
    client: TestClient, name: str, bucket: str, secret_owner: str | None = None
) -> Any:
    owner = secret_owner or name.split(":")[0]
    return client.post(
        "/collectors/register",
        json={"name": name, "bucket_name": bucket},
        headers=_auth(owner),
    )


def _submit(client: TestClient, collector_id: int, *objects: Any) -> Any:
    return client.post(
        f"/collectors/{collector_id}/objects",
        json={"objects": list(objects)},
        headers=_auth("feed"),
    )


def _indicator(original_id: str, ip: str) -> dict[str, object]:
    return {**INDICATOR, "id": original_id, "pattern": f"[ipv4-addr:value = '{ip}']"}


# --- registration and writing -----------------------------------------------


async def test_register_returns_stored_cursor() -> None:
    client = (await _platform()).client
    first = _register(client, "feed", BUCKET).json()
    assert first["cursor"] is None

    client.put(
        f"/collectors/{first['id']}/cursor",
        json={"cursor": "2026-02-01T00:00:00Z"},
        headers=_auth("feed"),
    )

    again = _register(client, "feed", BUCKET).json()
    assert again["id"] == first["id"]
    assert again["cursor"] == "2026-02-01T00:00:00Z"


async def test_submit_objects_stores_in_bucket_and_reports_failures() -> None:
    platform = await _platform()
    collector = _register(platform.client, "feed", BUCKET).json()

    bad = {"type": "indicator", "id": "indicator--bad"}  # no spec_version
    resp = _submit(platform.client, collector["id"], INDICATOR, bad)

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "complete_with_errors"
    assert body["failures"][0]["id"] == "indicator--bad"
    [entity] = await platform.entities(BUCKET)
    assert entity.creator == "collector:feed"
    assert entity.other_stix_ids == [INDICATOR["id"]]


async def test_unknown_collector_returns_404() -> None:
    client = (await _platform()).client
    resp = client.put(
        "/collectors/99/cursor", json={"cursor": "x"}, headers=_auth("feed")
    )
    assert resp.status_code == 404


# --- reading -----------------------------------------------------------------


async def test_entities_are_paged_by_id() -> None:
    client = (await _platform()).client
    feed = _register(client, "feed", BUCKET).json()
    objects = [_indicator(f"indicator--{n}", f"198.51.100.{n}") for n in range(3)]
    _submit(client, feed["id"], *objects)
    reader = _register(client, "misp:push", BUCKET).json()
    url = f"/collectors/{reader['id']}/entities"

    first = client.get(url, params={"limit": 2}, headers=_auth("misp")).json()
    after = first["entities"][-1]["id"]
    rest = client.get(url, params={"after": after}, headers=_auth("misp")).json()

    assert [e["other_stix_ids"] for e in first["entities"] + rest["entities"]] == [
        ["indicator--0"],
        ["indicator--1"],
        ["indicator--2"],
    ]


async def test_lookup_finds_newest_entity_by_platform_or_original_id() -> None:
    client = (await _platform()).client
    feed = _register(client, "feed", BUCKET).json()
    old = _submit(client, feed["id"], _indicator("indicator--a", "1.1.1.1"))
    _submit(client, feed["id"], _indicator("indicator--b", "1.1.1.1"))
    platform_id = old.json()["successes"][0]["id"]
    reader = _register(client, "misp:push", BUCKET).json()

    resp = client.post(
        f"/collectors/{reader['id']}/entities/lookup",
        json={"refs": ["indicator--a", platform_id, "indicator--unknown"]},
        headers=_auth("misp"),
    )

    found = resp.json()["entities"]
    assert set(found) == {"indicator--a", platform_id}
    assert found["indicator--a"]["other_stix_ids"] == ["indicator--a"]
    # Both entities share the platform ID; the newest wins.
    assert found[platform_id]["other_stix_ids"] == ["indicator--b"]


# --- access control ----------------------------------------------------------


async def test_registration_needs_the_collectors_own_secret() -> None:
    client = (await _platform()).client

    assert _register(client, "feed", BUCKET, secret_owner="misp").status_code == 401
    assert _register(client, "unknown", BUCKET).status_code == 401
    no_secret = client.post(
        "/collectors/register", json={"name": "feed", "bucket_name": BUCKET}
    )
    assert no_secret.status_code == 401


async def test_a_collector_may_only_register_for_its_configured_bucket() -> None:
    client = (await _platform()).client

    assert _register(client, "feed", "raw").status_code == 403
    assert _register(client, "misp:push", "raw").status_code == 403


async def test_calls_on_a_registration_need_its_secret() -> None:
    client = (await _platform()).client
    feed = _register(client, "feed", BUCKET).json()

    resp = client.post(
        f"/collectors/{feed['id']}/objects",
        json={"objects": [INDICATOR]},
        headers=_auth("misp"),
    )

    assert resp.status_code == 401


async def test_writers_cannot_read_and_readers_cannot_write() -> None:
    client = (await _platform()).client
    writer = _register(client, "feed", BUCKET).json()
    reader = _register(client, "misp:push", BUCKET).json()

    read = client.get(f"/collectors/{writer['id']}/entities", headers=_auth("feed"))
    write = client.post(
        f"/collectors/{reader['id']}/objects",
        json={"objects": [INDICATOR]},
        headers=_auth("misp"),
    )

    assert read.status_code == 403
    assert write.status_code == 403
