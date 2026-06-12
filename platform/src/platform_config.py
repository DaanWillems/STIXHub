from models.domain import (
    BucketConfig,
    BucketMode,
    CollectionConfig,
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
