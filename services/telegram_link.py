"""Telegram link service — service_role Supabase ops for linking/unlinking.

No FastAPI imports. Pure service layer callable from the router or tests.
"""
from __future__ import annotations

import logging
import secrets
import warnings

from postgrest.exceptions import APIError

from config import settings
from supabase_client import get_supabase_service

logger = logging.getLogger(__name__)


# ── Custom exceptions for RPC error mapping ───────────────────────────────────


class TokenInvalidOrExpired(Exception):
    """RPC P0002: token not found, already consumed, or expired."""


class UserAlreadyLinked(Exception):
    """RPC P0003: user already has a linked Telegram channel."""


class ChatAlreadyLinked(Exception):
    """RPC P0004: chat_id already linked to another user."""


class CodeNotFound(Exception):
    """RPC P0005: telegram_link_code not found in user_settings."""


# ── Link-code helpers ─────────────────────────────────────────────────────────


def _generate_code() -> str:
    """Return a zero-padded 9-digit CSPRNG code string."""
    return f"{secrets.randbelow(10 ** 9):09d}"


def _is_unique_violation(exc: APIError) -> bool:
    """Return True when the APIError represents a Postgres UNIQUE violation (23505)."""
    code = getattr(exc, "code", "") or ""
    message = getattr(exc, "message", "") or ""
    args_str = " ".join(str(a) for a in getattr(exc, "args", []) or [])
    haystack = " ".join([code, message, args_str, str(exc)])
    return "23505" in haystack or "unique" in haystack.lower()


def get_or_create_link_code(user_id: str) -> str:
    """Return the existing link code or generate + persist a new one.

    Lazy generation: SELECT first; only UPDATE if the column is NULL.
    Retries up to 3 times on UNIQUE collision.

    Args:
        user_id: Authenticated user UUID.

    Returns:
        9-digit zero-padded string code.

    Raises:
        RuntimeError: DB error or collision exhaustion after 3 retries.
    """
    supa = get_supabase_service()

    # Fetch current code
    try:
        result = (
            supa.table("user_settings")
            .select("telegram_link_code")
            .eq("user_id", user_id)
            .execute()
        )
    except APIError as exc:
        logger.error("user_settings select failed for user %s: %s", user_id, exc)
        raise RuntimeError("DB error fetching link code") from exc

    rows = result.data or []
    if rows and rows[0].get("telegram_link_code"):
        return rows[0]["telegram_link_code"]

    # Generate and persist
    for attempt in range(3):
        new_code = _generate_code()
        try:
            upd = (
                supa.table("user_settings")
                .update({"telegram_link_code": new_code})
                .eq("user_id", user_id)
                .execute()
            )
            updated = upd.data or []
            if updated:
                return updated[0]["telegram_link_code"]
            # Fallback: return generated code (no RETURNING data)
            return new_code
        except APIError as exc:
            if _is_unique_violation(exc) and attempt < 2:
                logger.warning("link code collision attempt %d for user %s", attempt + 1, user_id)
                continue
            logger.error("link code UPDATE failed for user %s: %s", user_id, exc)
            raise RuntimeError("DB error persisting link code") from exc

    raise RuntimeError("Exhausted 3 retry attempts generating unique link code")


def rotate_link_code(user_id: str) -> str:
    """Generate a new code and unconditionally overwrite the current one.

    Retries up to 3 times on UNIQUE collision (astronomically unlikely).

    Args:
        user_id: Authenticated user UUID.

    Returns:
        New 9-digit zero-padded string code.

    Raises:
        RuntimeError: DB error or collision exhaustion.
    """
    supa = get_supabase_service()

    for attempt in range(3):
        new_code = _generate_code()
        try:
            upd = (
                supa.table("user_settings")
                .update({"telegram_link_code": new_code})
                .eq("user_id", user_id)
                .execute()
            )
            updated = upd.data or []
            if updated:
                return updated[0]["telegram_link_code"]
            return new_code
        except APIError as exc:
            if _is_unique_violation(exc) and attempt < 2:
                logger.warning("rotate code collision attempt %d for user %s", attempt + 1, user_id)
                continue
            logger.error("rotate_link_code UPDATE failed for user %s: %s", user_id, exc)
            raise RuntimeError("DB error rotating link code") from exc

    raise RuntimeError("Exhausted 3 retry attempts rotating link code")


def link_by_code(code: str, chat_id: str, locale: str = "es") -> dict:
    """Atomically link a Telegram chat to the user identified by code.

    Invokes the ``link_telegram_by_code`` RPC (SECURITY DEFINER, service_role only).
    Maps SQLSTATE error codes to typed exceptions.

    Args:
        code:    9-digit binding code from the user.
        chat_id: Telegram chat_id (string).
        locale:  Locale detected from the Telegram user (e.g. "es").

    Returns:
        {"user_id": str, "locale": str}

    Raises:
        CodeNotFound:       P0005 — no row with this code.
        UserAlreadyLinked:  P0003 — user already has a telegram channel.
        ChatAlreadyLinked:  P0004 — chat_id bound to a different user.
        RuntimeError:       Any other DB error.
    """
    supa = get_supabase_service()
    try:
        result = supa.rpc(
            "link_telegram_by_code",
            {"p_code": code, "p_chat_id": str(chat_id), "p_locale": locale},
        ).execute()
    except APIError as exc:
        err_code = getattr(exc, "code", "") or ""
        details = getattr(exc, "details", "") or ""
        hint = getattr(exc, "hint", "") or ""
        message_attr = getattr(exc, "message", "") or ""
        args_str = " ".join(str(a) for a in getattr(exc, "args", []) or [])
        haystack = " ".join([err_code, details, hint, message_attr, args_str, str(exc)])
        logger.warning("link_telegram_by_code RPC error: %s", haystack)

        if "P0005" in haystack or "CODE_NOT_FOUND" in haystack:
            raise CodeNotFound("No user with that link code") from exc
        if "P0003" in haystack or "USER_ALREADY_LINKED" in haystack:
            raise UserAlreadyLinked("User already has a linked Telegram channel") from exc
        if "P0004" in haystack or "CHAT_ALREADY_LINKED" in haystack:
            raise ChatAlreadyLinked("Chat ID is already linked to another user") from exc
        logger.error("link_telegram_by_code RPC failed (no errcode match): %s", exc)
        raise RuntimeError("DB error linking telegram by code") from exc

    data = result.data
    return {"user_id": data["user_id"], "locale": data["locale"]}


# DEPRECATED 2026-05-06 — use link_by_code / get_or_create_link_code / rotate_link_code
# Kept 1 sprint for rollback safety (drop after ~2026-05-13).


def consume_link_token(token: str, chat_id: str, locale: str = "es") -> dict:
    """Atomically consume a Telegram link token via the RPC.

    Calls ``consume_telegram_link_token(p_token, p_chat_id, p_locale)`` via
    service_role client. The RPC is SECURITY DEFINER and handles race conditions.

    Args:
        token:   UUID string from the deep-link ``?start=<token>`` parameter.
        chat_id: Telegram chat_id (as string) received in the /start message.
        locale:  User locale sent by the bot (e.g. "es", "en"). Defaults to "es".

    Returns:
        {"user_id": str, "locale": str}  — passthrough from the RPC.

    Raises:
        TokenInvalidOrExpired: RPC errcode P0002 (token gone / expired / consumed).
        UserAlreadyLinked:     RPC errcode P0003 (user already has a channel).
        ChatAlreadyLinked:     RPC errcode P0004 (chat_id belongs to another user).
        RuntimeError:          Any other database error.
    """
    warnings.warn(
        "consume_link_token is deprecated (2026-05-06). Use link_by_code instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    supa = get_supabase_service()
    try:
        result = supa.rpc(
            "consume_telegram_link_token",
            {"p_token": token, "p_chat_id": str(chat_id), "p_locale": locale},
        ).execute()
    except APIError as exc:
        # APIError may carry the Postgres errcode in any of: .code, .details, .hint,
        # .message, args, or the str() repr. Concatenate everything searchable.
        code = getattr(exc, "code", "") or ""
        details = getattr(exc, "details", "") or ""
        hint = getattr(exc, "hint", "") or ""
        message_attr = getattr(exc, "message", "") or ""
        args_str = " ".join(str(a) for a in getattr(exc, "args", []) or [])
        haystack = " ".join([code, details, hint, message_attr, args_str, str(exc)])
        logger.warning("consume_telegram_link_token RPC error haystack: %s", haystack)
        if "P0002" in haystack or "TOKEN_INVALID_OR_EXPIRED" in haystack:
            raise TokenInvalidOrExpired("Token is invalid, expired or already consumed") from exc
        if "P0003" in haystack or "USER_ALREADY_LINKED" in haystack:
            raise UserAlreadyLinked("User already has a linked Telegram channel") from exc
        if "P0004" in haystack or "CHAT_ALREADY_LINKED" in haystack:
            raise ChatAlreadyLinked("Chat ID is already linked to another user") from exc
        logger.error("consume_telegram_link_token RPC failed (no errcode match): %s", exc)
        raise RuntimeError("DB error consuming link token") from exc

    data = result.data
    return {"user_id": data["user_id"], "locale": data["locale"]}


def init_link(user_id: str) -> dict:  # DEPRECATED 2026-05-06
    """Generate a Telegram deep-link token for the given user.

    Inserts a row into telegram_link_tokens (UUID + expires_at generated
    by DB defaults) and returns the deep_link_url + expires_at.

    Returns:
        {"deep_link_url": str, "expires_at": str}  # expires_at ISO8601

    Raises:
        RuntimeError on DB error.
    """
    warnings.warn(
        "init_link is deprecated (2026-05-06). Use get_or_create_link_code instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    supa = get_supabase_service()
    try:
        result = (
            supa.table("telegram_link_tokens")
            .insert({"user_id": user_id})
            .execute()
        )
    except APIError as exc:
        logger.error("telegram_link_tokens insert failed for user %s: %s", user_id, exc)
        raise RuntimeError("DB error creating link token") from exc

    if not result.data:
        raise RuntimeError("DB returned empty data on token insert")

    row = result.data[0]
    token: str = row["token"]
    expires_at: str = row["expires_at"]

    bot_username = settings.telegram_bot_username
    deep_link_url = f"https://t.me/{bot_username}?start={token}"

    return {"deep_link_url": deep_link_url, "expires_at": expires_at}


def resolve_user_by_chat(chat_id: str) -> dict | None:
    """Returns {user_id, locale} or None if not linked."""
    supa = get_supabase_service()
    try:
        result = (
            supa.table("notification_channels")
            .select("user_id,locale")
            .eq("external_id", str(chat_id))
            .eq("channel", "telegram")
            .limit(1)
            .execute()
        )
    except APIError as exc:
        logger.error("notification_channels lookup failed for chat_id %s: %s", chat_id, exc)
        return None

    rows = result.data or []
    if not rows:
        return None
    row = rows[0]
    return {"user_id": row["user_id"], "locale": row.get("locale") or "es"}


def update_locale(user_id: str, locale: str) -> None:
    """Update the locale for the user's Telegram notification channel."""
    supa = get_supabase_service()
    try:
        (
            supa.table("notification_channels")
            .update({"locale": locale})
            .eq("user_id", user_id)
            .eq("channel", "telegram")
            .execute()
        )
    except APIError as exc:
        logger.error("notification_channels locale update failed for user %s: %s", user_id, exc)
        raise RuntimeError("DB error updating locale") from exc


def delete_link(user_id: str) -> None:
    """Remove a user's Telegram link: purge channel row + unconsumed tokens.

    Two sequential deletes; no transaction needed (both are idempotent and
    the worst-case outcome of partial failure is stale data, not corruption).

    Raises:
        RuntimeError on DB error.
    """
    supa = get_supabase_service()

    try:
        (
            supa.table("notification_channels")
            .delete()
            .eq("user_id", user_id)
            .eq("channel", "telegram")
            .execute()
        )
    except APIError as exc:
        logger.error("notification_channels delete failed for user %s: %s", user_id, exc)
        raise RuntimeError("DB error deleting notification channel") from exc

    try:
        (
            supa.table("telegram_link_tokens")
            .delete()
            .eq("user_id", user_id)
            .is_("consumed_at", "null")
            .execute()
        )
    except APIError as exc:
        logger.error("telegram_link_tokens delete failed for user %s: %s", user_id, exc)
        raise RuntimeError("DB error revoking link tokens") from exc
