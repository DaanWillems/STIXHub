import json

import pytest

from collectors.misp_cursor import MispCursor, MispCursorError

OVERLAP = 3600


def test_roundtrip() -> None:
    cursor = MispCursor(100, {"a": 90})
    assert MispCursor.parse(cursor.serialize()) == cursor


def test_parse_defaults_recent() -> None:
    assert MispCursor.parse('{"publish_timestamp": 5}') == MispCursor(5, {})


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not json",
        "[]",
        '{"recent": {}}',
        '{"publish_timestamp": "5"}',
        '{"publish_timestamp": true}',
        '{"publish_timestamp": -1}',
        '{"publish_timestamp": 5, "recent": []}',
        '{"publish_timestamp": 5, "recent": {"a": "1"}}',
        '{"taxii": "https://x/?next=1"}',
    ],
)
def test_parse_rejects_bad_input(raw: str) -> None:
    with pytest.raises(MispCursorError):
        MispCursor.parse(raw)


def test_window_start_subtracts_overlap_and_floors_at_zero() -> None:
    assert MispCursor(10_000).window_start(OVERLAP) == 6_400
    assert MispCursor(100).window_start(OVERLAP) == 0


def test_initial_with_lookback() -> None:
    cursor = MispCursor.initial(now=10 * 86400, initial_lookback_days=3, overlap_seconds=OVERLAP)
    assert cursor.window_start(OVERLAP) == 7 * 86400
    assert cursor.recent == {}


def test_initial_without_lookback_is_unbounded() -> None:
    assert MispCursor.initial(now=10**9, initial_lookback_days=None).window_start(OVERLAP) == 0


def test_initial_lookback_larger_than_now() -> None:
    assert MispCursor.initial(now=100, initial_lookback_days=30).window_start(0) == 0


def test_advance_is_forward_only() -> None:
    cursor = MispCursor(10_000).advance("late", 9_000, OVERLAP)
    assert cursor.publish_timestamp == 10_000
    assert cursor.recent == {"late": 9_000}


def test_same_second_events_are_all_remembered() -> None:
    cursor = MispCursor(0)
    for uuid in ("a", "b", "c"):
        cursor = cursor.advance(uuid, 5_000, OVERLAP)
    assert cursor.publish_timestamp == 5_000
    assert all(cursor.should_skip(u, 5_000) for u in ("a", "b", "c"))
    assert not cursor.should_skip("d", 5_000)


def test_should_skip_requires_matching_timestamp() -> None:
    cursor = MispCursor(0).advance("a", 5_000, OVERLAP)
    assert cursor.should_skip("a", 5_000)
    assert not cursor.should_skip("a", 5_001)  # republished


def test_late_event_inside_overlap_is_processed_once() -> None:
    cursor = MispCursor(0).advance("new", 10_000, OVERLAP)
    assert cursor.window_start(OVERLAP) == 6_400
    # Event with an older timestamp becomes visible: not skipped, still in window.
    assert not cursor.should_skip("late", 8_000)
    cursor = cursor.advance("late", 8_000, OVERLAP)
    assert cursor.publish_timestamp == 10_000
    assert cursor.should_skip("late", 8_000)
    assert cursor.should_skip("new", 10_000)


def test_advance_prunes_entries_outside_window() -> None:
    cursor = MispCursor(0).advance("old", 2_000, OVERLAP).advance("mid", 5_000, OVERLAP)
    assert "old" in cursor.recent
    cursor = cursor.advance("new", 9_000, OVERLAP)
    assert cursor.recent == {"new": 9_000}


def test_advance_does_not_mutate() -> None:
    cursor = MispCursor(1, {"a": 1})
    cursor.advance("b", 2, OVERLAP)
    assert cursor == MispCursor(1, {"a": 1})
    assert json.loads(cursor.serialize()) == {"publish_timestamp": 1, "recent": {"a": 1}}
