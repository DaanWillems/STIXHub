"""MISP <-> STIX 2.1 conversion with misp-stix.

Only this module (and ``client``) imports misp-stix; everything else works with
plain dicts.
"""

import json
import logging
import uuid
from typing import Any

from misp_stix_converter import (
    ExternalSTIX2toMISPParser,
    InternalSTIX2toMISPParser,
    MISPtoSTIX21Parser,
)
from misp_stix_converter.tools.stix2_loading_helpers import load_stix2_content

logger = logging.getLogger(__name__)

# Labels misp-stix puts on the report/grouping of an event it converted. The
# same check as misp-stix's is_stix2_from_misp, minus its warning on every call.
_MISP_TOOL_LABELS = frozenset(
    {'misp:tool="MISP-STIX-Converter"', 'misp:tool="misp2stix2"'}
)
_CONTAINER_TYPES = frozenset({"report", "grouping"})
# Fields MISP sets itself. Sending them would make MISP refuse an update that
# isn't newer than its copy, or publish an event behind the config's back.
_SERVER_FIELDS = frozenset({"timestamp", "publish_timestamp", "published", "id"})


class ConversionError(Exception):
    """misp-stix could not convert the input."""


def event_to_stix(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert one MISP event to STIX 2.1 objects (a report or a grouping, its
    author, markings, and the objects for its attributes, objects and galaxies).
    """
    # The parser keeps state between events, so every event gets a new one.
    parser = MISPtoSTIX21Parser()
    try:
        parser.parse_misp_event(event)
        return [json.loads(obj.serialize()) for obj in parser.stix_objects]
    except Exception as exc:
        raise ConversionError(f"misp-stix could not convert the event: {exc}") from exc


def is_from_misp(objects: list[dict[str, Any]]) -> bool:
    """Whether misp-stix produced these objects from a MISP event."""
    return any(
        obj.get("type") in _CONTAINER_TYPES
        and _MISP_TOOL_LABELS.intersection(obj.get("labels", []))
        for obj in objects
    )


def _strip_server_fields(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_server_fields(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _strip_server_fields(item)
            for key, item in value.items()
            if key not in _SERVER_FIELDS
        }
    return value


def stix_to_event(
    objects: list[dict[str, Any]],
    *,
    event_uuid: str,
    distribution: int,
    sharing_group_id: int | None,
) -> dict[str, Any]:
    """Convert a bundle holding one report/grouping to a MISP event dict.

    The event gets ``event_uuid`` and is never marked published; MISP assigns
    timestamps and publishing is a separate call.
    """
    parser = (
        InternalSTIX2toMISPParser()
        if is_from_misp(objects)
        else ExternalSTIX2toMISPParser()
    )
    try:
        bundle = load_stix2_content(
            {"type": "bundle", "id": f"bundle--{uuid.uuid4()}", "objects": objects}
        )
        parser.load_stix_bundle(bundle)
        parser.parse_stix_bundle(
            distribution=distribution,
            sharing_group_id=sharing_group_id,
            single_event=True,
        )
        event: dict[str, Any] = json.loads(parser.misp_event.to_json())
    except Exception as exc:
        raise ConversionError(f"misp-stix could not convert the bundle: {exc}") from exc
    for message in _parser_messages(parser):
        logger.warning("misp-stix: %s", message)
    event = _strip_server_fields(event)
    event["uuid"] = event_uuid
    return event


def _parser_messages(parser: Any) -> list[str]:
    """Errors and warnings misp-stix recorded instead of raising."""
    messages: list[str] = []
    for attribute in ("errors", "warnings"):
        recorded = getattr(parser, attribute, None) or {}
        for key, values in dict(recorded).items():
            messages.extend(f"{key}: {value}" for value in values)
    return messages
