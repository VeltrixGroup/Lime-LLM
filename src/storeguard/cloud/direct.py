"""In-process "agent" for one-port mode (``storeguard cloud --with-dashboard``).

Same surface as :class:`storeguard.cloud.agent_client.CloudClient`, but calls
:mod:`storeguard.cloud.services` straight on the database, scoped to one
tenant (the store of the cabinet user who started the cameras). No agent key
and no HTTP round trip.
"""

from __future__ import annotations

import base64
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload, sessionmaker

from storeguard.cloud import services
from storeguard.cloud.models import Camera, Event, LimeCrmConfig, TelegramConfig, Tenant


class DirectCloud:
    def __init__(self, session_factory: sessionmaker, tenant_id: str, base_url: str) -> None:
        self._sf = session_factory
        self.tenant_id = tenant_id
        self._base_url = base_url

    def _deliver_async(self, event_id: str) -> None:
        threading.Thread(
            target=services.deliver_incident,
            args=(self._sf, event_id, self._base_url),
            daemon=True,
        ).start()

    def get_config(self) -> dict[str, Any]:
        with self._sf() as db:
            tenant = db.get(Tenant, self.tenant_id)
            cams = db.scalars(
                select(Camera)
                .where(Camera.tenant_id == self.tenant_id)
                .options(selectinload(Camera.zones))
                .order_by(Camera.created_at)
            ).all()
            return {
                "tenant_id": self.tenant_id,
                "tenant_name": tenant.name if tenant else "",
                "cameras": [
                    {
                        "id": c.id, "name": c.name, "source": c.source,
                        "process_every": c.process_every, "enabled": c.enabled,
                        "zones": [{"name": z.name, "points": z.points} for z in c.zones],
                    }
                    for c in cams
                ],
                **services.agent_config_extras(db, self.tenant_id),
            }

    def heartbeat(self) -> dict[str, Any]:
        return {"ok": True, "tenant_id": self.tenant_id}

    def send_event(self, kind: str, **kw: Any) -> dict[str, Any] | None:
        ts = kw.pop("ts", None)
        with self._sf() as db:
            ev = services.record_event(
                db, self.tenant_id, kind=kind,
                ts=datetime.fromisoformat(ts) if isinstance(ts, str) else ts, **kw,
            )
            if ev is None:
                return None
            crm = db.get(LimeCrmConfig, self.tenant_id)
            if crm is not None and crm.enabled and crm.webhook_url:
                payload = services.webhook_payload(ev)
                threading.Thread(
                    target=services.lime_crm.send_notification,
                    args=(crm.webhook_url, crm.auth_header, crm.auth_token, payload),
                    daemon=True,
                ).start()
            services.schedule_delivery(db, self._sf, ev, self._base_url)
            return {"id": ev.id}

    def upload_clip(self, event_id: str, path: str | Path) -> dict[str, Any]:
        with self._sf() as db:
            ev = db.scalar(select(Event).where(Event.id == event_id, Event.tenant_id == self.tenant_id))
            if ev is None:
                return {}
            services.attach_clip(db, ev, str(Path(path).resolve()), "video/mp4")
        return {"id": event_id}

    def push_stats(self, payload: dict[str, Any]) -> None:
        with self._sf() as db:
            services.record_stats(db, self.tenant_id, payload)

    def push_candidate(self, payload: dict[str, Any]) -> None:
        payload = dict(payload)
        with self._sf() as db:
            services.record_candidate(db, self.tenant_id, payload)

    def checkout(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._sf() as db:
            ev = services.check_checkout(
                db, self.tenant_id,
                ts=datetime.fromisoformat(payload["ts"]),
                camera_id=payload.get("camera_id"),
                person_id=payload.get("person_id"),
                register=payload.get("register", ""),
            )
            if ev is not None:
                self._deliver_async(ev.id)
            return {"incident_id": ev.id if ev else None}


def encode_crop(jpeg: bytes | None) -> str | None:
    return base64.b64encode(jpeg).decode("ascii") if jpeg else None
