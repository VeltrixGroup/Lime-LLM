"""Reverse proxy for the local ``storeguard dashboard`` process ("Live view").

Lets the cabinet serve live camera viewing under its own origin/port instead
of a second address the owner has to remember. The dashboard keeps running as
its own process — it has to: it's the thing actually connected to cameras, so
it must run on the store PC's LAN — this module just forwards HTTP and the
frame WebSocket to it, gated behind the cabinet's own login.

Single-tenant assumption: one cloud instance currently proxies to exactly one
dashboard (``settings.dashboard_upstream_url``). If this cloud ever serves
more than one store, this needs a per-tenant upstream instead of one fixed
URL for every tenant.
"""

from __future__ import annotations

import asyncio

import httpx
import websockets
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import RedirectResponse, Response
from starlette.websockets import WebSocketDisconnect

from storeguard.cloud.auth import current_membership

# Headers that are per-hop, not per-resource — forwarding them verbatim would
# either be meaningless (Host) or break the proxied response (Content-Length
# computed for the wrong body, Transfer-Encoding/Connection framing).
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}


def _clean_headers(headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


def build_live_proxy_router(upstream_base: str) -> APIRouter:
    """Build the ``/live/*`` proxy router forwarding to ``upstream_base``."""
    upstream_base = upstream_base.rstrip("/")
    ws_base = "ws" + upstream_base[len("http") :]  # http(s):// -> ws(s)://

    router = APIRouter()

    async def _proxy_http(request: Request, upstream_path: str) -> Response:
        url = f"{upstream_base}{upstream_path}"
        body = await request.body()
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                upstream_resp = await client.request(
                    request.method,
                    url,
                    headers=_clean_headers(request.headers),
                    content=body,
                    params=request.query_params,
                )
        except httpx.ConnectError as exc:
            raise HTTPException(
                status_code=502,
                detail=(
                    "Live view isn't running — start it with "
                    "`storeguard dashboard` on the store PC."
                ),
            ) from exc
        return Response(
            content=upstream_resp.content,
            status_code=upstream_resp.status_code,
            headers=_clean_headers(upstream_resp.headers),
        )

    @router.get("/live")
    def live_redirect() -> RedirectResponse:
        # Relative asset URLs in the proxied SPA's HTML (./assets/...) only
        # resolve correctly under a trailing slash — bounce bare "/live" to
        # "/live/" before ever serving that HTML.
        return RedirectResponse("/live/")

    @router.get("/live/")
    async def live_index(
        request: Request,
        _membership=Depends(current_membership),
    ) -> Response:
        return await _proxy_http(request, "/")

    @router.get("/live/assets/{path:path}")
    async def live_assets(path: str, request: Request) -> Response:
        # Built JS/CSS only — no user data, so no auth gate (same as the
        # cabinet's own /static/* assets, which are equally unauthenticated).
        return await _proxy_http(request, f"/assets/{path}")

    @router.api_route(
        "/live/api/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def live_api(
        path: str,
        request: Request,
        _membership=Depends(current_membership),
    ) -> Response:
        return await _proxy_http(request, f"/api/{path}")

    @router.websocket("/live/api/ws/frames")
    async def live_ws_frames(websocket: WebSocket) -> None:
        # WebSocket shares the same signed session cookie as the HTTP routes
        # (SessionMiddleware covers both scope types) — no DB round trip here,
        # just "is someone logged in", since this proxy is single-tenant.
        if not websocket.session.get("user_id"):
            await websocket.close(code=4401)
            return

        await websocket.accept()
        try:
            async with websockets.connect(f"{ws_base}/api/ws/frames") as upstream:

                async def client_to_upstream() -> None:
                    try:
                        while True:
                            message = await websocket.receive()
                            if message["type"] == "websocket.disconnect":
                                return
                            data = message.get("bytes")
                            if data is not None:
                                await upstream.send(data)
                                continue
                            text = message.get("text")
                            if text is not None:
                                await upstream.send(text)
                    except WebSocketDisconnect:
                        return

                async def upstream_to_client() -> None:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await websocket.send_bytes(message)
                        else:
                            await websocket.send_text(message)

                # Either direction ending (either side disconnected) ends the
                # proxy session — cancel whichever task is still running.
                tasks = [
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client()),
                ]
                _done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
        except (OSError, websockets.exceptions.WebSocketException):
            pass
        finally:
            try:
                await websocket.close()
            except RuntimeError:
                pass  # already closed

    return router
