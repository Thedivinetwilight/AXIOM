"""ASGI factory entrypoint: uvicorn app.main:create_app --factory."""

from app.api.server import create_app

__all__ = ["create_app"]
