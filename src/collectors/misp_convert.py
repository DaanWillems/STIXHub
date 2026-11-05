import json
from collections.abc import Iterator
from typing import Any

from misp_stix_converter import MISPtoSTIX21Parser

from processors.stix import REF_PROPERTIES


def convert_event(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert one MISP event (the inner ``Event`` dict) to STIX 2.1 objects.

    A fresh parser is used per event. Objects that hold references (see
    ``REF_PROPERTIES``) are ordered last so ref rewriting stays correct across
    submission chunks. Exceptions from the parser propagate to the caller.
    """
    parser = MISPtoSTIX21Parser()
    parser.parse_misp_event({"Event": event})
    bundle = json.loads(parser.bundle.serialize())
    objects: list[dict[str, Any]] = list(bundle.get("objects", []))
    plain = [o for o in objects if o.get("type") not in REF_PROPERTIES]
    referencing = [o for o in objects if o.get("type") in REF_PROPERTIES]
    return plain + referencing


def chunk(objects: list[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    """Yield consecutive slices of ``objects`` with at most ``size`` items."""
    if size < 1:
        raise ValueError("size must be at least 1")
    for start in range(0, len(objects), size):
        yield objects[start : start + size]
