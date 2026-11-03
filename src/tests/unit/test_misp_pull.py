import pytest

from collectors.misp.common import RegistrationError
from collectors.misp.convert import ConversionError, event_to_stix
from collectors.misp.puller import MispPuller, PullCursor
from models.domain import MispEventOrigin, MispEventSync
from tests.misp_support import (
    EVENT_UUID,
    INSTANCE,
    OUR_ORG,
    T0,
    THEIR_ORG,
    FakeMisp,
    connector_config,
    fixture_event,
    make_connector,
    small_event,
)
from tests.platform_support import Platform, make_platform

OTHER = "6f9e6d6b-1c2d-4c9f-8e3b-2b3c4d5e6f70"


async def _pull(misp: FakeMisp, **pull: object) -> tuple[MispPuller, Platform]:
    platform = await make_platform("raw")
    config = connector_config(pull={"target_bucket": "raw", **pull}, push=None)
    connector = make_connector(config, misp, platform)
    assert connector.puller is not None
    return connector.puller, platform


async def _reports(platform: Platform) -> list[str]:
    return [e.stix_id for e in await platform.entities("raw") if e.type == "report"]


async def test_events_are_ingested_and_recorded_as_misp_owned() -> None:
    misp = FakeMisp()
    misp.seed(fixture_event())
    misp.seed(small_event(OTHER, "Second event", T0 + 5))
    pull, platform = await _pull(misp)

    assert pull.run_once() == 2

    entities = await platform.entities("raw")
    assert {e.creator for e in entities} == {"collector:circl"}
    assert len(await _reports(platform)) == 2
    records = await platform.sync.get_many(INSTANCE, [EVENT_UUID, OTHER])
    assert {r.origin for r in records.values()} == {MispEventOrigin.misp}
    assert pull.cursor == PullCursor(T0 + 5, frozenset({OTHER}))


async def test_a_second_pull_ingests_only_new_events() -> None:
    misp = FakeMisp()
    misp.seed(small_event(EVENT_UUID, "First", T0))
    pull, platform = await _pull(misp)
    pull.run_once()

    # Same second as the cursor: must not be skipped, nor re-ingest the first.
    misp.seed(small_event(OTHER, "Same second", T0))

    assert pull.run_once() == 1
    assert pull.run_once() == 0
    assert len(await _reports(platform)) == 2


async def test_edited_event_is_ingested_again_under_the_same_report_id() -> None:
    misp = FakeMisp()
    misp.seed(fixture_event())
    pull, platform = await _pull(misp)
    pull.run_once()

    misp.edit(
        EVENT_UUID, info="Phishing campaign against ACME (updated)", republish=True
    )

    assert pull.run_once() == 1
    first, second = await _reports(platform)
    assert first == second


async def test_events_published_after_newer_ones_are_still_pulled() -> None:
    """A draft that is published later sorts by when it was published."""
    misp = FakeMisp()
    misp.seed(small_event(EVENT_UUID, "Draft", T0, published=False))
    misp.seed(small_event(OTHER, "Published", T0 + 5))
    pull, _ = await _pull(misp)
    assert pull.run_once() == 1

    misp.publish(EVENT_UUID)

    assert pull.run_once() == 1


async def test_lookback_and_filters_are_applied() -> None:
    misp = FakeMisp()
    misp.seed(small_event(EVENT_UUID, "Too old", T0 - 31 * 86400))
    pull, _ = await _pull(misp, tags=["tlp:green"], orgs=["ACME"], published_only=False)

    assert pull.run_once() == 0
    assert misp.index_filters[0] == {
        "since": T0 - 30 * 86400,
        "published_only": False,
        "tags": ["tlp:green"],
        "orgs": ["ACME"],
    }


async def test_own_push_is_not_pulled_back_until_edited_in_misp() -> None:
    misp = FakeMisp()
    misp.seed(small_event(EVENT_UUID, "Pushed by STIXHub", T0))
    pull, platform = await _pull(misp)
    await platform.sync.upsert(
        MispEventSync(INSTANCE, EVENT_UUID, MispEventOrigin.stixhub, T0)
    )

    assert pull.run_once() == 0

    misp.edit(EVENT_UUID, info="Edited by an analyst", republish=True)

    assert pull.run_once() == 1
    record = (await platform.sync.get_many(INSTANCE, [EVENT_UUID]))[EVENT_UUID]
    assert record.origin is MispEventOrigin.misp


@pytest.mark.parametrize(("org", "pulled"), [(OUR_ORG, 0), (THEIR_ORG, 1)])
async def test_unrecorded_push_is_adopted_only_if_ours(org: str, pulled: int) -> None:
    """A pending record means an add whose outcome was never recorded."""
    misp = FakeMisp()
    misp.seed(small_event(EVENT_UUID, "Pushed?", T0), org=org)
    pull, platform = await _pull(misp)
    await platform.sync.upsert(
        MispEventSync(INSTANCE, EVENT_UUID, MispEventOrigin.stixhub)
    )

    assert pull.run_once() == pulled

    record = (await platform.sync.get_many(INSTANCE, [EVENT_UUID]))[EVENT_UUID]
    assert record.misp_timestamp == T0
    assert record.origin is (
        MispEventOrigin.stixhub if pulled == 0 else MispEventOrigin.misp
    )


async def test_unconvertible_event_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    misp = FakeMisp()
    misp.seed(small_event(EVENT_UUID, "Broken", T0))
    misp.seed(small_event(OTHER, "Fine", T0 + 1))

    def failing(event: dict[str, object]) -> list[dict[str, object]]:
        if event["uuid"] == EVENT_UUID:
            raise ConversionError("unsupported")
        return event_to_stix(event)

    monkeypatch.setattr("collectors.misp.puller.event_to_stix", failing)
    pull, _ = await _pull(misp)

    assert pull.run_once() == 1
    assert pull.cursor == PullCursor(T0 + 1, frozenset({OTHER}))


async def test_events_pushed_by_another_connector_on_the_same_misp_are_not_pulled() -> (
    None
):
    misp = FakeMisp()
    platform = await make_platform("raw", "refined")
    puller = make_connector(connector_config(name="misp-in", push=None), misp, platform)
    pusher = make_connector(
        connector_config(name="misp-out", pull=None), misp, platform
    )
    assert puller.puller is not None and pusher.pusher is not None
    await platform.ingest(
        "refined", event_to_stix(small_event(OTHER, "Ours", T0)), "feed"
    )

    assert pusher.pusher.run_once() == 1
    assert puller.puller.run_once() == 0


async def test_every_index_page_is_read() -> None:
    misp = FakeMisp()
    for n in range(3):
        misp.seed(small_event(f"{n}{OTHER[1:]}", f"Event {n}", T0 + n))
    pull, platform = await _pull(misp, page_size=1)

    assert pull.run_once() == 3
    assert len(await _reports(platform)) == 3


async def test_event_the_platform_rejects_is_retried_next_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    misp = FakeMisp()
    misp.seed(small_event(EVENT_UUID, "Rejected", T0))
    pull, platform = await _pull(misp)
    cursor = pull.cursor
    invalid = [{"type": "indicator", "id": "indicator--no-spec-version"}]
    monkeypatch.setattr("collectors.misp.puller.event_to_stix", lambda event: invalid)

    assert pull.run_once() == 0

    assert pull.cursor == cursor
    assert await platform.sync.get_many(INSTANCE, [EVENT_UUID]) == {}


async def test_unreadable_stored_cursor_fails_registration() -> None:
    platform = await make_platform("raw")
    config = connector_config(push=None)
    platform.enroll(config)
    api = platform.api(config.name)
    api.update_cursor(api.register(config.name, "raw").id, "2026-01-01T00:00:00Z")

    with pytest.raises(RegistrationError, match="can't read"):
        make_connector(config, FakeMisp(), platform)
