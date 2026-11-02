from typing import Any

from models.domain import Bucket
from processors.stix import process
from database.repositories.bucket import InMemoryBucketRepository
from services.ingest import ingest_objects


def _malware(id_: str, name: str) -> dict[str, Any]:
    return {
        "type": "malware",
        "id": id_,
        "spec_version": "2.1",
        "name": name,
        "is_family": False,
    }


def _attack_pattern(id_: str, name: str) -> dict[str, Any]:
    return {"type": "attack-pattern", "id": id_, "spec_version": "2.1", "name": name}


def _relationship(id_: str, source: str, target: str) -> dict[str, Any]:
    return {
        "type": "relationship",
        "id": id_,
        "spec_version": "2.1",
        "relationship_type": "uses",
        "source_ref": source,
        "target_ref": target,
    }


async def _setup() -> tuple[InMemoryBucketRepository, Bucket]:
    repo = InMemoryBucketRepository()
    bucket = await repo.save(Bucket(name="b"))
    return repo, bucket


async def test_relationship_refs_resolved_within_batch() -> None:
    repo, bucket = await _setup()
    malware = _malware("malware--orig", "WannaCry")
    pattern = _attack_pattern("attack-pattern--orig", "Phishing")
    # Relationship deliberately listed before the objects it references
    rel = _relationship("relationship--orig", "malware--orig", "attack-pattern--orig")

    status = await ingest_objects(repo, bucket, [rel, malware, pattern], "test")

    assert status.success_count == 3
    stored = {e.type: e for e in await repo.get_entities(bucket_id=bucket.id)}
    rel_obj = stored["relationship"].object
    assert rel_obj["source_ref"] == stored["malware"].stix_id
    assert rel_obj["target_ref"] == stored["attack-pattern"].stix_id
    # The status report keeps the input order
    assert [s.id for s in status.successes] == [
        stored["relationship"].stix_id,
        stored["malware"].stix_id,
        stored["attack-pattern"].stix_id,
    ]


async def test_relationship_refs_resolved_from_bucket() -> None:
    repo, bucket = await _setup()
    await ingest_objects(
        repo,
        bucket,
        [
            _malware("malware--orig", "WannaCry"),
            _attack_pattern("attack-pattern--orig", "Phishing"),
        ],
        "test",
    )

    status = await ingest_objects(
        repo,
        bucket,
        [_relationship("relationship--orig", "malware--orig", "attack-pattern--orig")],
        "test",
    )

    assert status.success_count == 1
    by_type = {e.type: e for e in await repo.get_entities(bucket_id=bucket.id)}
    rel_obj = by_type["relationship"].object
    assert rel_obj["source_ref"] == by_type["malware"].stix_id
    assert rel_obj["target_ref"] == by_type["attack-pattern"].stix_id


async def test_same_fact_from_different_sources_gets_same_id() -> None:
    repo, bucket = await _setup()
    feed_a = [
        _malware("malware--a", "WannaCry"),
        _attack_pattern("attack-pattern--a", "Phishing"),
        _relationship("relationship--a", "malware--a", "attack-pattern--a"),
    ]
    feed_b = [
        _malware("malware--b", "WannaCry"),
        _attack_pattern("attack-pattern--b", "Phishing"),
        _relationship("relationship--b", "malware--b", "attack-pattern--b"),
    ]

    status_a = await ingest_objects(repo, bucket, feed_a, "a")
    status_b = await ingest_objects(repo, bucket, feed_b, "b")

    assert status_a.successes[2].id == status_b.successes[2].id


async def test_unresolvable_refs_are_kept() -> None:
    repo, bucket = await _setup()
    rel = _relationship(
        "relationship--orig", "malware--nowhere", "attack-pattern--nowhere"
    )

    status = await ingest_objects(repo, bucket, [rel], "test")

    assert status.success_count == 1
    [stored] = await repo.get_entities(bucket_id=bucket.id)
    assert stored.object["source_ref"] == "malware--nowhere"
    assert stored.stix_id == process(rel).platform_id


async def test_lookup_is_scoped_to_bucket() -> None:
    repo, bucket = await _setup()
    other = await repo.save(Bucket(name="other"))
    await ingest_objects(repo, other, [_malware("malware--orig", "WannaCry")], "test")

    await ingest_objects(
        repo,
        bucket,
        [_relationship("relationship--orig", "malware--orig", "attack-pattern--x")],
        "test",
    )

    [stored] = await repo.get_entities(bucket_id=bucket.id)
    assert stored.object["source_ref"] == "malware--orig"


async def test_invalid_relationship_reported_as_failure() -> None:
    repo, bucket = await _setup()
    rel = _relationship("relationship--bad", "malware--a", "attack-pattern--b")
    del rel["relationship_type"]

    status = await ingest_objects(
        repo, bucket, [rel, _malware("malware--a", "WannaCry")], "test"
    )

    assert status.status == "complete_with_errors"
    assert status.success_count == 1
    assert status.failures[0].id == "relationship--bad"
