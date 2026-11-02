"""Translate STIX refs to readable values for pipelines, and back.

Pipelines address ``created_by_ref`` and ``object_marking_refs`` by value
(an identity name, ``tlp:red``, ``statement:...``) instead of by STIX ID.
Refs are resolved only against the buckets the pipeline's role can read, so
a pipeline never acts on intel a reader of its sink might not be allowed to
see. TLP markings resolve from a built-in registry: they are public spec
constants, not intel, and a TLP condition must never fail silently because a
marking object was not ingested into a readable bucket.
"""

import copy
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any, TypeGuard

from database.repositories.bucket import BucketRepository
from models.domain import StixEntity
from processors.stix import TLP_VALUES_BY_ID, platform_id

logger = logging.getLogger(__name__)

AUTHOR_FIELD = "created_by_ref"
MARKINGS_FIELD = "object_marking_refs"

_STIX_ID = re.compile(
    r"^[a-z0-9-]+--[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def is_stix_id(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and _STIX_ID.match(value) is not None


@dataclass
class ResolvedEntity:
    """An entity copy whose ref fields hold readable values.

    ``originals`` maps ``(field, value)`` to the ref the value was read from,
    so values a pipeline leaves untouched are written back with their
    original ref.
    """

    entity: StixEntity
    originals: dict[tuple[str, str], str] = field(default_factory=dict)


class RefResolver:
    def __init__(self, repo: BucketRepository, readable_bucket_ids: list[int]) -> None:
        self._repo = repo
        self._bucket_ids = readable_bucket_ids
        self._authors: dict[str, str] = {}
        self._markings: dict[str, str] = {}

    async def prefetch(self, entities: list[StixEntity]) -> None:
        """Look up every author and marking ref of ``entities`` in one go."""
        author_refs = {
            ref
            for e in entities
            if is_stix_id(ref := e.object.get(AUTHOR_FIELD))
            and ref not in self._authors
        }
        marking_refs = {
            ref
            for e in entities
            for ref in e.object.get(MARKINGS_FIELD) or []
            if ref not in TLP_VALUES_BY_ID and ref not in self._markings
        }
        if author_refs:
            found = await self._repo.find_by_refs(
                self._bucket_ids, "identity", sorted(author_refs)
            )
            self._authors.update({ref: e.value for ref, e in found.items()})
        if marking_refs:
            found = await self._repo.find_by_refs(
                self._bucket_ids, "marking-definition", sorted(marking_refs)
            )
            self._markings.update({ref: e.value for ref, e in found.items()})

    def _marking_value(self, ref: str) -> str | None:
        return TLP_VALUES_BY_ID.get(ref) or self._markings.get(ref)

    def to_values(self, entity: StixEntity) -> ResolvedEntity:
        """Return a copy of ``entity`` with refs replaced by readable values.

        Refs that can't be resolved are kept as raw IDs. ``prefetch`` must
        have been called for ``entity`` first.
        """
        obj = copy.deepcopy(entity.object)
        resolved = ResolvedEntity(entity=replace(entity, object=obj))

        author_ref = obj.get(AUTHOR_FIELD)
        if is_stix_id(author_ref):
            name = self._authors.get(author_ref)
            if name is None:
                logger.warning(
                    "%s %s of %s is not in a bucket readable by this pipeline; "
                    "keeping the raw ID",
                    AUTHOR_FIELD, author_ref, entity.stix_id,
                )
            else:
                obj[AUTHOR_FIELD] = name
                resolved.originals[(AUTHOR_FIELD, name)] = author_ref

        if obj.get(MARKINGS_FIELD):
            values: list[str] = []
            for ref in obj[MARKINGS_FIELD]:
                value = self._marking_value(ref)
                if value is None:
                    logger.warning(
                        "marking %s of %s is not in a bucket readable by this "
                        "pipeline; keeping the raw ID",
                        ref, entity.stix_id,
                    )
                    value = ref
                else:
                    resolved.originals.setdefault((MARKINGS_FIELD, value), ref)
                if value not in values:
                    values.append(value)
            obj[MARKINGS_FIELD] = values

        return resolved

    def to_refs(self, resolved: ResolvedEntity) -> StixEntity:
        """Turn the readable values of ``resolved`` back into refs.

        Values present on input get their original ref back; new values get
        the deterministic platform ID, so no lookup is needed and nothing is
        created. Raw IDs pass through unchanged.
        """
        obj = resolved.entity.object

        def ref_for(field_name: str, stix_type: str, value: str) -> str:
            if is_stix_id(value):
                return value
            original = resolved.originals.get((field_name, value))
            return original if original is not None else platform_id(stix_type, value)

        author = obj.get(AUTHOR_FIELD)
        if isinstance(author, str):
            obj[AUTHOR_FIELD] = ref_for(AUTHOR_FIELD, "identity", author)

        markings = obj.get(MARKINGS_FIELD)
        if isinstance(markings, list):
            refs: list[str] = []
            for value in markings:
                ref = ref_for(MARKINGS_FIELD, "marking-definition", value)
                if ref not in refs:
                    refs.append(ref)
            obj[MARKINGS_FIELD] = refs
        elif isinstance(markings, str):
            # Adding a marking to an object without any yields a bare string.
            obj[MARKINGS_FIELD] = [ref_for(MARKINGS_FIELD, "marking-definition", markings)]

        return resolved.entity
