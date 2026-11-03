"""End to end: MISP <-> MispConnector <-> platform API <-> buckets, both ways.

Runs in-process (fake MISP, FastAPI TestClient, in-memory repositories), so no
database or MISP instance is needed.
"""

from typing import Any

from models.domain import MispEventOrigin
from tests.misp_support import (
    EVENT_UUID,
    FakeMisp,
    connector_config,
    fixture_event,
    make_connector,
)
from tests.platform_support import Platform, make_platform

REPORT = "11111111-1111-4111-8111-111111111111"
INDICATOR = "22222222-2222-4222-8222-222222222222"
CREATED = "2026-01-01T00:00:00Z"


def _feed_report(name: str) -> list[dict[str, Any]]:
    """A report as a TAXII feed writes it to the refined bucket."""
    return [
        {
            "type": "report",
            "spec_version": "2.1",
            "id": f"report--{REPORT}",
            "created": CREATED,
            "modified": CREATED,
            "name": name,
            "published": CREATED,
            "object_refs": [f"indicator--{INDICATOR}"],
        },
        {
            "type": "indicator",
            "spec_version": "2.1",
            "id": f"indicator--{INDICATOR}",
            "created": CREATED,
            "modified": CREATED,
            "pattern": "[ipv4-addr:value = '198.51.100.9']",
            "pattern_type": "stix",
            "valid_from": CREATED,
        },
    ]


async def _owner(platform: Platform, uuid: str) -> MispEventOrigin:
    return (await platform.sync.get_many("circl", [uuid]))[uuid].origin


async def test_two_way_sync_settles_and_respects_misp_edits() -> None:
    misp = FakeMisp()
    misp.seed(fixture_event())
    platform = await make_platform("raw", "refined")
    connector = make_connector(
        connector_config(push={"source_bucket": "refined", "publish": True}),
        misp,
        platform,
    )

    # MISP -> raw; a pipeline refines it, and a TAXII feed adds a report.
    connector.run_cycle()
    await platform.copy("raw", "refined")
    await platform.ingest("refined", _feed_report("Feed report"), creator="feed")
    # Only the feed's report goes to MISP: the event came from MISP.
    connector.run_cycle()
    assert set(misp.events) == {EVENT_UUID, REPORT}
    assert misp.events[REPORT]["published"]

    # Steady state: STIXHub's own event isn't pulled back, nothing is rewritten.
    pulled, writes = len(await platform.entities("raw")), list(misp.writes)
    connector.run_cycle()
    connector.run_cycle()
    assert len(await platform.entities("raw")) == pulled
    assert misp.writes == writes

    # An analyst edits the pushed event: it flows into raw and MISP owns it ...
    misp.edit(REPORT, info="Edited in MISP")
    connector.run_cycle()
    assert await _owner(platform, REPORT) is MispEventOrigin.misp
    assert any(
        e.object.get("name") == "Edited in MISP" for e in await platform.entities("raw")
    )

    # ... so a newer version from the feed no longer overwrites the edit.
    await platform.ingest("refined", _feed_report("Feed report v2"), creator="feed")
    connector.run_cycle()
    assert misp.events[REPORT]["info"] == "Edited in MISP"
    assert misp.writes == writes
