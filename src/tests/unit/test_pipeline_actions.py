from datetime import UTC, datetime
from typing import Any

import pytest

from database.repositories.bucket import InMemoryBucketRepository
from models.domain import PipelineConfig, StixEntity
from pipeline.pipeline import (
    PipelineExecutionError,
    PipelineExecutor,
    PipelineValidationError,
)
from pipeline.refs import RefResolver


def _entity(type_: str, **fields: Any) -> StixEntity:
    stix_id = f"{type_}--00000000-0000-4000-8000-000000000001"
    now = datetime.now(UTC)
    return StixEntity(
        id=1,
        bucket_id=1,
        stix_id=stix_id,
        type=type_,
        spec_version="2.1",
        creator="test",
        value="v",
        platform_modified=now,
        platform_created=now,
        object={"id": stix_id, "type": type_, **fields},
    )


def _pipeline(*steps: dict[str, Any]) -> PipelineExecutor:
    return PipelineExecutor(
        PipelineConfig.model_validate(
            {"name": "p", "role": "r", "sources": [], "sinks": [], "steps": list(steps)}
        )
    )


async def _run(pipeline: PipelineExecutor, entity: StixEntity) -> dict[str, Any]:
    [out] = await pipeline.execute_pipeline(
        [entity], RefResolver(InMemoryBucketRepository(), [])
    )
    return out.object


# The pipeline from platform_config.yaml that exposed the bug.
TEST_THEN_BANAAN = _pipeline(
    {"action": "add", "field": "labels", "value": "test"},
    {
        "action": "add",
        "field": "labels",
        "value": "banaan",
        "condition": {"field": "type", "type": "eq", "value": "campaign"},
    },
)


async def test_add_to_missing_field_creates_list_and_chains() -> None:
    assert (await _run(TEST_THEN_BANAAN, _entity("campaign")))["labels"] == [
        "test",
        "banaan",
    ]
    assert (await _run(TEST_THEN_BANAAN, _entity("attack-pattern")))["labels"] == [
        "test"
    ]


async def test_add_appends_without_duplicates() -> None:
    pipeline = _pipeline(
        {"action": "add", "field": "labels", "value": "a"},
        {"action": "add", "field": "labels", "value": "b"},
    )
    assert (await _run(pipeline, _entity("malware", labels=["a"])))["labels"] == [
        "a",
        "b",
    ]


async def test_add_to_wrong_shape_raises() -> None:
    pipeline = _pipeline({"action": "add", "field": "labels", "value": "a"})
    with pytest.raises(PipelineExecutionError, match="expected a list, got str"):
        await _run(pipeline, _entity("malware", labels="x"))


async def test_set_missing_and_existing_scalar() -> None:
    pipeline = _pipeline({"action": "set", "field": "confidence", "value": "80"})
    assert (await _run(pipeline, _entity("malware")))["confidence"] == "80"
    assert (await _run(pipeline, _entity("malware", confidence=10)))["confidence"] == "80"


async def test_delete_from_list() -> None:
    pipeline = _pipeline({"action": "delete", "field": "labels", "value": "a"})
    assert (await _run(pipeline, _entity("malware", labels=["a", "b"])))["labels"] == [
        "b"
    ]
    # A value that isn't there is a no-op.
    assert (await _run(pipeline, _entity("malware", labels=["b"])))["labels"] == ["b"]


async def test_delete_scalar_field_and_missing_field() -> None:
    pipeline = _pipeline({"action": "delete", "field": "confidence", "value": ""})
    assert "confidence" not in await _run(pipeline, _entity("malware", confidence=10))
    assert "confidence" not in await _run(pipeline, _entity("malware"))


@pytest.mark.parametrize(
    ("step", "message"),
    [
        (
            {"action": "set", "field": "labels", "value": "a"},
            "Action set is not allowed on list field labels",
        ),
        (
            {"action": "add", "field": "confidence", "value": "1"},
            "Action add is not allowed on scalar field confidence",
        ),
        (
            {"action": "replace", "field": "confidence", "value": "1"},
            "Unknown action 'replace'",
        ),
        (
            {"action": "add", "field": "name", "value": "x"},
            "Field 'name' cannot be modified by pipelines",
        ),
    ],
)
def test_invalid_steps_rejected_at_startup(step: dict[str, Any], message: str) -> None:
    with pytest.raises(PipelineValidationError, match=message):
        _pipeline(step)


# --- AND / OR conditions ---


def _tag_when(condition: dict[str, Any]) -> PipelineExecutor:
    return _pipeline(
        {"action": "add", "field": "labels", "value": "hit", "condition": condition}
    )


def _eq(field: str, value: str) -> dict[str, Any]:
    return {"field": field, "type": "eq", "value": value}


async def _hit(pipeline: PipelineExecutor, entity: StixEntity) -> bool:
    return "hit" in (await _run(pipeline, entity)).get("labels", [])


async def test_and_condition() -> None:
    pipeline = _tag_when(
        {"type": "and", "condition": [_eq("type", "campaign"), _eq("name", "X")]}
    )
    assert await _hit(pipeline, _entity("campaign", name="X"))
    assert not await _hit(pipeline, _entity("campaign", name="Y"))


async def test_or_condition() -> None:
    pipeline = _tag_when(
        {"type": "or", "condition": [_eq("type", "campaign"), _eq("type", "malware")]}
    )
    assert await _hit(pipeline, _entity("malware"))
    assert not await _hit(pipeline, _entity("tool"))


async def test_nested_conditions() -> None:
    # campaign AND (name == X OR labels contains apt)
    pipeline = _tag_when(
        {
            "type": "and",
            "condition": [
                _eq("type", "campaign"),
                {
                    "type": "or",
                    "condition": [
                        _eq("name", "X"),
                        {"field": "labels", "type": "contains", "value": "apt"},
                    ],
                },
            ],
        }
    )
    assert await _hit(pipeline, _entity("campaign", name="Y", labels=["apt"]))
    assert await _hit(pipeline, _entity("campaign", name="X"))
    assert not await _hit(pipeline, _entity("campaign", name="Y", labels=[]))
    assert not await _hit(pipeline, _entity("malware", name="X"))


async def test_empty_groups() -> None:
    assert await _hit(_tag_when({"type": "and", "condition": []}), _entity("tool"))
    assert not await _hit(_tag_when({"type": "or", "condition": []}), _entity("tool"))


def test_unsupported_condition_type_rejected_at_startup() -> None:
    nested_greater = {
        "type": "and",
        "condition": [{"field": "confidence", "type": "greater", "value": "50"}],
    }
    with pytest.raises(
        PipelineValidationError, match="Condition type 'greater' is not supported"
    ):
        _tag_when(nested_greater)
