from dataclasses import dataclass
from datetime import datetime

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