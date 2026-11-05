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
    model_validator,
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

class BaseCollectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    # Names the env var holding this collector's shared secret for the
    # platform's /collectors API: STIX_HUB_COLLECTOR_SECRETS__<secret_env>.
    secret_env: str = Field(min_length=1)
    target_bucket: str
    poll_interval: PositiveFloat = 300
    max_retries: PositiveInt = 3
    shutdown_timeout: PositiveFloat = 30


class TaxiiCollectorConfig(BaseCollectorConfig):
    type: Literal["taxii"] = "taxii"
    remote_url: str
    remote_collection_id: str
    credentials_env: str | None = None


class MispCollectorConfig(BaseCollectorConfig):
    type: Literal["misp"]
    misp_url: str
    # Names the env var holding the MISP API key:
    # STIX_HUB_COLLECTOR_API_KEYS__<api_key_env>.
    api_key_env: str = Field(min_length=1)
    verify_tls: bool = True
    ca_bundle: str | None = None
    timeout: PositiveFloat = 120
    initial_lookback_days: PositiveInt | None = None
    overlap_seconds: int = Field(default=3600, ge=0)
    tags: list[str] = Field(default_factory=list)
    org: str | None = None
    page_size: PositiveInt = 100
    submit_batch_size: PositiveInt = 500

    @model_validator(mode="after")
    def _check_tls(self) -> "MispCollectorConfig":
        if self.ca_bundle is not None and not self.verify_tls:
            raise ValueError("ca_bundle and verify_tls: false are mutually exclusive")
        return self


def _collector_type(value: Any) -> str:
    """Discriminator: collectors without a ``type`` are TAXII collectors."""
    if isinstance(value, dict):
        return str(value.get("type", "taxii"))
    return str(getattr(value, "type", "taxii"))


CollectorConfig = Annotated[
    Annotated[TaxiiCollectorConfig, Tag("taxii")]
    | Annotated[MispCollectorConfig, Tag("misp")],
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
