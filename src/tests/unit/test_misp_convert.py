import json
from pathlib import Path
from typing import Any

import pytest

from collectors import misp_convert
from collectors.misp_convert import chunk, convert_event
from processors.stix import REF_PROPERTIES

FIXTURE = Path(__file__).parent.parent / "fixtures" / "misp_event.json"


def _event() -> dict[str, Any]:
    event: dict[str, Any] = json.loads(FIXTURE.read_text())["Event"]
    return event


def test_convert_normal_event() -> None:
    objects = convert_event(_event())
    types = [o["type"] for o in objects]
    assert "indicator" in types
    assert "ipv4-addr" in types
    assert "relationship" in types


def test_refs_come_last() -> None:
    types = [o["type"] for o in convert_event(_event())]
    first_ref = next(i for i, t in enumerate(types) if t in REF_PROPERTIES)
    assert all(t in REF_PROPERTIES for t in types[first_ref:])


def test_empty_result(monkeypatch: pytest.MonkeyPatch) -> None:
    class EmptyParser:
        class bundle:
            @staticmethod
            def serialize() -> str:
                return json.dumps({"type": "bundle"})

        def parse_misp_event(self, event: dict[str, Any]) -> None:
            pass

    monkeypatch.setattr(misp_convert, "MISPtoSTIX21Parser", EmptyParser)
    assert convert_event(_event()) == []


def test_parser_exception_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenParser:
        def parse_misp_event(self, event: dict[str, Any]) -> None:
            raise RuntimeError("boom")

    monkeypatch.setattr(misp_convert, "MISPtoSTIX21Parser", BrokenParser)
    with pytest.raises(RuntimeError):
        convert_event(_event())


def test_fresh_parser_per_event() -> None:
    first = convert_event(_event())
    second = convert_event(_event())
    assert [o["id"] for o in first] == [o["id"] for o in second]


def test_chunk() -> None:
    items = [{"i": i} for i in range(5)]
    assert [len(c) for c in chunk(items, 2)] == [2, 2, 1]
    assert list(chunk([], 3)) == []
    with pytest.raises(ValueError):
        list(chunk(items, 0))
