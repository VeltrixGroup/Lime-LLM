"""Extra edge-facing endpoints (agent key auth) and the POS receipt webhook."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from storeguard.cloud import services
from storeguard.cloud.auth import current_agent
from storeguard.cloud.db import get_db
from storeguard.cloud.models import AgentKey, PosReceipt, Tenant


class StatsIn(BaseModel):
    visitors: list[dict] = []
    employees: list[dict] = []
    cameras: list[dict] | None = None
    gpu: float | None = None


class CandidateIn(BaseModel):
    person_id: str = Field(min_length=1, max_length=64)
    zone: str = Field(default="", max_length=120)
    embedding: list[float] = Field(min_length=8, max_length=4096)
    crop_jpeg_b64: str | None = Field(default=None, max_length=2_000_000)
    seen_count: int = Field(default=1, ge=1, le=10_000)


class CheckoutIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ts: datetime
    camera_id: str | None = None
    person_id: str | None = Field(default=None, max_length=64)
    register_name: str = Field(default="", max_length=60, alias="register")


class ReceiptIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    #: Register / till name, e.g. "Касса 2" (JSON key "register").
    register_name: str = Field(default="", max_length=60, alias="register")
    kind: str = Field(default="sale", pattern="^(sale|refund)$")
    amount: float | None = None
    ts: datetime | None = None
    #: Refunds only: did the customer show the original receipt?
    has_receipt: bool = True


def build_agent_router(session_factory, public_base_url: str) -> APIRouter:
    r = APIRouter()

    def _deliver(background: BackgroundTasks, ev) -> None:
        # No clip will ever come for these (POS / checkout cross-checks).
        if ev is not None:
            background.add_task(services.deliver_incident, session_factory, ev.id, public_base_url)

    @r.post("/api/agent/stats")
    def agent_stats(
        body: StatsIn,
        db: Session = Depends(get_db),
        agent: AgentKey = Depends(current_agent),
    ) -> dict:
        try:
            services.record_stats(db, agent.tenant_id, body.model_dump(exclude_none=True))
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"bad stats payload: {exc}") from exc
        return {"ok": True}

    @r.post("/api/agent/candidates")
    def agent_candidate(
        body: CandidateIn,
        db: Session = Depends(get_db),
        agent: AgentKey = Depends(current_agent),
    ) -> dict:
        cand = services.record_candidate(db, agent.tenant_id, body.model_dump())
        return {"ok": True, "id": cand.id if cand else None}

    @r.post("/api/agent/checkout")
    def agent_checkout(
        body: CheckoutIn,
        background: BackgroundTasks,
        db: Session = Depends(get_db),
        agent: AgentKey = Depends(current_agent),
    ) -> dict:
        try:
            ev = services.check_checkout(
                db, agent.tenant_id, ts=body.ts, camera_id=body.camera_id,
                person_id=body.person_id, register=body.register_name,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _deliver(background, ev)
        return {"incident_id": ev.id if ev else None}

    @r.post("/api/pos/receipts", status_code=201)
    def pos_receipt(
        body: ReceiptIn,
        background: BackgroundTasks,
        x_pos_token: str = Header(default=""),
        db: Session = Depends(get_db),
    ) -> dict:
        """Receipt webhook for the store's POS (header ``X-POS-Token``)."""
        if not x_pos_token:
            raise HTTPException(status_code=401, detail="X-POS-Token required")
        tenant = db.scalar(select(Tenant).where(Tenant.pos_token == x_pos_token))
        if tenant is None:
            raise HTTPException(status_code=401, detail="invalid POS token")
        # A POS usually sends local wall-clock time without an offset.
        ts = services.local_to_utc(body.ts) if body.ts else services.utcnow()
        db.add(PosReceipt(tenant_id=tenant.id, register=body.register_name, kind=body.kind, amount=body.amount, ts=ts))
        db.commit()
        ev = None
        if body.kind == "refund" and not body.has_receipt:
            where = f" ({body.register_name})" if body.register_name else ""
            ev = services.record_event(
                db, tenant.id, kind="refund_no_receipt",
                message=f"Возврат без чека в руке{where}", ts=ts,
            )
            _deliver(background, ev)
        return {"ok": True, "incident_id": ev.id if ev else None}

    return r
