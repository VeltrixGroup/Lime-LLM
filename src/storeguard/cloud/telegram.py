"""Telegram delivery for the cloud control plane (per-tenant bot).

Torch-free (just ``requests``). All functions are defensive — a failed send is
logged and reported as ``False`` so alert delivery can never crash a request or
a background task.
"""

from __future__ import annotations

import os
from pathlib import Path

import requests
from rich.console import Console

_console = Console(stderr=True)
_DEFAULT_API = "https://api.telegram.org"


def api_base() -> str:
    """Telegram Bot API base URL.

    Override with ``TELEGRAM_API_BASE`` (e.g. a reverse proxy outside a region
    where api.telegram.org is blocked). Read on every call so it can be changed
    without a code change.
    """
    return (os.environ.get("TELEGRAM_API_BASE") or _DEFAULT_API).rstrip("/")


def send_message(bot_token: str, chat_id: str, text: str, timeout: float = 20.0) -> bool:
    """Send a text message; returns True on a 2xx from Telegram."""
    try:
        resp = requests.post(
            f"{api_base()}/bot{bot_token}/sendMessage",
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
                f"{api_base()}/bot{bot_token}/sendVideo",
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


# ---- richer delivery (Lime AI panel): buttons, silent sends, photos, bot poll ----


def _post(bot_token: str, method: str, timeout: float = 20.0, **kwargs) -> dict | None:
    """POST a Bot API method; the decoded ``result`` on success, else ``None``."""
    try:
        resp = requests.post(f"{api_base()}/bot{bot_token}/{method}", timeout=timeout, **kwargs)
        data = resp.json() if resp.content else {}
        if not resp.ok or not data.get("ok"):
            _console.log(
                f"[red]telegram.{method} failed ({resp.status_code}): {resp.text[:200]}[/red]"
            )
            return None
        return data.get("result") if isinstance(data.get("result"), (dict, list)) else {}
    except Exception as exc:  # noqa: BLE001 - delivery must never raise
        _console.log(f"[red]telegram.{method} error: {exc}[/red]")
        return None


def review_keyboard(event_id: str, labels: tuple[str, str]) -> str:
    """Inline "Подтвердить / Ложная тревога" buttons for an incident message."""
    import json

    return json.dumps(
        {
            "inline_keyboard": [
                [
                    {"text": labels[0], "callback_data": f"ok:{event_id}"},
                    {"text": labels[1], "callback_data": f"no:{event_id}"},
                ]
            ]
        }
    )


def send(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    video: str | Path | None = None,
    photo: bytes | None = None,
    reply_markup: str | None = None,
    silent: bool = False,
) -> bool:
    """Send one incident message as video / photo / text (falls back to text)."""
    data: dict = {"chat_id": chat_id, "disable_notification": "true" if silent else "false"}
    if reply_markup:
        data["reply_markup"] = reply_markup
    if video is not None and Path(video).is_file():
        with Path(video).open("rb") as fh:
            ok = _post(
                bot_token,
                "sendVideo",
                timeout=60.0,
                data={**data, "caption": text[:1024]},
                files={"video": (Path(video).name, fh, "video/mp4")},
            )
        if ok is not None:
            return True
    if photo:
        ok = _post(
            bot_token,
            "sendPhoto",
            timeout=30.0,
            data={**data, "caption": text[:1024]},
            files={"photo": ("snapshot.jpg", photo, "image/jpeg")},
        )
        if ok is not None:
            return True
    return _post(bot_token, "sendMessage", data={**data, "text": text[:4096]}) is not None


def get_me(bot_token: str) -> dict | None:
    """The bot's own user (``username`` etc.), or ``None`` if the token is bad."""
    return _post(bot_token, "getMe")


def get_updates(bot_token: str, offset: int, timeout: float = 10.0) -> list[dict]:
    """New updates after ``offset`` (short poll)."""
    result = _post(
        bot_token,
        "getUpdates",
        timeout=timeout,
        data={"offset": offset, "timeout": 0, "allowed_updates": '["message","callback_query","my_chat_member"]'},
    )
    return result if isinstance(result, list) else []


def answer_callback(bot_token: str, callback_id: str, text: str) -> None:
    _post(bot_token, "answerCallbackQuery", data={"callback_query_id": callback_id, "text": text})


def clear_buttons(bot_token: str, chat_id: str | int, message_id: int) -> None:
    _post(
        bot_token,
        "editMessageReplyMarkup",
        data={"chat_id": chat_id, "message_id": message_id, "reply_markup": '{"inline_keyboard":[]}'},
    )
