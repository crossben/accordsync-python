"""Accord for FastAPI: `accord_router(...)` mounts the sync endpoints on an application."""

from accordsync_server import PROTOCOL_VERSION

from .router import CompactionScheduler, accord_router, router_server

__all__ = ["PROTOCOL_VERSION", "CompactionScheduler", "accord_router", "router_server"]
