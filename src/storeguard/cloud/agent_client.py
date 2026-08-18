"""HTTP client an edge agent uses to talk to the cloud control plane.

Depends only on ``requests`` (no torch/cv2), so the edge process can import it
cheaply.  The agent authenticates with a token minted in the cabinet
(``POST /api/agent-keys``) and sent as ``Authorization: Bearer <token>``.

Typical edge loop::

    client = CloudClient(server_url, token)
    client.heartbeat()                       # confirm enrollment
    cfg = client.get_config()                # pull cameras + zones
    ev = client.send_event("theft", camera_id=cam_id, message="…")
    client.upload_clip(ev["id"], "clip.mp4") # attach the short alarm clip
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import requests


class CloudClient:
    """Minimal client for the storeguard cloud agent API."""

    def __init__(self, server_url: str, token: str, timeout: float = 15.0) -> None:
        self.base = server_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def heartbeat(self) -> dict[str, Any]:
        """Confirm the token is valid; returns ``{ok, tenant_id}``."""
        r = self.session.post(
            f"{self.base}/api/agent/heartbeat", timeout=self.timeout
        )
        r.raise_for_status()
        return r.json()

    def get_config(self) -> dict[str, Any]:
        """Pull this tenant's cameras (with full sources) and zones."""
        r = self.session.get(f"{self.base}/api/agent/config", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def send_event(
        self,
        kind: str,
        *,
        message: str = "",
        camera_id: str | None = None,
        person_id: str | None = None,
        track_id: int | None = None,
        score: float | None = None,
        ts: str | None = None,
    ) -> dict[str, Any]:
        """Push one detection event (metadata); returns the created event.

        ``person_id`` is the cross-camera identity from the edge re-ID; sending
        it lets the cloud group every camera that saw the same person.
        """
        body: dict[str, Any] = {
            "kind": kind,
            "message": message,
            "camera_id": camera_id,
            "person_id": person_id,
            "track_id": track_id,
            "score": score,
        }
        if ts is not None:
            body["ts"] = ts
        r = self.session.post(
            f"{self.base}/api/agent/events", json=body, timeout=self.timeout
        )
        r.raise_for_status()
        return r.json()

    def upload_clip(self, event_id: str, path: str | Path) -> dict[str, Any]:
        """Attach a short alarm clip to an event."""
        p = Path(path)
        with p.open("rb") as fh:
            r = self.session.post(
                f"{self.base}/api/agent/events/{event_id}/clip",
                files={"clip": (p.name, fh)},
                timeout=max(self.timeout, 60.0),
            )
        r.raise_for_status()
        return r.json()
