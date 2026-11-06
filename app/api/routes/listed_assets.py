"""Authenticated, read-only listed-share/ETF identity and routing policy API."""

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from app.listed_assets import ListedAssetIdentity, ListedAssetService


router = APIRouter(prefix="/api/listed-assets")


class IdentityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=15)
    exchange: str = Field(min_length=2, max_length=24)
    currency: str = Field(min_length=3, max_length=3)
    product_type: str = Field(pattern=r"^(LISTED_SHARE|LISTED_ETF)$")


def service(request: Request) -> ListedAssetService:
    value = getattr(request.app.state, "listed_assets", None)
    if value is None:
        value = ListedAssetService()
        request.app.state.listed_assets = value
    return value


@router.get("/status")
def status(request: Request):
    return service(request).status()


@router.post("/routing-policy")
def routing_policy(body: IdentityRequest, request: Request):
    # This is a policy response only. It cannot connect, select a session,
    # create an intent, or call any execution provider.
    identity = ListedAssetIdentity(**body.model_dump())
    return service(request).routing_policy(identity)
