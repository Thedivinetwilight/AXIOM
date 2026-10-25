"""New operator shell and local manual. No credentials are embedded in pages."""

from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, PlainTextResponse

from app.api.auth import loopback_or_bearer


router = APIRouter()
ROOT = Path(__file__).resolve().parents[3]
PAGE = ROOT / "app" / "static" / "terminal" / "index.html"
MANUAL = ROOT / "USER_MANUAL.md"


@router.get("/terminal", response_class=HTMLResponse, include_in_schema=False,
            dependencies=[Depends(loopback_or_bearer)])
def terminal():
    if not PAGE.is_file():
        return HTMLResponse("AXIOM operator assets are missing", status_code=500)
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))


@router.get("/manual", include_in_schema=False, dependencies=[Depends(loopback_or_bearer)])
def manual():
    if not MANUAL.is_file():
        return PlainTextResponse("User manual is missing from this installation", status_code=500)
    return PlainTextResponse(MANUAL.read_text(encoding="utf-8"))
