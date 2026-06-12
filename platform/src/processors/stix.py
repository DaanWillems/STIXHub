import json
import uuid
from dataclasses import dataclass
from typing import Any

STIX_NAMESPACE = uuid.UUID("00abedb4-aa42-466c-9c01-fed23315a9b7")


@dataclass
class ProcessedStixObject:
    platform_id: str
    value: str
    other_stix_ids: list[str]
    object: dict[str, Any]


def _uuid5(obj_type: str, contributing: dict[str, Any]) -> str:
    canonical = json.dumps(contributing, sort_keys=True, separators=(",", ":"))
    return f"{obj_type}--{uuid.uuid5(STIX_NAMESPACE, canonical)}"


def _sco_value_processor(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    v = raw["value"]
    return v, {"value": v}


def _name_processor(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    v = raw["name"]
    return v, {"value": v}


def _indicator_processor(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    v = raw["pattern"]
    return v, {"value": v}


def _grouping_processor(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    v = raw["context"]
    return v, {"value": v}


def _location_processor(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    v = raw.get("name") or raw.get("region") or raw.get("country")
    if v is None:
        raise ValueError("location requires at least one of: name, region, country")
    return v, {"value": v}


def _malware_analysis_processor(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    v = raw["product"]
    return v, {"value": v}


def _note_processor(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    v = raw.get("abstract") or raw["content"]
    return v, {"value": v}


def _observed_data_processor(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    v = raw["first_observed"]
    return v, {"value": v}


def _opinion_processor(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    v = raw["opinion"]
    return v, {"value": v}


_PROCESSORS: dict[str, Any] = {
    # SCOs — contributing property per STIX 2.1 spec
    "ipv4-addr": _sco_value_processor,
    "domain-name": _sco_value_processor,
    "url": _sco_value_processor,
    # SDOs — contributing property is the searchable value
    "attack-pattern": _name_processor,
    "campaign": _name_processor,
    "course-of-action": _name_processor,
    "grouping": _grouping_processor,
    "identity": _name_processor,
    "indicator": _indicator_processor,
    "infrastructure": _name_processor,
    "intrusion-set": _name_processor,
    "location": _location_processor,
    "malware": _name_processor,
    "malware-analysis": _malware_analysis_processor,
    "note": _note_processor,
    "observed-data": _observed_data_processor,
    "opinion": _opinion_processor,
    "report": _name_processor,
    "threat-actor": _name_processor,
    "tool": _name_processor,
    "vulnerability": _name_processor,
}


def process(raw: dict[str, Any]) -> ProcessedStixObject:
    obj_type = raw["type"]
    original_id = raw["id"]

    processor = _PROCESSORS.get(obj_type)
    if processor is None:
        raise NotImplementedError(
            f"No processor implemented for STIX type '{obj_type}'"
        )

    value, contributing = processor(raw)
    platform_id = _uuid5(obj_type, contributing)
    processed_obj = {**raw, "id": platform_id}

    return ProcessedStixObject(
        platform_id=platform_id,
        value=value,
        other_stix_ids=[original_id],
        object=processed_obj,
    )
