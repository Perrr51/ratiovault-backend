"""Contact form endpoint — POST /contact.

Authentication: requires Supabase JWT (Bearer token). No anonymous access.
Rate-limit: 5 requests per hour per IP via slowapi.
Email: delegates to services.email.send_email (Resend primary, Zoho SMTP fallback).
Body sanitization: user input is treated as opaque plain text; no HTML rendered.

Rate-limit storage is in-memory by default (slowapi). Resets on restart.
This is acceptable for a 5/hour anti-abuse guard (not a hard security control).
"""
import html
import logging
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, EmailStr, field_validator

from auth import verify_supabase_jwt
from config import settings
from deps import limiter
from services.email import send_email

logger = logging.getLogger(__name__)

router = APIRouter(tags=["contact"])

# ── Recipient mapping ─────────────────────────────────────────────────────────
# Single source of truth. Drift with frontend RECIPIENT_OPTIONS is detected
# via the vitest contract test in the frontend suite.

RECIPIENTS: dict[str, str] = {
    "soporte": "soporte@ratiovault.com",
    "facturacion": "facturacion@ratiovault.com",
    "legal": "legal@ratiovault.com",
    "contacto": "contacto@ratiovault.com",
}

# ── Request schema ────────────────────────────────────────────────────────────


class ContactRequest(BaseModel):
    from_email: EmailStr
    recipient_key: Literal["soporte", "facturacion", "legal", "contacto"]
    subject: str
    message: str
    locale: str = "es"

    @field_validator("subject")
    @classmethod
    def subject_length(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 1:
            raise ValueError("subject must not be empty")
        if len(v) > 200:
            raise ValueError("subject must be at most 200 characters")
        return v

    @field_validator("message")
    @classmethod
    def message_length(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 10:
            raise ValueError("message must be at least 10 characters")
        if len(v) > 2000:
            raise ValueError("message must be at most 2000 characters")
        return v


def _sanitize_text(raw: str) -> str:
    """Escape HTML entities so no tags are rendered in the email body.

    The resulting string is plain text — angle brackets appear as &lt;/&gt;
    in the raw email text, which all mail clients display literally.
    We also unescape so the final text has no HTML entities visible to reader
    (use html.escape to strip the attack surface, html.unescape not needed here
    because we want the escaped form in plain-text to be literal &lt; chars).
    Actually for plain-text emails we want to strip HTML tags entirely:
    """
    # Remove any HTML tags by replacing < and > with safe alternatives
    # For plain text email: just strip tags rather than escape
    import re
    clean = re.sub(r"<[^>]+>", "", raw)
    return clean


def _build_body(from_email: str, locale: str, message: str) -> str:
    """Assemble the plain-text email body."""
    return f"De: {from_email}\nLocale: {locale}\n\n{message}"


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.post("/contact")
@limiter.limit("5/hour")
def contact_form(
    request: Request,
    body: ContactRequest,
    authorization: str = Header(None),
):
    """Accept a contact form submission and dispatch via email.

    Requires: Authorization: Bearer <supabase_jwt>
    Rate-limit: 5/hour per IP (slowapi, in-memory).
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    token = authorization.removeprefix("Bearer ").strip()

    # Verify JWT — raises 401 on invalid/expired token
    verify_supabase_jwt(token, settings.supabase_jwt_secret)

    to_email = RECIPIENTS[body.recipient_key]  # always valid (Pydantic Literal validates)
    clean_subject = _sanitize_text(body.subject.strip())
    clean_message = _sanitize_text(body.message.strip())

    prefixed_subject = f"[{body.recipient_key}] {clean_subject}"
    email_body = _build_body(
        from_email=str(body.from_email),
        locale=body.locale,
        message=clean_message,
    )

    result = send_email(
        to=to_email,
        subject=prefixed_subject,
        body=email_body,
        reply_to=str(body.from_email),
    )

    if not result.ok:
        logger.error("Contact email send failed: %s", result.error)
        raise HTTPException(status_code=500, detail="send_failed")

    return {"success": True}
