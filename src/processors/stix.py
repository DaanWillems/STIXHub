import json
import uuid
from collections.abc import Mapping
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


def _relationship_processor(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    relationship_type = raw["relationship_type"]
    source_ref = raw["source_ref"]
    target_ref = raw["target_ref"]
    v = f"{source_ref} {relationship_type} {target_ref}"
    return v, {
        "relationship_type": relationship_type,
        "source_ref": source_ref,
        "target_ref": target_ref,
    }


# Well-known TLP marking-definition IDs. TLP markings keep these IDs on
# ingestion (instead of a derived uuid5) so consumers still recognise them.
# TLP 1.0: STIX 2.1 spec section 7.2.1.4.
TLP_1_IDS: dict[str, str] = {
    "tlp:white": "marking-definition--613f2e26-407d-48c7-9eca-b8e91df99dc9",
    "tlp:green": "marking-definition--34098fce-860f-48ae-8e50-ebd3cc5e41da",
    "tlp:amber": "marking-definition--f88d31f6-486f-44da-b317-01333bde0b82",
    "tlp:red": "marking-definition--5e57c739-391a-4eb3-b6be-7d15ca92d5ed",
}
# TLP 2.0: OASIS cti-stix-common-objects, extension-definition-specifications/tlp-2.0.
TLP_2_EXTENSION_ID = "extension-definition--60a3c5c5-0d10-413e-aab3-9e08dde9e88d"
TLP_2_IDS: dict[str, str] = {
    "tlp:clear": "marking-definition--94868c89-83c2-464b-929b-a1a8aa3c8487",
    "tlp:green": "marking-definition--bab4a63c-aed9-4cf5-a766-dfca5abac2bb",
    "tlp:amber": "marking-definition--55d920b0-5e8b-4f79-9ee9-91f868d9b421",
    "tlp:amber+strict": "marking-definition--939a9414-2ddd-4d32-a0cd-375ea402b003",
    "tlp:red": "marking-definition--e828b379-4e03-4974-9ac4-e53a884c97c1",
}
# Reverse lookup covering both TLP versions, e.g. both green IDs -> "tlp:green".
TLP_VALUES_BY_ID: dict[str, str] = {
    **{marking_id: value for value, marking_id in TLP_1_IDS.items()},
    **{marking_id: value for value, marking_id in TLP_2_IDS.items()},
}


def _marking_definition_value(raw: dict[str, Any]) -> tuple[str, int | None]:
    """Return the readable value of a marking and its TLP version (or None)."""
    tlp_2 = raw.get("extensions", {}).get(TLP_2_EXTENSION_ID)
    if tlp_2 is not None:
        return f"tlp:{tlp_2['tlp_2_0'].lower()}", 2
    definition_type = raw.get("definition_type")
    definition = raw.get("definition") or {}
    if definition_type == "tlp":
        return f"tlp:{definition['tlp'].lower()}", 1
    if definition_type == "statement":
        return f"statement:{definition['statement']}", None
    if raw.get("name"):
        return str(raw["name"]), None
    raise ValueError(
        "marking-definition requires a TLP definition, a statement or a name"
    )


def _marking_definition_processor(raw: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    v, _ = _marking_definition_value(raw)
    return v, {"value": v}


def _well_known_id(obj_type: str, raw: dict[str, Any]) -> str | None:
    if obj_type != "marking-definition":
        return None
    value, version = _marking_definition_value(raw)
    if version == 1:
        return TLP_1_IDS.get(value)
    if version == 2:
        return TLP_2_IDS.get(value)
    return None


def platform_id(stix_type: str, value: str) -> str:
    """Deterministic platform ID of an object of ``stix_type`` with ``value``.

    Matches the ID ``process`` assigns to value-keyed types (everything except
    relationships). A ``tlp:*`` marking value maps to its TLP 2.0 ID.
    """
    if stix_type == "relationship":
        raise ValueError("relationship IDs are not derived from a single value")
    if stix_type == "marking-definition" and value in TLP_2_IDS:
        return TLP_2_IDS[value]
    if stix_type == "marking-definition" and value in TLP_1_IDS:
        return TLP_1_IDS[value]
    return _uuid5(stix_type, {"value": value})


# Properties holding a single STIX ID that must be rewritten to platform IDs
# before an object is processed, keyed by STIX type.
REF_PROPERTIES: dict[str, tuple[str, ...]] = {
    "relationship": ("source_ref", "target_ref"),
}


def unresolved_refs(raw: dict[str, Any]) -> list[str]:
    """Return the STIX IDs referenced by ``raw`` that need translating."""
    return [raw[prop] for prop in REF_PROPERTIES.get(raw["type"], ()) if prop in raw]


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
    # SMOs
    "marking-definition": _marking_definition_processor,
    # SROs — identified by the (source, type, target) triple
    "relationship": _relationship_processor,
}


def process(
    raw: dict[str, Any], id_map: Mapping[str, str] | None = None
) -> ProcessedStixObject:
    """Assign ``raw`` a deterministic platform ID.

    ``id_map`` maps original STIX IDs to platform IDs. Reference properties
    (see ``REF_PROPERTIES``) are rewritten through it before the ID is
    derived; refs missing from the map are kept unchanged.
    """
    obj_type = raw["type"]
    original_id = raw["id"]

    if id_map:
        raw = {
            **raw,
            **{
                prop: id_map.get(raw[prop], raw[prop])
                for prop in REF_PROPERTIES.get(obj_type, ())
                if prop in raw
            },
        }

    processor = _PROCESSORS.get(obj_type)
    if processor is None:
        raise NotImplementedError(
            f"No processor implemented for STIX type '{obj_type}'"
        )

    value, contributing = processor(raw)
    object_id = _well_known_id(obj_type, raw) or _uuid5(obj_type, contributing)
    processed_obj = {**raw, "id": object_id}

    return ProcessedStixObject(
        platform_id=object_id,
        value=value,
        other_stix_ids=[original_id],
        object=processed_obj,
    )
