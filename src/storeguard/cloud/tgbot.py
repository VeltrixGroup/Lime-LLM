"""Background Telegram bot loop for every tenant with a connected bot.

* ``/start <code>`` from the link on the Settings page binds that chat to a
  :class:`~storeguard.cloud.models.TelegramRecipient`.
* "Подтвердить" / "Ложная тревога" buttons under an incident message update
  the incident (only for recipients whose role has the ``resolve`` right).
* Incidents still "new" after ``remind_min`` minutes get one reminder.

Short-polls ``getUpdates`` (no webhook), so it works on a store PC without a
public address. One thread for all tenants; every step is best-effort.
"""

from __future__ import annotations

import secrets
import threading
from datetime import timedelta

from rich.console import Console
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from storeguard.cloud import services, telegram
from storeguard.cloud.auth import permissions_of
from storeguard.cloud.models import (
    INCIDENT_CONFIRMED,
    INCIDENT_FALSE,
    INCIDENT_NEW,
    Event,
    Membership,
    TelegramConfig,
    TelegramRecipient,
)

_console = Console(stderr=True)


def handle_update(db: Session, cfg: TelegramConfig, upd: dict) -> None:
    """Apply one Telegram update to the database (testable without a network)."""
    # The bot was added to (or wrote in) a group: that's the alerts group,
    # unless one is already set ("Проверить подключение" relies on this).
    member = upd.get("my_chat_member") or {}
    for chat in ((member.get("chat") or {}), ((upd.get("message") or {}).get("chat") or {})):
        if chat.get("type") in ("group", "supergroup") and not cfg.chat_id:
            cfg.chat_id = str(chat["id"])
            db.commit()
    msg = upd.get("message") or {}
    text = (msg.get("text") or "").strip()
    if text.startswith("/start"):
        parts = text.split(maxsplit=1)
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if len(parts) == 2 and chat_id:
            rec = db.scalar(
                select(TelegramRecipient).where(
                    TelegramRecipient.tenant_id == cfg.tenant_id,
                    TelegramRecipient.link_token == parts[1].strip(),
                )
            )
            if rec is not None:
                rec.chat_id = chat_id
                # One-time link: a forwarded/leaked link can't re-bind it later.
                rec.link_token = secrets.token_urlsafe(16)
                db.commit()
                telegram.send(cfg.bot_token, chat_id, services.tg_text(cfg, "linked"))
        return

    cb = upd.get("callback_query")
    if not cb:
        return
    data = cb.get("data") or ""
    action, _, event_id = data.partition(":")
    if action not in ("ok", "no") or not event_id:
        return
    chat_id = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
    from_id = str((cb.get("from") or {}).get("id", ""))
    ev = db.scalar(select(Event).where(Event.id == event_id, Event.tenant_id == cfg.tenant_id))
    if ev is None:
        return
    # Who pressed it: a linked recipient (private chat id == their user id).
    rec = db.scalar(
        select(TelegramRecipient).where(
            TelegramRecipient.tenant_id == cfg.tenant_id,
            TelegramRecipient.chat_id.in_([from_id, chat_id]),
        )
    )
    allowed = False
    if rec is not None:
        m = db.scalar(
            select(Membership).where(
                Membership.tenant_id == cfg.tenant_id, Membership.user_id == rec.user_id
            )
        )
        allowed = m is not None and "resolve" in permissions_of(db, m)
    if not allowed:
        telegram.answer_callback(cfg.bot_token, cb.get("id", ""), services.tg_text(cfg, "denied"))
        return
    ev.status = INCIDENT_CONFIRMED if action == "ok" else INCIDENT_FALSE
    ev.reviewed_by = rec.user_id
    ev.reviewed_at = services.utcnow()
    db.commit()
    telegram.answer_callback(
        cfg.bot_token,
        cb.get("id", ""),
        services.tg_text(cfg, "confirmed" if action == "ok" else "dismissed"),
    )
    message = cb.get("message") or {}
    if message.get("message_id") and chat_id:
        telegram.clear_buttons(cfg.bot_token, chat_id, message["message_id"])


def send_reminders(db: Session, cfg: TelegramConfig, base_url: str) -> int:
    """One reminder per incident still "new" after ``remind_min`` minutes."""
    if not cfg.remind_min:
        return 0
    cutoff = services.utcnow() - timedelta(minutes=cfg.remind_min)
    oldest = services.utcnow() - timedelta(days=1)
    rows = db.scalars(
        select(Event).where(
            Event.tenant_id == cfg.tenant_id,
            Event.status == INCIDENT_NEW,
            Event.reminded_at.is_(None),
            Event.created_at <= cutoff,
            Event.created_at >= oldest,
            Event.kind != "camera_offline",
        )
    ).all()
    sent = 0
    for ev in rows:
        targets = services.telegram_targets(db, cfg, ev)
        text = f"⏰ {services.tg_text(cfg, 'remind')}\n" + services.incident_text(db, cfg, ev, base_url)
        markup = None
        if cfg.buttons:
            markup = telegram.review_keyboard(
                ev.id, (services.tg_text(cfg, "ok"), services.tg_text(cfg, "no"))
            )
        for chat in targets:
            telegram.send(cfg.bot_token, chat, text, reply_markup=markup, silent=services._in_quiet_hours(cfg))
        ev.reminded_at = services.utcnow()
        sent += 1
    db.commit()
    return sent


class BotPoller:
    """Runs :func:`poll_once` every ``interval`` seconds on a daemon thread."""

    def __init__(self, session_factory: sessionmaker, base_url: str, interval: float = 3.0) -> None:
        self._sf = session_factory
        self._base_url = base_url
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def poll_once(self) -> None:
        with self._sf() as db:
            cfgs = db.scalars(
                select(TelegramConfig).where(
                    TelegramConfig.enabled.is_(True), TelegramConfig.bot_token != ""
                )
            ).all()
            for cfg in cfgs:
                try:
                    updates = telegram.get_updates(cfg.bot_token, cfg.update_offset)
                    for upd in updates:
                        cfg.update_offset = max(cfg.update_offset, int(upd.get("update_id", 0)) + 1)
                        handle_update(db, cfg, upd)
                    db.commit()
                    send_reminders(db, cfg, self._base_url)
                except Exception as exc:  # noqa: BLE001 - one bad tenant mustn't stop the rest
                    db.rollback()
                    _console.log(f"[red]telegram bot poll failed for a tenant: {exc}[/red]")

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001
                _console.log(f"[red]telegram bot loop error: {exc}[/red]")

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="telegram-bot", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

