"""Generic outbound webhook delivery for the cloud control plane.

Named for the first integration this shipped for (the user's own Lime CRM),
but the receiver can be anything that accepts a JSON POST — the URL and the
single auth header are both tenant-configured in :class:`~storeguard.cloud.
models.LimeCrmConfig`, nothing here is Lime-CRM-specific. Torch-free (just
``requests``). Defensive like :mod:`storeguard.cloud.telegram`: a failed send
is logged and reported as ``False`` so alert delivery can never crash a
request or a background task.
"""

from __future__ import annotations

import requests
from rich.console import Console

_console = Console(stderr=True)


def send_notification(
    webhook_url: str,
    auth_header: str,
    auth_token: str,
    payload: dict,
    timeout: float = 20.0,
) -> bool:
    """POST ``payload`` as JSON to ``webhook_url``; True on a 2xx response."""
    headers = {}
    if auth_header and auth_token:
        headers[auth_header] = auth_token
    try:
        resp = requests.post(webhook_url, json=payload, headers=headers, timeout=timeout)
        if not resp.ok:
            _console.log(
                f"[red]lime_crm.send_notification failed ({resp.status_code}): "
                f"{resp.text[:200]}[/red]"
            )
        return resp.ok
    except Exception as exc:  # noqa: BLE001 - delivery must never raise
        _console.log(f"[red]lime_crm.send_notification error: {exc}[/red]")
        return False
