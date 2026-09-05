"""Password-reset email delivery for the cloud control plane (global SMTP).

stdlib-only (``smtplib`` + ``email``) — no new dependency for a single
transactional email. Defensive like ``telegram.py``: a failed send is logged
and reported as ``False``, never raised, so it's safe to run from a
background task.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from rich.console import Console

_console = Console(stderr=True)


def send_password_reset_email(
    host: str,
    port: int,
    username: str,
    password: str,
    use_tls: bool,
    from_email: str,
    to_email: str,
    reset_url: str,
    timeout: float = 20.0,
) -> bool:
    """Send the "reset your password" email; True if handed off to the SMTP server."""
    if not host:
        _console.log(
            "[red]mailer.send_password_reset_email: no SMTP host configured "
            "(set STOREGUARD_SMTP_HOST) — email not sent[/red]"
        )
        return False
    msg = EmailMessage()
    msg["Subject"] = "Reset your storeguard password"
    msg["From"] = from_email
    msg["To"] = to_email
    msg.set_content(
        "Someone requested a password reset for this storeguard account.\n\n"
        f"Reset it here (link expires in 1 hour):\n{reset_url}\n\n"
        "If you didn't request this, you can safely ignore this email."
    )
    try:
        with smtplib.SMTP(host, port, timeout=timeout) as smtp:
            if use_tls:
                smtp.starttls()
            if username:
                smtp.login(username, password)
            smtp.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001 - delivery must never raise
        _console.log(f"[red]mailer.send_password_reset_email error: {exc}[/red]")
        return False
