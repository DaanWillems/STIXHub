from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, RootModel


@dataclass
class Bucket:
    id: int
    name: str


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


class TaxiiCollectionMediaTypes(str, Enum):
    STIX_JSON_2_1 = "application/stix+json;version=2.1"


class TaxiiCollectionsRootResponseModel(BaseModel):
    model_config = ConfigDict(title="TaxiiCollectionsRootResponse")
    collections: list[TaxiiCollectionModel]


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


class TaxiiObjectResponseModel(BaseModel):
    model_config = ConfigDict(title="TaxiiObjectResponse")
    objects: list[dict]
