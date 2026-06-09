from fastapi import APIRouter, Depends, Request
from models.domain import (
    TaxiiCollectionModel,
    TaxiiCollectionsRootResponseModel,
    TaxiiDiscoveryResponseModel,
    TaxiiRootResponseModel,
)


taxii2_router = APIRouter(prefix="/taxii2", tags=["TAXII 2.1"])

type CollectionsRepository = dict[str, TaxiiCollectionModel]


def get_dummy_collections() -> CollectionsRepository:
    return {
        "60a12bdb-9987-3qa8-be66-6ca6fb1234af": TaxiiCollectionModel(
            id="70a16fcf-8146-2da8-be66-6ca6fb7280af",
            title="Example collection",
            description="test",
            can_read=False,
            can_write=True,
            media_types=[],
        ),
    }


@taxii2_router.get("/")
def taxii_discovery(request: Request) -> TaxiiDiscoveryResponseModel:
    return TaxiiDiscoveryResponseModel(
        title="StixHub TAXII2.1 Server",
        description="This server is used for exchanging CTI with StixHub",
        contact="n.a",
        default=f"{request.url}root/",
        api_roots=[f"{request.url}root/"],
    )


@taxii2_router.get("/root/")
def taxii_root() -> TaxiiRootResponseModel:
    return TaxiiRootResponseModel(
        title="Root 1", description="Description root 1", max_content_length=1
    )


@taxii2_router.get("/root/collections/")
def taxii_collections_root(
    dummy_collections: CollectionsRepository = Depends(get_dummy_collections),
) -> TaxiiCollectionsRootResponseModel:
    return TaxiiCollectionsRootResponseModel(
        collections=list(dummy_collections.values())
    )
