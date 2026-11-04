from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from collectors.misp.client import MispRequestError
from collectors.misp.pusher import MispPusher
from models.domain import MispEventOrigin, MispEventSync
from tests.misp_support import (
    EVENT_UUID,
    INSTANCE,
    NOW,
    OUR_ORG,
    T0,
    THEIR_ORG,
    FakeMisp,
    all_uuids,
    connector_config,
    fixture_event,
    make_connector,
    small_event,
)
from tests.platform_support import Platform, make_platform

REPORT = "11111111-1111-4111-8111-111111111111"
INDICATOR = "22222222-2222-4222-8222-222222222222"
MALWARE = "33333333-3333-4333-8333-333333333333"
LOOSE = "44444444-4444-4444-8444-444444444444"
RELATIONSHIP = "55555555-5555-4555-8555-555555555555"
CREATED = "2026-01-01T00:00:00Z"


def _sdo(type_: str, uuid: str, **fields: Any) -> dict[str, Any]:
    return {
        "type": type_,
        "spec_version": "2.1",
        "id": f"{type_}--{uuid}",
        "created": CREATED,
        "modified": CREATED,
        **fields,
    }


def _indicator(uuid: str, ip: str) -> dict[str, Any]:
    pattern = f"[ipv4-addr:value = '{ip}']"
    return _sdo(
        "indicator", uuid, pattern=pattern, pattern_type="stix", valid_from=CREATED
    )


def _feed(name: str = "TAXII report") -> list[dict[str, Any]]:
    """A report from a TAXII feed: an indicator of a malware, and a loose object."""
    return [
        _sdo(
            "report",
            REPORT,
            name=name,
            published=CREATED,
            object_refs=[f"indicator--{INDICATOR}", f"relationship--{RELATIONSHIP}"],
        ),
        _indicator(INDICATOR, "198.51.100.9"),
        _sdo("malware", MALWARE, name="Emotet", is_family=True),
        _sdo(
            "relationship",
            RELATIONSHIP,
            relationship_type="indicates",
            source_ref=f"indicator--{INDICATOR}",
            target_ref=f"malware--{MALWARE}",
        ),
        _indicator(LOOSE, "203.0.113.1"),
    ]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _report(uuid: str, indicator: str, ip: str) -> list[dict[str, Any]]:
    """A report with a single indicator."""
    return [
        _sdo(
            "report",
            uuid,
            name=f"Report {uuid[:4]}",
            published=CREATED,
            object_refs=[f"indicator--{indicator}"],
        ),
        _indicator(indicator, ip),
    ]


async def _push(
    misp: FakeMisp, clock: Callable[[], datetime] = lambda: NOW, **push: object
) -> tuple[MispPusher, Platform]:
    platform = await make_platform("refined")
    config = connector_config(pull=None, push={"source_bucket": "refined", **push})
    connector = make_connector(
        config,
        misp,
        platform,
        excluded_creators=frozenset({"collector:sibling"}),
        clock=clock,
    )
    assert connector.pusher is not None
    return connector.pusher, platform


async def _record(platform: Platform, uuid: str = REPORT) -> MispEventSync | None:
    return (await platform.sync.get_many(INSTANCE, [uuid])).get(uuid)


async def test_report_becomes_event_with_what_it_references() -> None:
    misp = FakeMisp()
    push, platform = await _push(misp)
    await platform.ingest("refined", _feed(), creator="feed")

    assert push.run_once() == 1

    event = misp.events[REPORT]
    assert event["info"] == "TAXII report"
    # The loose indicator is not part of any report, so it isn't pushed.
    assert all_uuids(event) == {INDICATOR}
    assert await _record(platform) == MispEventSync(
        INSTANCE, REPORT, MispEventOrigin.stixhub, int(event["timestamp"])
    )
    assert push.run_once() == 0


SECOND = "66666666-6666-4666-8666-666666666666"


async def test_refused_report_is_skipped_and_later_ones_are_still_pushed() -> None:
    misp = FakeMisp()
    misp.fail_next("add", REPORT, MispRequestError(403, "Not allowed"))
    push, platform = await _push(misp)
    await platform.ingest("refined", _feed(), creator="feed")
    await platform.ingest("refined", _report(SECOND, LOOSE, "203.0.113.1"), "feed")

    assert push.run_once() == 1

    assert set(misp.events) == {SECOND}
    assert push.cursor == max(e.id for e in await platform.entities("refined"))


@pytest.mark.parametrize("status", [401, 403])
async def test_failing_read_ends_the_step_and_keeps_the_cursor(status: int) -> None:
    misp = FakeMisp()
    misp.fail_next("get", REPORT, MispRequestError(status, "Denied"))
    push, platform = await _push(misp)
    await platform.ingest("refined", _feed(), creator="feed")

    with pytest.raises(MispRequestError):
        push.run_once()

    assert push.cursor == 0
    assert push.run_once() == 1


async def test_every_page_of_the_bucket_is_read() -> None:
    misp = FakeMisp()
    push, platform = await _push(misp, batch_size=1)
    await platform.ingest("refined", _feed(), creator="feed")

    assert push.run_once() == 1
    assert push.cursor == max(e.id for e in await platform.entities("refined"))


async def test_newer_report_version_updates_the_event_and_publishes() -> None:
    misp = FakeMisp()
    push, platform = await _push(misp, publish=True)
    await platform.ingest("refined", _feed(), creator="feed")
    push.run_once()

    await platform.ingest("refined", _feed("Renamed report"), creator="feed")

    assert push.run_once() == 1
    assert misp.writes == [
        ("add", REPORT),
        ("publish", REPORT),
        ("update", REPORT),
        ("publish", REPORT),
    ]
    assert misp.events[REPORT]["info"] == "Renamed report"


async def test_misp_event_keeps_its_uuids_through_stixhub() -> None:
    source, target = FakeMisp(), FakeMisp()
    source.seed(fixture_event())
    platform = await make_platform("raw", "refined")
    puller = make_connector(connector_config(name="a", push=None), source, platform)
    pusher = make_connector(
        connector_config(name="b", url="https://other.example", pull=None),
        target,
        platform,
    )
    assert puller.puller is not None and pusher.pusher is not None
    puller.puller.run_once()
    await platform.copy("raw", "refined")

    assert pusher.pusher.run_once() == 1
    assert all_uuids(target.events[EVENT_UUID]) == all_uuids(fixture_event())


async def test_report_waits_for_objects_still_on_their_way() -> None:
    misp = FakeMisp()
    push, platform = await _push(misp, clock=_utcnow, wait_for_refs_seconds=60)
    report, *objects, _loose = _feed()
    await platform.ingest("refined", [report], creator="feed")

    assert push.run_once() == 0
    await platform.ingest("refined", objects, creator="feed")

    assert push.run_once() == 1
    assert all_uuids(misp.events[REPORT]) == {INDICATOR}


async def test_report_is_pushed_without_objects_that_never_arrive() -> None:
    misp, now = FakeMisp(), [datetime.now(UTC)]
    push, platform = await _push(misp, clock=lambda: now[0], wait_for_refs_seconds=60)
    report, *_ = _feed()
    await platform.ingest("refined", [report], creator="feed")
    assert push.run_once() == 0

    now[0] += timedelta(seconds=61)

    assert push.run_once() == 1
    assert misp.writes == [("add", REPORT)]


@pytest.mark.parametrize(
    "creator", ["collector:circl", "collector:sibling", "excluded-feed"]
)
async def test_excluded_creators_are_not_pushed(creator: str) -> None:
    misp = FakeMisp()
    push, platform = await _push(misp, exclude_creators=["excluded-feed"])
    await platform.ingest("refined", _feed(), creator=creator)

    assert push.run_once() == 0
    assert misp.events == {}


STIXHUB = MispEventOrigin.stixhub
MISP = MispEventOrigin.misp
REMOTE_TS = T0 + 5


@pytest.mark.parametrize(
    ("record", "in_misp", "write", "owner"),
    [
        # STIXHub creates events nobody else has.
        (None, None, "add", STIXHUB),
        (None, THEIR_ORG, None, None),
        # A pending record: an earlier add crashed before or after MISP got
        # it. Only an event STIXHub's organisation created is adopted.
        ((STIXHUB, None), None, "add", STIXHUB),
        ((STIXHUB, None), OUR_ORG, "update", STIXHUB),
        ((STIXHUB, None), THEIR_ORG, None, MISP),
        # STIXHub updates its own events until someone edits them in MISP.
        ((STIXHUB, REMOTE_TS), OUR_ORG, "update", STIXHUB),
        ((STIXHUB, REMOTE_TS - 1), OUR_ORG, None, MISP),
        ((STIXHUB, REMOTE_TS), None, None, STIXHUB),
        ((MISP, REMOTE_TS), THEIR_ORG, None, MISP),
    ],
    ids=[
        "new",
        "exists-elsewhere",
        "pending-absent",
        "pending-ours",
        "pending-theirs",
        "own",
        "edited-in-misp",
        "deleted-in-misp",
        "misp-owned",
    ],
)
async def test_ownership_rules(
    record: tuple[MispEventOrigin, int | None] | None,
    in_misp: str | None,
    write: str | None,
    owner: MispEventOrigin | None,
) -> None:
    """``in_misp`` is the organisation that created the event in MISP, if any."""
    misp = FakeMisp()
    push, platform = await _push(misp)
    if in_misp is not None:
        misp.seed(small_event(REPORT, "Existing", REMOTE_TS), org=in_misp)
    if record is not None:
        await platform.sync.upsert(MispEventSync(INSTANCE, REPORT, *record))
    await platform.ingest("refined", _feed(), creator="feed")

    push.run_once()

    assert misp.writes == ([(write, REPORT)] if write else [])
    found = await _record(platform)
    assert (found.origin if found else None) is owner


async def test_add_whose_response_got_lost_is_adopted_not_repeated() -> None:
    misp = FakeMisp(lose_next_add_response=True)
    push, platform = await _push(misp)
    await platform.ingest("refined", _feed(), creator="feed")

    assert push.run_once() == 1

    assert misp.writes == [("add", REPORT)]
    assert await _record(platform) == MispEventSync(
        INSTANCE, REPORT, STIXHUB, int(misp.events[REPORT]["timestamp"])
    )
