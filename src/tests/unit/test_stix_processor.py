import json
import uuid

import pytest

from processors.stix import STIX_NAMESPACE, TLP_2_EXTENSION_ID, platform_id, process


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
    raw = _base(
        "location", "location--original", name="Amsterdam", region="western-europe"
    )
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
    raw = _base(
        "note",
        "note--original",
        abstract="TL;DR",
        content="Long content here",
        object_refs=["x--1"],
    )
    result = process(raw)
    assert result.value == "TL;DR"


def test_note_falls_back_to_content() -> None:
    raw = _base(
        "note", "note--original", content="Long content here", object_refs=["x--1"]
    )
    result = process(raw)
    assert result.value == "Long content here"


def test_opinion_uses_opinion_field() -> None:
    raw = _base("opinion", "opinion--original", opinion="agree", object_refs=["x--1"])
    result = process(raw)
    assert result.value == "agree"


def test_grouping_uses_context() -> None:
    raw = _base(
        "grouping",
        "grouping--original",
        context="suspicious-activity",
        object_refs=["x--1"],
    )
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
    raw = _base("language-content", "language-content--x", object_ref="x--1")
    with pytest.raises(NotImplementedError, match="language-content"):
        process(raw)


# --- marking-definition ---


def test_marking_definition_tlp1_keeps_well_known_id() -> None:
    raw = _base(
        "marking-definition",
        "marking-definition--34098fce-860f-48ae-8e50-ebd3cc5e41da",
        definition_type="tlp",
        definition={"tlp": "green"},
    )
    result = process(raw)
    assert result.value == "tlp:green"
    assert (
        result.platform_id == "marking-definition--34098fce-860f-48ae-8e50-ebd3cc5e41da"
    )


def test_marking_definition_tlp2_keeps_well_known_id() -> None:
    raw = _base(
        "marking-definition",
        "marking-definition--939a9414-2ddd-4d32-a0cd-375ea402b003",
        name="TLP:AMBER+STRICT",
        extensions={
            TLP_2_EXTENSION_ID: {
                "extension_type": "property-extension",
                "tlp_2_0": "amber+strict",
            }
        },
    )
    result = process(raw)
    assert result.value == "tlp:amber+strict"
    assert (
        result.platform_id == "marking-definition--939a9414-2ddd-4d32-a0cd-375ea402b003"
    )


def test_marking_definition_statement_gets_deterministic_id() -> None:
    raw = _base(
        "marking-definition",
        "marking-definition--original",
        definition_type="statement",
        definition={"statement": "Internal use only"},
    )
    result = process(raw)
    assert result.value == "statement:Internal use only"
    assert result.platform_id == platform_id(
        "marking-definition", "statement:Internal use only"
    )
    assert result.other_stix_ids == ["marking-definition--original"]


def test_marking_definition_without_definition_raises() -> None:
    raw = _base("marking-definition", "marking-definition--x")
    with pytest.raises(ValueError):
        process(raw)


def test_platform_id_matches_process_for_identity() -> None:
    raw = _base("identity", "identity--original", name="ACME Corp")
    assert process(raw).platform_id == platform_id("identity", "ACME Corp")


def test_platform_id_maps_tlp_value_to_tlp2_id() -> None:
    assert platform_id("marking-definition", "tlp:red") == (
        "marking-definition--e828b379-4e03-4974-9ac4-e53a884c97c1"
    )


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


# --- SROs ---


def _rel(**extra: object) -> dict:  # type: ignore[type-arg]
    fields: dict[str, object] = {
        "relationship_type": "uses",
        "source_ref": "malware--src",
        "target_ref": "attack-pattern--tgt",
    }
    fields.update(extra)
    return _base("relationship", "relationship--original", **fields)


def test_relationship_value_and_id() -> None:
    result = process(_rel())
    assert result.value == "malware--src uses attack-pattern--tgt"
    assert result.platform_id.startswith("relationship--")
    assert result.other_stix_ids == ["relationship--original"]
    assert result.object["id"] == result.platform_id


def test_relationship_deterministic_id_ignores_original_id() -> None:
    r1 = process(_rel())
    r2 = process({**_rel(), "id": "relationship--other"})
    assert r1.platform_id == r2.platform_id


@pytest.mark.parametrize(
    "change",
    [
        {"relationship_type": "targets"},
        {"source_ref": "malware--other"},
        {"target_ref": "attack-pattern--other"},
    ],
)
def test_relationship_id_depends_on_triple(change: dict[str, str]) -> None:
    assert process(_rel()).platform_id != process(_rel(**change)).platform_id


@pytest.mark.parametrize("missing", ["relationship_type", "source_ref", "target_ref"])
def test_relationship_missing_required_field_raises(missing: str) -> None:
    raw = _rel()
    del raw[missing]
    with pytest.raises(KeyError):
        process(raw)


def test_relationship_refs_rewritten_through_id_map() -> None:
    id_map = {
        "malware--src": "malware--platform",
        "attack-pattern--tgt": "attack-pattern--platform",
    }
    raw = _rel()
    result = process(raw, id_map)
    assert result.object["source_ref"] == "malware--platform"
    assert result.object["target_ref"] == "attack-pattern--platform"
    assert result.value == "malware--platform uses attack-pattern--platform"
    # The ID is derived from the rewritten refs, and the input is not mutated
    expected = process(
        _rel(source_ref="malware--platform", target_ref="attack-pattern--platform")
    )
    assert result.platform_id == expected.platform_id
    assert raw["source_ref"] == "malware--src"


def test_relationship_refs_missing_from_id_map_kept() -> None:
    result = process(_rel(), {"malware--src": "malware--platform"})
    assert result.object["source_ref"] == "malware--platform"
    assert result.object["target_ref"] == "attack-pattern--tgt"


def test_relationship_without_id_map_keeps_refs() -> None:
    result = process(_rel())
    assert result.object["source_ref"] == "malware--src"
    assert result.object["target_ref"] == "attack-pattern--tgt"


def test_id_map_does_not_touch_non_ref_objects() -> None:
    raw = _base("malware", "malware--original", name="WannaCry", is_family=False)
    assert process(raw, {"malware--original": "malware--x"}) == process(raw)


# --- More SCOs ---


@pytest.mark.parametrize(
    ("type_", "fields", "value"),
    [
        ("ipv6-addr", {"value": "2001:db8::1"}, "2001:db8::1"),
        ("mac-addr", {"value": "00:00:5e:00:53:01"}, "00:00:5e:00:53:01"),
        ("email-addr", {"value": "a@example.com"}, "a@example.com"),
        ("autonomous-system", {"number": 64496}, "64496"),
        ("file", {"hashes": {"MD5": "m", "SHA-256": "s"}, "name": "a"}, "s"),
        ("file", {"name": "evil.exe"}, "evil.exe"),
        ("artifact", {"hashes": {"SHA-1": "h"}}, "h"),
        ("directory", {"path": "/tmp"}, "/tmp"),
        ("mutex", {"name": "Global\\x"}, "Global\\x"),
        (
            "software",
            {"vendor": "ACME", "name": "Tool", "version": "1.0"},
            "ACME Tool 1.0",
        ),
        ("user-account", {"account_type": "unix", "user_id": "1001"}, "unix 1001"),
        (
            "windows-registry-key",
            {"key": "HKEY_LOCAL_MACHINE\\x"},
            "HKEY_LOCAL_MACHINE\\x",
        ),
        ("x509-certificate", {"serial_number": "01:02"}, "01:02"),
        ("email-message", {"subject": "Invoice", "is_multipart": False}, "Invoice"),
        (
            "network-traffic",
            {"dst_ref": "ipv4-addr--d", "dst_port": 443, "protocols": ["tcp"]},
            "* -> ipv4-addr--d:443 tcp",
        ),
    ],
)
def test_sco_values(type_: str, fields: dict[str, object], value: str) -> None:
    result = process(_base(type_, f"{type_}--original", **fields))
    assert result.value == value
    assert (
        result.platform_id
        == process(_base(type_, f"{type_}--other", **fields)).platform_id
    )


def test_sco_without_identifying_field_raises() -> None:
    with pytest.raises(ValueError, match="hashes.SHA-256"):
        process(_base("file", "file--original", size=10))


def test_network_traffic_refs_rewritten_through_id_map() -> None:
    raw = _base("network-traffic", "network-traffic--o", dst_ref="ipv4-addr--orig")
    result = process(raw, {"ipv4-addr--orig": "ipv4-addr--platform"})
    assert result.object["dst_ref"] == "ipv4-addr--platform"
    assert (
        result.platform_id
        == process({**raw, "dst_ref": "ipv4-addr--platform"}).platform_id
    )


# --- Occurrences: ID from the original ID ---

OCCURRENCES = [
    _base("report", "report--1", name="Phishing", object_refs=["x--1"]),
    _base(
        "grouping", "grouping--1", context="suspicious-activity", object_refs=["x--1"]
    ),
    _base("note", "note--1", content="text", object_refs=["x--1"]),
    _base("opinion", "opinion--1", opinion="agree", object_refs=["x--1"]),
    _base("observed-data", "observed-data--1", first_observed="2024-01-01T00:00:00Z"),
    _base("sighting", "sighting--1", sighting_of_ref="indicator--1"),
    _base("process", "process--1", pid=4),
    _base("x-misp-attribute", "x-misp-attribute--1", x_misp_value="free text"),
]


@pytest.mark.parametrize("raw", OCCURRENCES, ids=lambda raw: raw["type"])
def test_occurrence_id_derives_from_original_id(raw: dict[str, object]) -> None:
    result = process(raw)
    expected = f"{raw['type']}--{uuid.uuid5(STIX_NAMESPACE, json.dumps({'original_id': raw['id']}, separators=(',', ':')))}"
    assert result.platform_id == expected
    # Another occurrence with the same content stays separate ...
    assert process({**raw, "id": f"{raw['type']}--2"}).platform_id != expected
    # ... and an edit keeps the ID.
    assert process({**raw, "description": "edited"}).platform_id == expected


@pytest.mark.parametrize(
    ("raw", "value"),
    [
        (
            _base(
                "grouping", "grouping--1", name="Event", context="suspicious-activity"
            ),
            "Event",
        ),
        (
            _base("grouping", "grouping--1", context="suspicious-activity"),
            "suspicious-activity",
        ),
        (
            _base("sighting", "sighting--1", sighting_of_ref="indicator--1"),
            "indicator--1",
        ),
        (
            _base("process", "process--1", command_line="cmd.exe /c x", pid=4),
            "cmd.exe /c x",
        ),
        (_base("process", "process--1"), "process--1"),
        (
            _base("x-misp-object", "x-misp-object--1", name="bank-account"),
            "bank-account",
        ),
        (_base("x-misp-attribute", "x-misp-attribute--1", x_misp_value="text"), "text"),
        (_base("x-custom", "x-custom--1"), "x-custom--1"),
    ],
)
def test_occurrence_values(raw: dict[str, object], value: str) -> None:
    assert process(raw).value == value


def test_platform_id_rejects_occurrence_types() -> None:
    with pytest.raises(ValueError, match="report"):
        platform_id("report", "Phishing")
