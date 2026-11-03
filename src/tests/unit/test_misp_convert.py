from typing import Any

from collectors.misp.convert import event_to_stix, is_from_misp, stix_to_event
from database.repositories.bucket import InMemoryBucketRepository
from models.domain import Bucket
from services.ingest import ingest_objects
from tests.misp_support import EVENT_UUID, all_uuids, fixture_event

REPORT_UUID = "11111111-1111-4111-8111-111111111111"
INDICATOR_UUID = "22222222-2222-4222-8222-222222222222"


def _external_report() -> list[dict[str, Any]]:
    """A report as a TAXII feed would publish it."""
    created = "2026-01-01T00:00:00Z"
    return [
        {
            "type": "report",
            "spec_version": "2.1",
            "id": f"report--{REPORT_UUID}",
            "created": created,
            "modified": created,
            "name": "TAXII report",
            "published": created,
            "object_refs": [f"indicator--{INDICATOR_UUID}"],
        },
        {
            "type": "indicator",
            "spec_version": "2.1",
            "id": f"indicator--{INDICATOR_UUID}",
            "created": created,
            "modified": created,
            "pattern": "[ipv4-addr:value = '198.51.100.9']",
            "pattern_type": "stix",
            "valid_from": created,
        },
    ]


def test_published_event_becomes_report_and_unpublished_a_grouping() -> None:
    def containers(event: dict[str, Any]) -> list[str]:
        return [
            o["id"] for o in event_to_stix(event) if o["type"] in ("report", "grouping")
        ]

    assert containers(fixture_event()) == [f"report--{EVENT_UUID}"]
    assert containers(fixture_event(published=False)) == [f"grouping--{EVENT_UUID}"]


async def test_every_converted_object_can_be_ingested() -> None:
    repo = InMemoryBucketRepository()
    bucket = await repo.save(Bucket(name="raw"))

    status = await ingest_objects(repo, bucket, event_to_stix(fixture_event()), "t")

    assert status.failures == []


def test_round_trip_keeps_event_attribute_and_object_uuids() -> None:
    objects = event_to_stix(fixture_event())
    assert is_from_misp(objects)

    event = stix_to_event(
        objects, event_uuid=EVENT_UUID, distribution=0, sharing_group_id=None
    )

    assert event["uuid"] == EVENT_UUID
    assert all_uuids(event) == all_uuids(fixture_event())
    # MISP assigns these; publishing is a separate, explicit call.
    assert {"timestamp", "published", "publish_timestamp"}.isdisjoint(event)


def test_external_report_becomes_event_with_given_uuid() -> None:
    objects = _external_report()
    assert not is_from_misp(objects)

    event = stix_to_event(
        objects, event_uuid=REPORT_UUID, distribution=1, sharing_group_id=None
    )

    assert event["uuid"] == REPORT_UUID
    assert event["info"] == "TAXII report"
    assert int(event["distribution"]) == 1
    assert all_uuids(event) == {INDICATOR_UUID}
