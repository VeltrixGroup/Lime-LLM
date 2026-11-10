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
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.websockets import WebSocketDisconnect

from storeguard.cloud.auth import permissions_of, require_perm
from storeguard.cloud.models import Membership, User


def live_access(session_factory, session: dict) -> tuple[bool, str | None]:
    """(allowed, tenant_id): an active user, a member of the session's store,
    whose role holds "live". Checked on every request / socket, so a removed
    or disabled user loses the live view immediately, not when the cookie ends."""
    user_id, tenant_id = session.get("user_id"), session.get("tenant_id")
    if not user_id or not tenant_id or session_factory is None:
        return False, None
    with session_factory() as db:
        user = db.get(User, user_id)
        if user is None or not user.is_active:
            return False, None
        m = db.query(Membership).filter_by(user_id=user_id, tenant_id=tenant_id).one_or_none()
        if m is None or "live" not in permissions_of(db, m):
            return False, None
    return True, tenant_id

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


class LoginRequired:
    """ASGI wrapper: only logged-in cabinet users reach the wrapped app.

    Used when the detection dashboard runs inside the cloud process (mounted
    at ``/live``) instead of behind the proxy below. The cabinet's
    SessionMiddleware wraps the whole app, so ``scope["session"]`` is already
    decoded here. Built JS/CSS under ``assets/`` stays public, like
    ``/static/*``.
    """

    def __init__(self, app, session_factory=None) -> None:
        self.app = app
        self.session_factory = session_factory

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if "/assets/" in path:
            await self.app(scope, receive, send)
            return
        import anyio

        allowed, tenant_id = await anyio.to_thread.run_sync(
            live_access, self.session_factory, dict(scope.get("session", {}))
        )
        if allowed:
            # The dashboard runs cameras for *this* store (one-port mode).
            scope.setdefault("state", {})["cabinet_tenant_id"] = tenant_id
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4403})
            return
        if path.rstrip("/").endswith("/live"):
            # The page itself: back to the cabinet, which shows its login form.
            response = RedirectResponse("/")
        else:
            response = JSONResponse({"detail": "not logged in"}, status_code=401)
        await response(scope, receive, send)


def _clean_headers(headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


def build_live_proxy_router(upstream_base: str, session_factory=None) -> APIRouter:
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
        _membership=Depends(require_perm("live")),
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
        _membership=Depends(require_perm("live")),
    ) -> Response:
        return await _proxy_http(request, f"/api/{path}")

    @router.websocket("/live/api/ws/frames")
    async def live_ws_frames(websocket: WebSocket) -> None:
        # WebSocket shares the same signed session cookie as the HTTP routes
        # (SessionMiddleware covers both scope types) — no DB round trip here,
        # just "is someone logged in", since this proxy is single-tenant.
        import anyio

        allowed, _ = await anyio.to_thread.run_sync(
            live_access, session_factory, dict(websocket.session)
        )
        if not allowed:
            await websocket.close(code=4403)
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
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                for task in done:
                    # asyncio.wait() never raises on task failure — retrieve
                    # each result so a dropped upstream connection surfaces
                    # into the except below instead of being logged as
                    # "Task exception was never retrieved" once GC'd.
                    task.result()
        except (OSError, websockets.exceptions.WebSocketException):
            pass
        finally:
            try:
                await websocket.close()
            except RuntimeError:
                pass  # already closed

    return router
