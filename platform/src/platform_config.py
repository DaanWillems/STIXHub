from models.domain import (
    BucketConfig,
    BucketMode,
    CollectionConfig,
    RoleConfig,
    TaxiiCollectionModel,
)

BUCKET_CONFIGS: dict[str, BucketConfig] = {
    "Example collection": BucketConfig(
        name="Example collection", mode=BucketMode.append
    ),
}

COLLECTION_CONFIGS: dict[str, CollectionConfig] = {
    "70a16fcf-8146-2da8-be66-6ca6fb7280af": CollectionConfig(
        taxii_collection=TaxiiCollectionModel(
            id="70a16fcf-8146-2da8-be66-6ca6fb7280af",
            title="Example collection",
            description="test",
            can_read=True,
            can_write=True,
            media_types=[],
        ),
        bucket_name="Example collection",
    ),
}

ROLE_CONFIGS: dict[str, RoleConfig] = {
    "admin": RoleConfig(
        name="admin",
        can_read=["Example collection"],
        can_write=["Example collection"],
    ),
    "reader": RoleConfig(
        name="reader",
        can_read=["Example collection"],
        can_write=[],
    ),
    "writer": RoleConfig(
        name="writer",
        can_read=[],
        can_write=["Example collection"],
    ),
}
