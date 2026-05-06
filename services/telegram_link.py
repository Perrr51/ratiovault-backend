"""Telegram link service — service_role Supabase ops for linking/unlinking.

No FastAPI imports. Pure service layer callable from the router or tests.
"""
from __future__ import annotations

import logging

from postgrest.exceptions import APIError

from config import settings
from supabase_client import get_supabase_service

logger = logging.getLogger(__name__)


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
