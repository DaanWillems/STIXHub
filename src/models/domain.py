from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal, Optional
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    PositiveFloat,
    PositiveInt,
    Tag,
)


class ProcessingStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    processed = "processed"


class BucketMode(str, Enum):
    append = "append"
    merge = "merge"


class BucketConfig(BaseModel):
    name: str
    mode: BucketMode


class RoleConfig(BaseModel):
    name: str
    can_read: list[str]
    can_write: list[str]


class CollectionConfig(BaseModel):
    id: str
    title: str
    description: str
    can_read: bool
    can_write: bool
    bucket_name: str


class PipelineBooleanType(str, Enum):
    AND = "and"
    OR = "or"


class PipelineConditionType(str, Enum):
    eq = "eq"
    not_eq = "not_eq"
    greater = "greater"
    lesser = "lesser"
    greater_or_equal = "greater_or_equal"
    lesser_or_eqal = "lesser_or_eqal"
    contains = "contains"
    not_contains = "not_contains"


class PipelineExpression(BaseModel):
    field: str
    type: PipelineConditionType
    value: str


class PipelineCondition(BaseModel):
    """AND / OR group of sub-conditions, which may themselves be groups."""

    type: PipelineBooleanType
    condition: list["PipelineCondition | PipelineExpression"]


PipelineCondition.model_rebuild()


class PipelineStepConfig(BaseModel):
    action: str
    field: str
    value: str
    condition: PipelineCondition | PipelineExpression | None = None


class PipelineConfig(BaseModel):
    name: str
    # Role the pipeline runs as: sources must be readable and sinks writable by
    # it, and refs are only resolved against buckets it can read.
    role: str
    sources: list[str]
    sinks: list[str]
    steps: list[PipelineStepConfig]


# Collector names end up in creators ("collector:<name>") and registrations
# ("<name>:push"), so they are restricted to a safe alphabet.
CollectorName = Annotated[str, Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")]


class TaxiiCollectorConfig(BaseModel):
    type: Literal["taxii"] = "taxii"
    name: CollectorName
    remote_url: str
    remote_collection_id: str
    credentials_env: str | None = None
    target_bucket: str
    poll_interval: PositiveFloat = 300
    max_retries: PositiveInt = 3
    shutdown_timeout: PositiveFloat = 30


class MispPullConfig(BaseModel):
    target_bucket: str
    published_only: bool = True
    initial_lookback_days: PositiveInt = 30
    tags: list[str] = Field(default_factory=list)
    orgs: list[str] = Field(default_factory=list)
    page_size: PositiveInt = 100


class MispPushConfig(BaseModel):
    source_bucket: str
    exclude_creators: list[str] = Field(default_factory=list)
    distribution: int = Field(default=0, ge=0, le=5)
    sharing_group_id: int | None = None
    publish: bool = False
    # The internal API returns at most 1000 entities per page.
    batch_size: int = Field(default=100, ge=1, le=1000)
    # Entities younger than this aren't read yet, so the cursor never passes a
    # row whose transaction is still committing.
    settle_seconds: float = Field(default=10, ge=0)
    # How long a report waits for objects it references to reach the bucket
    # (e.g. while a pipeline is still copying them) before it is pushed anyway.
    wait_for_refs_seconds: float = Field(default=120, ge=0)


class MispConnectorConfig(BaseModel):
    type: Literal["misp"]
    name: CollectorName
    url: str
    credentials_env: str
    # True, False, or the path to a CA bundle.
    verify_tls: bool | str = True
    poll_interval: PositiveFloat = 300
    max_retries: PositiveInt = 3
    shutdown_timeout: PositiveFloat = 30
    pull: MispPullConfig | None = None
    push: MispPushConfig | None = None

    @property
    def normalized_url(self) -> str:
        """``url`` without a trailing slash, scheme and host lower-cased."""
        scheme, sep, rest = self.url.rstrip("/").partition("://")
        host, slash, path = rest.partition("/")
        return f"{scheme.lower()}{sep}{host.lower()}{slash}{path}"


def _collector_type(value: Any) -> str:
    """Collectors without a ``type`` are TAXII collectors."""
    found = (
        value.get("type") if isinstance(value, dict) else getattr(value, "type", None)
    )
    return str(found or "taxii")


CollectorConfig = Annotated[
    Annotated[TaxiiCollectorConfig, Tag("taxii")]
    | Annotated[MispConnectorConfig, Tag("misp")],
    Discriminator(_collector_type),
]


class PlatformConfig(BaseModel):
    buckets: list[BucketConfig]
    collections: list[CollectionConfig]
    roles: list[RoleConfig]
    collectors: list[CollectorConfig] = Field(default_factory=list)
    pipelines: list[PipelineConfig]


@dataclass
class Bucket:
    name: str
    id: Optional[int] = None
    mode: BucketMode = field(default=BucketMode.append)


@dataclass
class StixEntity:
    id: int
    bucket_id: int
    stix_id: str
    type: str
    spec_version: str
    creator: str
    value: str
    platform_modified: datetime
    platform_created: datetime
    object: dict
    status: ProcessingStatus = field(default=ProcessingStatus.pending)
    other_stix_ids: list[str] = field(default_factory=list)
    # Name of the last pipeline that wrote this entity; None when ingested directly.
    pipeline: str | None = None


@dataclass
class Collector:
    id: int
    name: str
    bucket_id: int
    cursor: str | None = None


class MispEventOrigin(str, Enum):
    """Who owns a MISP event: STIXHub only ever writes events it owns."""

    misp = "misp"
    stixhub = "stixhub"


@dataclass
class MispEventSync:
    """What STIXHub knows about one event of one MISP instance.

    Records are shared by every connector of that instance, so one
    connector's pushes are never pulled back by another.
    """

    instance: str  # the instance's normalised URL
    event_uuid: str
    origin: MispEventOrigin
    # The event's MISP timestamp at the last sync; None while an add is pending.
    misp_timestamp: int | None = None


class MispEventSyncUpdate(BaseModel):
    origin: MispEventOrigin
    misp_timestamp: int | None = None


class MispEventRecord(BaseModel):
    """A sync record as the internal API returns it to a connector."""

    event_uuid: str
    origin: MispEventOrigin
    misp_timestamp: int | None = None


class MispEventSyncLookup(BaseModel):
    event_uuids: list[str]


class MispEventSyncLookupResponse(BaseModel):
    events: list[MispEventRecord]


class CollectorEntity(BaseModel):
    """An entity as the internal collector API hands it to a connector."""

    id: int
    stix_id: str
    type: str
    creator: str
    platform_created: datetime
    other_stix_ids: list[str]
    object: dict[str, Any]


class CollectorEntityPage(BaseModel):
    entities: list[CollectorEntity]


class CollectorEntityLookup(BaseModel):
    refs: list[str]


class CollectorEntityLookupResponse(BaseModel):
    # Keyed by the ref that was looked up.
    entities: dict[str, CollectorEntity]


class CollectorRegisterRequest(BaseModel):
    name: str = Field(min_length=1)
    bucket_name: str


class CollectorCursorUpdate(BaseModel):
    cursor: str = Field(min_length=1)


class CollectorResponse(BaseModel):
    id: int
    name: str
    bucket_id: int
    cursor: str | None = None


class TaxiiCollectionMediaTypes(str, Enum):
    STIX_JSON_2_1 = "application/stix+json;version=2.1"


class TaxiiCollectionModel(BaseModel):
    model_config = ConfigDict(title="TaxiiCollection")
    id: str
    title: str
    description: str
    can_read: bool
    can_write: bool
    media_types: list[TaxiiCollectionMediaTypes] = Field(
        default_factory=lambda: list(TaxiiCollectionMediaTypes)
    )


class TaxiiCollectionsRootResponseModel(BaseModel):
    model_config = ConfigDict(title="TaxiiCollectionsRootResponse")
    collections: list[TaxiiCollectionModel]


class TaxiiCollectionResponseModel(BaseModel):
    model_config = ConfigDict(title="TaxiiCollectionResponse")
    id: str
    title: str
    can_read: bool
    can_write: bool


class TaxiiDiscoveryResponseModel(BaseModel):
    model_config = ConfigDict(title="TaxiiRootResponse")
    title: str
    description: str | None = None
    contact: str | None = None
    default: str | None = None
    api_roots: list[str] | None


class TaxiiRootResponseModel(BaseModel):
    model_config = ConfigDict(title="TaxiiRootResponse")
    title: str
    description: str | None = None
    versions: list[TaxiiCollectionMediaTypes] = Field(
        default_factory=lambda: list(TaxiiCollectionMediaTypes)
    )
    max_content_length: PositiveInt


class TaxiiErrorModel(BaseModel):
    model_config = ConfigDict(title="TaxiiError")
    title: str
    description: str | None = None
    error_id: str | None = None
    error_code: str | None = None
    http_status: str | None = None
    details: dict[str, Any] | None = None


class TaxiiObjectResponseModel(BaseModel):
    model_config = ConfigDict(title="TaxiiObjectResponse")
    more: bool = False
    next: str | None = None
    objects: list[dict[str, Any]]


class TaxiiEnvelopeModel(BaseModel):
    model_config = ConfigDict(title="TaxiiEnvelope")
    objects: list[dict[str, Any]] = Field(default_factory=list)


class TaxiiStatusRef(BaseModel):
    id: str
    version: str | None = None
    message: str | None = None


class TaxiiWriteStatusModel(BaseModel):
    model_config = ConfigDict(title="TaxiiWriteStatus")
    id: str
    status: Literal["complete", "complete_with_errors"]
    request_timestamp: str
    total_count: int
    success_count: int
    failure_count: int
    pending_count: int = 0
    successes: list[TaxiiStatusRef] = Field(default_factory=list)
    failures: list[TaxiiStatusRef] = Field(default_factory=list)
    pendings: list[TaxiiStatusRef] = Field(default_factory=list)


@dataclass
class RoleConfig:
    name: str
    can_read: list[str]
    can_write: list[str]


@dataclass
class User:
    id: UUID
    email: str
    roles: list[str]
    created_at: datetime


class UserCreate(BaseModel):
    email: str
    roles: list[str]


class UserPatch(BaseModel):
    roles: list[str]


class UserResponse(BaseModel):
    id: UUID
    email: str
    roles: list[str]
    created_at: datetime


class UserCreateResponse(UserResponse):
    api_key: str
