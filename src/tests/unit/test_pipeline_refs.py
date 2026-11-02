import copy
from datetime import UTC, datetime
from typing import Any

import pytest

from database.repositories.bucket import InMemoryBucketRepository
from models.domain import Bucket, PipelineConfig, PlatformConfig, StixEntity
from pipeline.pipeline import PipelineExecutor, PipelineValidationError
from pipeline.pipeline_manager import PipelineManager
from pipeline.refs import RefResolver
from processors.stix import TLP_1_IDS, TLP_2_IDS, platform_id

ACME_ORIGINAL = "identity--11111111-1111-4111-8111-111111111111"
ACME_PLATFORM = platform_id("identity", "ACME Corp")
UNKNOWN_IDENTITY = "identity--22222222-2222-4222-8222-222222222222"
STATEMENT_ORIGINAL = "marking-definition--33333333-3333-4333-8333-333333333333"


def _entity(
    bucket_id: int, stix_id: str, type_: str, value: str, obj: dict[str, Any],
    other_stix_ids: list[str] | None = None,
) -> StixEntity:
    now = datetime.now(UTC)
    return StixEntity(
        id=0,
        bucket_id=bucket_id,
        stix_id=stix_id,
        type=type_,
        spec_version="2.1",
        creator="test",
        value=value,
        platform_modified=now,
        platform_created=now,
        object=obj,
        other_stix_ids=other_stix_ids or [],
    )


def _indicator(bucket_id: int, **refs: Any) -> StixEntity:
    stix_id = "indicator--44444444-4444-4444-8444-444444444444"
    return _entity(
        bucket_id, stix_id, "indicator", "[ipv4-addr:value = '1.2.3.4']",
        {"id": stix_id, "type": "indicator", **refs},
    )


async def _repo_with_acme(bucket_name: str) -> tuple[InMemoryBucketRepository, int]:
    """A repo whose `bucket_name` bucket holds the ACME identity and a statement marking."""
    repo = InMemoryBucketRepository()
    bucket = await repo.save(Bucket(name=bucket_name))
    assert bucket.id is not None
    await repo.add_entities(
        bucket.id,
        [
            _entity(
                bucket.id, ACME_PLATFORM, "identity", "ACME Corp",
                {"id": ACME_PLATFORM, "type": "identity", "name": "ACME Corp"},
                other_stix_ids=[ACME_ORIGINAL],
            ),
            _entity(
                bucket.id,
                platform_id("marking-definition", "statement:Internal use only"),
                "marking-definition", "statement:Internal use only", {},
                other_stix_ids=[STATEMENT_ORIGINAL],
            ),
        ],
    )
    return repo, bucket.id


def _pipeline(*steps: dict[str, Any]) -> PipelineExecutor:
    return PipelineExecutor(
        PipelineConfig.model_validate(
            {"name": "p", "role": "r", "sources": [], "sinks": [], "steps": list(steps)}
        )
    )


TAG_ACME = {
    "action": "add", "field": "labels", "value": "acme",
    "condition": {"field": "created_by_ref", "type": "eq", "value": "ACME Corp"},
}


async def test_author_in_readable_bucket_resolves_to_name() -> None:
    repo, bucket_id = await _repo_with_acme("readable")
    entity = _indicator(bucket_id, created_by_ref=ACME_ORIGINAL, labels=[])

    [out] = await _pipeline(TAG_ACME).execute_pipeline(
        [entity], RefResolver(repo, [bucket_id])
    )

    assert out.object["labels"] == ["acme"]
    assert out.object["created_by_ref"] == ACME_ORIGINAL


async def test_author_in_unreadable_bucket_stays_raw_id() -> None:
    repo, _ = await _repo_with_acme("red")
    green = await repo.save(Bucket(name="green"))
    assert green.id is not None
    entity = _indicator(green.id, created_by_ref=ACME_ORIGINAL, labels=[])

    # The role can only read "green", so the ACME identity in "red" is invisible.
    [out] = await _pipeline(TAG_ACME).execute_pipeline(
        [entity], RefResolver(repo, [green.id])
    )

    assert out.object["labels"] == []
    assert out.object["created_by_ref"] == ACME_ORIGINAL


async def test_unknown_author_raw_id_still_matches_condition() -> None:
    repo, bucket_id = await _repo_with_acme("readable")
    entity = _indicator(bucket_id, created_by_ref=UNKNOWN_IDENTITY, labels=[])
    step = {
        "action": "add", "field": "labels", "value": "unknown-author",
        "condition": {"field": "created_by_ref", "type": "eq", "value": UNKNOWN_IDENTITY},
    }

    [out] = await _pipeline(step).execute_pipeline(
        [entity], RefResolver(repo, [bucket_id])
    )

    assert out.object["labels"] == ["unknown-author"]
    assert out.object["created_by_ref"] == UNKNOWN_IDENTITY


async def test_tlp_resolves_without_marking_object() -> None:
    repo = InMemoryBucketRepository()
    bucket = await repo.save(Bucket(name="empty"))
    assert bucket.id is not None
    entity = _indicator(
        bucket.id, object_marking_refs=[TLP_1_IDS["tlp:red"]], labels=[]
    )
    step = {
        "action": "add", "field": "labels", "value": "red",
        "condition": {"field": "object_marking_refs", "type": "contains", "value": "tlp:red"},
    }

    [out] = await _pipeline(step).execute_pipeline(
        [entity], RefResolver(repo, [bucket.id])
    )

    assert out.object["labels"] == ["red"]
    assert out.object["object_marking_refs"] == [TLP_1_IDS["tlp:red"]]


async def test_round_trip_preserves_original_refs() -> None:
    repo, bucket_id = await _repo_with_acme("readable")
    refs = {
        "created_by_ref": ACME_ORIGINAL,
        "object_marking_refs": [TLP_1_IDS["tlp:green"], STATEMENT_ORIGINAL],
    }
    entity = _indicator(bucket_id, **copy.deepcopy(refs))

    [out] = await _pipeline().execute_pipeline([entity], RefResolver(repo, [bucket_id]))

    assert out.object == entity.object


async def test_new_values_get_deterministic_ids() -> None:
    repo, bucket_id = await _repo_with_acme("readable")
    entity = _indicator(bucket_id, object_marking_refs=[TLP_1_IDS["tlp:green"]])

    [out] = await _pipeline(
        {"action": "set", "field": "created_by_ref", "value": "Example Org"},
        {"action": "add", "field": "object_marking_refs", "value": "tlp:amber"},
        {"action": "add", "field": "object_marking_refs", "value": "statement:New"},
    ).execute_pipeline([entity], RefResolver(repo, [bucket_id]))

    assert out.object["created_by_ref"] == platform_id("identity", "Example Org")
    assert out.object["object_marking_refs"] == [
        TLP_1_IDS["tlp:green"],
        TLP_2_IDS["tlp:amber"],
        platform_id("marking-definition", "statement:New"),
    ]


async def test_marking_added_to_object_without_markings_is_a_list() -> None:
    repo = InMemoryBucketRepository()
    bucket = await repo.save(Bucket(name="b"))
    assert bucket.id is not None
    entity = _indicator(bucket.id)

    [out] = await _pipeline(
        {"action": "add", "field": "object_marking_refs", "value": "tlp:clear"},
    ).execute_pipeline([entity], RefResolver(repo, [bucket.id]))

    assert out.object["object_marking_refs"] == [TLP_2_IDS["tlp:clear"]]


async def test_source_entity_is_not_modified() -> None:
    repo, bucket_id = await _repo_with_acme("readable")
    entity = _indicator(
        bucket_id,
        created_by_ref=ACME_ORIGINAL,
        object_marking_refs=[TLP_1_IDS["tlp:red"]],
        labels=[],
    )
    before = copy.deepcopy(entity.object)

    await _pipeline(TAG_ACME).execute_pipeline([entity], RefResolver(repo, [bucket_id]))

    assert entity.object == before


# --- role validation ---


def _platform_config(
    can_read: list[str], can_write: list[str], pipeline_role: str = "refiner"
) -> PlatformConfig:
    return PlatformConfig.model_validate(
        {
            "buckets": [
                {"name": "raw", "mode": "append"},
                {"name": "refined", "mode": "merge"},
            ],
            "collections": [],
            "roles": [{"name": "refiner", "can_read": can_read, "can_write": can_write}],
            "pipelines": [
                {
                    "name": "p",
                    "role": pipeline_role,
                    "sources": ["raw"],
                    "sinks": ["refined"],
                    "steps": [],
                }
            ],
        }
    )


def test_pipeline_role_with_access_loads() -> None:
    config = _platform_config(can_read=["raw"], can_write=["refined"])
    assert len(PipelineManager(config).pipelines()) == 1


def test_pipeline_unknown_role_raises() -> None:
    config = _platform_config(["raw"], ["refined"], pipeline_role="missing")
    with pytest.raises(PipelineValidationError, match="unknown role 'missing'"):
        PipelineManager(config)


def test_pipeline_role_without_source_read_raises() -> None:
    with pytest.raises(PipelineValidationError, match="cannot read source bucket 'raw'"):
        PipelineManager(_platform_config(can_read=[], can_write=["refined"]))


def test_pipeline_role_without_sink_write_raises() -> None:
    with pytest.raises(PipelineValidationError, match="cannot write sink bucket 'refined'"):
        PipelineManager(_platform_config(can_read=["raw"], can_write=[]))
