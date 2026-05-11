"""Email service port — hexagonal-lite adapter pattern.

Primary adapter: Resend HTTP API (RESEND_API_KEY must be set).
Fallback adapter: Zoho SMTP via smtplib SMTP + STARTTLS.

Factory function `send_email` selects the active adapter from settings at
call time, allowing tests to swap adapters via monkeypatching settings fields.

Email body is always plain text. The caller is responsible for assembling the
body string; this module does NOT render HTML from user input.
"""
from __future__ import annotations

import smtplib
from dataclasses import dataclass, field
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import httpx

from config import settings


# ── Result type ───────────────────────────────────────────────────────────────


@dataclass
class EmailResult:
    ok: bool
    error: Optional[str] = field(default=None)

    @classmethod
    def success(cls) -> "EmailResult":
        return cls(ok=True)

    @classmethod
    def failure(cls, error: str) -> "EmailResult":
        return cls(ok=False, error=error)


# ── Resend adapter ────────────────────────────────────────────────────────────


def _send_via_resend(
    to: str,
    subject: str,
    body: str,
    reply_to: str,
    api_key: str,
    from_addr: str,
) -> EmailResult:
    """Send email via Resend HTTP API.

    Uses plain-text body only — no HTML rendering of user input.
    Raises on non-2xx; caller wraps into EmailResult.
    """
    payload = {
        "from": from_addr,
        "to": [to],
        "subject": subject,
        "text": body,  # plain-text only
        "reply_to": reply_to,
    }
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            )
            resp.raise_for_status()
        return EmailResult.success()
    except httpx.HTTPStatusError as exc:
        return EmailResult.failure(f"Resend HTTP error: {exc.response.status_code}")
    except httpx.HTTPError as exc:
        return EmailResult.failure(f"Resend network error: {exc}")


# ── Zoho SMTP adapter ─────────────────────────────────────────────────────────


def _send_via_smtp(
    to: str,
    subject: str,
    body: str,
    reply_to: str,
    host: str,
    user: str,
    password: str,
    from_addr: str,
    port: int = 587,
) -> EmailResult:
    """Send email via Zoho SMTP (STARTTLS on port 587).

    Uses plain-text body only.
    """
    try:
        msg = MIMEMultipart()
        msg["From"] = from_addr
        msg["To"] = to
        msg["Subject"] = subject
        msg["Reply-To"] = reply_to
        msg.attach(MIMEText(body, "plain", "utf-8"))

        raw = msg.as_string()

        with smtplib.SMTP(host, port) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(user, password)
            smtp.sendmail(from_addr, to, raw)

        return EmailResult.success()
    except smtplib.SMTPException as exc:
        return EmailResult.failure(f"SMTP error: {exc}")
    except Exception as exc:  # noqa: BLE001
        return EmailResult.failure(f"SMTP unexpected error: {exc}")


# ── Factory / public API ───────────────────────────────────────────────────────


def send_email(
    to: str,
    subject: str,
    body: str,
    reply_to: str,
) -> EmailResult:
    """Send an email using the configured adapter.

    Selector logic:
    - If RESEND_API_KEY is set → use Resend HTTP adapter.
    - Elif ZOHO_SMTP_USER is set → use Zoho SMTP adapter.
    - Else → return failure (no adapter configured).

    Args:
        to: Recipient email address (must already be mapped from recipient_key).
        subject: Email subject line. Caller should prefix e.g. "[soporte]".
        body: Plain-text body. User input is treated as opaque text — no HTML.
        reply_to: The sender's email (pre-filled from auth session).

    Returns:
        EmailResult with ok=True on success, ok=False + error on failure.
    """
    from_addr = settings.contact_from or "noreply@ratiovault.com"

    if settings.resend_api_key:
        return _send_via_resend(
            to=to,
            subject=subject,
            body=body,
            reply_to=reply_to,
            api_key=settings.resend_api_key,
            from_addr=from_addr,
        )

    if settings.zoho_smtp_user:
        return _send_via_smtp(
            to=to,
            subject=subject,
            body=body,
            reply_to=reply_to,
            host=settings.zoho_smtp_host or "smtp.zoho.eu",
            user=settings.zoho_smtp_user,
            password=settings.zoho_smtp_pass or "",
            from_addr=from_addr,
        )

    return EmailResult.failure("No email adapter configured: set RESEND_API_KEY or ZOHO_SMTP_USER")
