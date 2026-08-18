"""Telegram delivery for the cloud control plane (per-tenant bot).

Torch-free (just ``requests``). All functions are defensive — a failed send is
logged and reported as ``False`` so alert delivery can never crash a request or
a background task.
"""

from __future__ import annotations

from pathlib import Path

import requests
from rich.console import Console

_console = Console(stderr=True)
_API = "https://api.telegram.org"


def send_message(bot_token: str, chat_id: str, text: str, timeout: float = 20.0) -> bool:
    """Send a text message; returns True on a 2xx from Telegram."""
    try:
        resp = requests.post(
            f"{_API}/bot{bot_token}/sendMessage",
            data={"chat_id": chat_id, "text": text},
            timeout=timeout,
        )
        if not resp.ok:
            _console.log(
                f"[red]telegram.send_message failed ({resp.status_code}): "
                f"{resp.text[:200]}[/red]"
            )
        return resp.ok
    except Exception as exc:  # noqa: BLE001 - delivery must never raise
        _console.log(f"[red]telegram.send_message error: {exc}[/red]")
        return False


def send_video(
    bot_token: str,
    chat_id: str,
    clip_path: str | Path,
    caption: str = "",
    timeout: float = 60.0,
) -> bool:
    """Send a video clip with a caption; returns True on a 2xx from Telegram."""
    path = Path(clip_path)
    if not path.is_file():
        return False
    try:
        with path.open("rb") as fh:
            resp = requests.post(
                f"{_API}/bot{bot_token}/sendVideo",
                data={"chat_id": chat_id, "caption": caption},
                files={"video": (path.name, fh, "video/mp4")},
                timeout=timeout,
            )
        if not resp.ok:
            _console.log(
                f"[red]telegram.send_video failed ({resp.status_code}): "
                f"{resp.text[:200]}[/red]"
            )
        return resp.ok
    except Exception as exc:  # noqa: BLE001 - delivery must never raise
        _console.log(f"[red]telegram.send_video error: {exc}[/red]")
        return False


def deliver_clip(bot_token: str, chat_id: str, clip_path: str, caption: str) -> None:
    """Deliver an event clip to Telegram; fall back to a text-only alert.

    Intended to run as a background task, so it never raises.
    """
    if send_video(bot_token, chat_id, clip_path, caption):
        return
    send_message(bot_token, chat_id, caption)
