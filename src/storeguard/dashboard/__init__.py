"""Local web dashboard: upload a video and watch person detection live."""

from __future__ import annotations

__all__ = ["create_app", "serve"]


def __getattr__(name: str):
    if name == "create_app":
        from storeguard.dashboard.app import create_app

        return create_app
    if name == "serve":
        from storeguard.dashboard.app import serve

        return serve
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
