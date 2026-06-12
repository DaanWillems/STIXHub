import uuid

import pytest

from processors.stix import STIX_NAMESPACE, process


def _base(type_: str, id_: str, **extra: object) -> dict:  # type: ignore[type-arg]
    return {"type": type_, "id": id_, "spec_version": "2.1", **extra}


# --- SCOs ---

def test_ipv4_addr_value_and_id() -> None:
    raw = _base("ipv4-addr", "ipv4-addr--original", value="198.51.100.1")
    result = process(raw)
    assert result.value == "198.51.100.1"
    assert result.platform_id.startswith("ipv4-addr--")
    assert result.other_stix_ids == ["ipv4-addr--original"]
    assert result.object["id"] == result.platform_id


def test_domain_name_value_and_id() -> None:
    raw = _base("domain-name", "domain-name--original", value="example.com")
    result = process(raw)
    assert result.value == "example.com"
    assert result.platform_id.startswith("domain-name--")


def test_url_value_and_id() -> None:
    raw = _base("url", "url--original", value="https://example.com/path")
    result = process(raw)
    assert result.value == "https://example.com/path"
    assert result.platform_id.startswith("url--")


def test_sco_deterministic_id_is_stable() -> None:
    raw = _base("ipv4-addr", "ipv4-addr--original", value="10.0.0.1")
    assert process(raw).platform_id == process(raw).platform_id


def test_sco_different_values_produce_different_ids() -> None:
    r1 = process(_base("ipv4-addr", "ipv4-addr--a", value="1.1.1.1"))
    r2 = process(_base("ipv4-addr", "ipv4-addr--b", value="2.2.2.2"))
    assert r1.platform_id != r2.platform_id


def test_sco_id_matches_stix21_spec() -> None:
    import json
    raw = _base("ipv4-addr", "ipv4-addr--original", value="198.51.100.0/24")
    result = process(raw)
    expected = f"ipv4-addr--{uuid.uuid5(STIX_NAMESPACE, json.dumps({'value': '198.51.100.0/24'}, sort_keys=True, separators=(',', ':')))}"
    assert result.platform_id == expected


# --- SDOs ---

def test_indicator_uses_pattern_as_value() -> None:
    raw = _base(
        "indicator",
        "indicator--original",
        pattern="[ipv4-addr:value = '1.2.3.4']",
        pattern_type="stix",
        valid_from="2024-01-01T00:00:00Z",
    )
    result = process(raw)
    assert result.value == "[ipv4-addr:value = '1.2.3.4']"
    assert result.platform_id.startswith("indicator--")


def test_malware_uses_name() -> None:
    raw = _base("malware", "malware--original", name="WannaCry", is_family=False)
    result = process(raw)
    assert result.value == "WannaCry"
    assert result.platform_id.startswith("malware--")


def test_vulnerability_uses_name() -> None:
    raw = _base("vulnerability", "vulnerability--original", name="CVE-2021-44228")
    result = process(raw)
    assert result.value == "CVE-2021-44228"


def test_location_uses_name_preferentially() -> None:
    raw = _base("location", "location--original", name="Amsterdam", region="western-europe")
    result = process(raw)
    assert result.value == "Amsterdam"


def test_location_falls_back_to_region() -> None:
    raw = _base("location", "location--original", region="western-europe")
    result = process(raw)
    assert result.value == "western-europe"


def test_location_falls_back_to_country() -> None:
    raw = _base("location", "location--original", country="NL")
    result = process(raw)
    assert result.value == "NL"


def test_location_without_identifying_field_raises() -> None:
    raw = _base("location", "location--original", description="somewhere")
    with pytest.raises(ValueError, match="name, region, country"):
        process(raw)


def test_note_uses_abstract_over_content() -> None:
    raw = _base("note", "note--original", abstract="TL;DR", content="Long content here", object_refs=["x--1"])
    result = process(raw)
    assert result.value == "TL;DR"


def test_note_falls_back_to_content() -> None:
    raw = _base("note", "note--original", content="Long content here", object_refs=["x--1"])
    result = process(raw)
    assert result.value == "Long content here"


def test_opinion_uses_opinion_field() -> None:
    raw = _base("opinion", "opinion--original", opinion="agree", object_refs=["x--1"])
    result = process(raw)
    assert result.value == "agree"


def test_grouping_uses_context() -> None:
    raw = _base("grouping", "grouping--original", context="suspicious-activity", object_refs=["x--1"])
    result = process(raw)
    assert result.value == "suspicious-activity"


def test_malware_analysis_uses_product() -> None:
    raw = _base("malware-analysis", "malware-analysis--original", product="VirusTotal")
    result = process(raw)
    assert result.value == "VirusTotal"


def test_observed_data_uses_first_observed() -> None:
    raw = _base(
        "observed-data",
        "observed-data--original",
        first_observed="2024-01-01T00:00:00Z",
        last_observed="2024-01-01T01:00:00Z",
        number_observed=1,
        object_refs=["x--1"],
    )
    result = process(raw)
    assert result.value == "2024-01-01T00:00:00Z"


# --- Unimplemented type ---

def test_unimplemented_type_raises() -> None:
    raw = _base("relationship", "relationship--x", relationship_type="uses", source_ref="x--1", target_ref="x--2")
    with pytest.raises(NotImplementedError, match="relationship"):
        process(raw)


def test_marking_definition_raises() -> None:
    raw = _base("marking-definition", "marking-definition--x", definition_type="statement")
    with pytest.raises(NotImplementedError):
        process(raw)


# --- other_stix_ids ---

def test_other_stix_ids_contains_original_id() -> None:
    original_id = "ipv4-addr--some-original-uuid"
    raw = _base("ipv4-addr", original_id, value="1.2.3.4")
    result = process(raw)
    assert original_id in result.other_stix_ids


def test_processed_object_has_platform_id() -> None:
    raw = _base("ipv4-addr", "ipv4-addr--original", value="1.2.3.4")
    result = process(raw)
    assert result.object["id"] == result.platform_id
    assert result.object["spec_version"] == "2.1"
