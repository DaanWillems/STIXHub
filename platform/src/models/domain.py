from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, PositiveInt


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


class CollectionConfig(BaseModel):
    id: str
    title: str
    description: str
    can_read: bool
    can_write: bool
    bucket_name: str


class PlatformConfig(BaseModel):
    buckets: list[BucketConfig]
    collections: list[CollectionConfig]


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


