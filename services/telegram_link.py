"""Telegram link service — service_role Supabase ops for linking/unlinking.

No FastAPI imports. Pure service layer callable from the router or tests.
"""
from __future__ import annotations

import logging

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
    supa = get_supabase_service()
    try:
        result = supa.rpc(
            "consume_telegram_link_token",
            {"p_token": token, "p_chat_id": str(chat_id), "p_locale": locale},
        ).execute()
    except APIError as exc:
        code = getattr(exc, "code", "") or ""
        msg = str(exc)
        # APIError may carry the Postgres errcode in .code or in the message.
        if "P0002" in code or "P0002" in msg or "TOKEN_INVALID_OR_EXPIRED" in msg:
            raise TokenInvalidOrExpired("Token is invalid, expired or already consumed") from exc
        if "P0003" in code or "P0003" in msg or "USER_ALREADY_LINKED" in msg:
            raise UserAlreadyLinked("User already has a linked Telegram channel") from exc
        if "P0004" in code or "P0004" in msg or "CHAT_ALREADY_LINKED" in msg:
            raise ChatAlreadyLinked("Chat ID is already linked to another user") from exc
        logger.error("consume_telegram_link_token RPC failed: %s", exc)
        raise RuntimeError("DB error consuming link token") from exc

    data = result.data
    return {"user_id": data["user_id"], "locale": data["locale"]}


def init_link(user_id: str) -> dict:
    """Generate a Telegram deep-link token for the given user.

    Inserts a row into telegram_link_tokens (UUID + expires_at generated
    by DB defaults) and returns the deep_link_url + expires_at.

    Returns:
        {"deep_link_url": str, "expires_at": str}  # expires_at ISO8601

    Raises:
        RuntimeError on DB error.
    """
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
