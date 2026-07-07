"""Alert delivery: JSONL event log, saved mp4 clips and Telegram messages.

The :class:`AlertSink` is intentionally defensive — a failing disk write or
Telegram request is logged and swallowed so that alert delivery can never
crash the video pipeline.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

import cv2
import requests
from rich.console import Console

from storeguard.config import TelegramCfg
from storeguard.types import Event

if TYPE_CHECKING:
    import numpy as np

_console = Console(stderr=True)

_TELEGRAM_API = "https://api.telegram.org"
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(name: str) -> str:
    """Make a string safe for use inside a file name."""
    return _UNSAFE_FILENAME_CHARS.sub("-", name).strip("-") or "camera"


class AlertSink:
    """Persist and deliver events: JSONL log + mp4 clip + Telegram.

    Applies a per-``(camera, kind)`` minimum gap of :attr:`min_gap_sec`
    seconds on top of the scenarios' own per-track cooldowns. Thread-safe:
    one sink instance may be shared by all camera worker threads.
    """

    min_gap_sec: ClassVar[float] = 10.0
    clip_fps: ClassVar[float] = 10.0

    def __init__(self, telegram: TelegramCfg, events_dir: str) -> None:
        """Create the sink.

        Args:
            telegram: Telegram settings; messages are sent only when
                ``telegram.enabled`` is true.
            events_dir: Directory for ``events.jsonl`` and the ``clips/``
                subdirectory (created on demand).
        """
        self._telegram = telegram
        self._events_dir = Path(events_dir)
        self._last_sent: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def handle(self, event: Event, frames: list["np.ndarray"]) -> None:
        """Log the event, save an evidence clip and notify Telegram.

        Args:
            event: The incident to deliver.
            frames: Recent BGR frames (ring-buffer content) for the evidence
                clip; may be empty, in which case no clip is written.
        """
        with self._lock:
            key = (event.camera, event.kind)
            last = self._last_sent.get(key)
            if last is not None and event.ts - last < self.min_gap_sec:
                return
            self._last_sent[key] = event.ts

        self._append_jsonl(event)
        clip_path = self._write_clip(event, frames)
        if self._telegram.enabled:
            self._send_telegram(event, clip_path)

    def _append_jsonl(self, event: Event) -> None:
        """Append the event as one JSON line to ``events_dir/events.jsonl``."""
        try:
            self._events_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "kind": event.kind,
                "camera": event.camera,
                "message": event.message,
                "ts": event.ts,
                "iso_time": datetime.fromtimestamp(event.ts).astimezone().isoformat(),
                "track_id": event.track_id,
                "score": event.score,
            }
            line = json.dumps(record, ensure_ascii=False)
            with self._lock:
                with open(self._events_dir / "events.jsonl", "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except Exception as exc:  # noqa: BLE001 - alerts must never crash the pipeline
            _console.log(f"[red]AlertSink: failed to write events.jsonl: {exc}[/red]")

    def _write_clip(self, event: Event, frames: list["np.ndarray"]) -> Path | None:
        """Write the evidence mp4 and return its path (None if not written)."""
        if not frames:
            return None
        try:
            clips_dir = self._events_dir / "clips"
            clips_dir.mkdir(parents=True, exist_ok=True)
            path = clips_dir / (
                f"{_safe_name(event.camera)}_{event.kind}_{int(event.ts)}.mp4"
            )
            height, width = frames[0].shape[:2]
            writer = cv2.VideoWriter(
                str(path),
                cv2.VideoWriter_fourcc(*"mp4v"),
                self.clip_fps,
                (width, height),
            )
            if not writer.isOpened():
                _console.log(
                    f"[red]AlertSink: could not open VideoWriter for {path}[/red]"
                )
                return None
            try:
                for frame in frames:
                    if frame.shape[:2] != (height, width):
                        frame = cv2.resize(frame, (width, height))
                    writer.write(frame)
            finally:
                writer.release()
            return path
        except Exception as exc:  # noqa: BLE001 - alerts must never crash the pipeline
            _console.log(f"[red]AlertSink: failed to write event clip: {exc}[/red]")
            return None

    def _send_telegram(self, event: Event, clip_path: Path | None) -> None:
        """Send the event message and, if available, the saved clip."""
        base = f"{_TELEGRAM_API}/bot{self._telegram.bot_token}"
        try:
            resp = requests.post(
                f"{base}/sendMessage",
                data={"chat_id": self._telegram.chat_id, "text": event.message},
                timeout=20,
            )
            if not resp.ok:
                _console.log(
                    f"[red]AlertSink: Telegram sendMessage failed "
                    f"({resp.status_code}): {resp.text[:200]}[/red]"
                )
        except Exception as exc:  # noqa: BLE001 - alerts must never crash the pipeline
            _console.log(f"[red]AlertSink: Telegram sendMessage error: {exc}[/red]")

        if clip_path is None or not clip_path.exists():
            return
        try:
            with open(clip_path, "rb") as fh:
                resp = requests.post(
                    f"{base}/sendVideo",
                    data={"chat_id": self._telegram.chat_id, "caption": event.message},
                    files={"video": (clip_path.name, fh, "video/mp4")},
                    timeout=20,
                )
            if not resp.ok:
                _console.log(
                    f"[red]AlertSink: Telegram sendVideo failed "
                    f"({resp.status_code}): {resp.text[:200]}[/red]"
                )
        except Exception as exc:  # noqa: BLE001 - alerts must never crash the pipeline
            _console.log(f"[red]AlertSink: Telegram sendVideo error: {exc}[/red]")
