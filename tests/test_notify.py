"""Tests for webhook notification delivery on unpaid-exit events."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from storeguard.alerts import AlertSink, event_payload
from storeguard.config import NotifyCfg, TelegramCfg
from storeguard.types import Event


def _event(kind: str = "exit_no_pay") -> Event:
    return Event(
        kind=kind,
        camera="hall-1",
        message="[hall-1] Suspected unpaid exit: person #3 …",
        ts=1_700_000_000.0,
        track_id=3,
        score=1.0,
        extra={"shelf_dwell_sec": 4.2, "checkout_dwell_sec": 0.1},
    )


def test_event_payload_shape() -> None:
    payload = event_payload(_event(), clip_path=Path("events/clips/x.mp4"))
    assert payload["kind"] == "exit_no_pay"
    assert payload["camera"] == "hall-1"
    assert payload["track_id"] == 3
    assert payload["extra"]["shelf_dwell_sec"] == 4.2
    assert payload["clip_path"] == "events/clips/x.mp4"
    assert "iso_time" in payload


def test_webhook_posts_exit_no_pay(tmp_path: Path) -> None:
    notify = NotifyCfg(
        enabled=True,
        url="https://api.example.com/v1/alerts",
        kinds=["exit_no_pay"],
        headers={"Authorization": "Bearer test"},
    )
    sink = AlertSink(TelegramCfg(), str(tmp_path), notify=notify)
    frame = np.zeros((48, 64, 3), dtype=np.uint8)

    with patch("storeguard.alerts.requests.post") as post:
        post.return_value = MagicMock(ok=True, status_code=200, text="ok")
        sink.handle(_event("exit_no_pay"), [frame], fps=10.0)

    assert post.called
    args, kwargs = post.call_args
    assert args[0] == "https://api.example.com/v1/alerts"
    assert kwargs["json"]["kind"] == "exit_no_pay"
    assert kwargs["json"]["track_id"] == 3
    assert kwargs["headers"]["Authorization"] == "Bearer test"
    assert kwargs["headers"]["Content-Type"] == "application/json"


def test_webhook_skips_other_kinds(tmp_path: Path) -> None:
    notify = NotifyCfg(enabled=True, url="https://api.example.com/v1/alerts", kinds=["exit_no_pay"])
    sink = AlertSink(TelegramCfg(), str(tmp_path), notify=notify)

    with patch("storeguard.alerts.requests.post") as post:
        sink.handle(_event("pocket"), [], fps=10.0)

    post.assert_not_called()


def test_webhook_disabled_does_nothing(tmp_path: Path) -> None:
    notify = NotifyCfg(enabled=False, url="https://api.example.com/v1/alerts")
    sink = AlertSink(TelegramCfg(), str(tmp_path), notify=notify)

    with patch("storeguard.alerts.requests.post") as post:
        sink.handle(_event(), [], fps=10.0)

    post.assert_not_called()


def test_webhook_empty_kinds_sends_all(tmp_path: Path) -> None:
    notify = NotifyCfg(enabled=True, url="https://api.example.com/v1/alerts", kinds=[])
    sink = AlertSink(TelegramCfg(), str(tmp_path), notify=notify)

    with patch("storeguard.alerts.requests.post") as post:
        post.return_value = MagicMock(ok=True, status_code=200, text="ok")
        sink.handle(_event("pocket"), [], fps=10.0)

    assert post.called
    assert post.call_args.kwargs["json"]["kind"] == "pocket"
