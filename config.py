"""
Configuration management for the FastAPI backend.
Loads settings from environment variables with sensible defaults.
"""

import logging
from typing import List, Literal
from pydantic import field_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""

    # CORS Configuration
    cors_origins: str = "http://localhost:5173,http://localhost:5174,http://localhost:3000"

    # SEC API Configuration
    sec_user_agent: str = "ThinkInvest contact@thinkinvest.com"

    # Cache Configuration
    chart_cache_ttl: int = 300  # 5 minutes in seconds
    chart_cache_max_size: int = 100  # Maximum number of cached items

    # Logging Configuration
    log_level: str = "INFO"

    # Server Configuration
    port: int = 8000
    host: str = "0.0.0.0"

    # ── Supabase ────────────────────────────────────────────────────────────
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_jwt_secret: str = ""

    # ── Telegram Bot ────────────────────────────────────────────────────────
    telegram_bot_username: str = ""       # e.g. "RatioVaultBot" — used for deep_link_url
    telegram_bot_token: str = ""          # BotFather token; empty = bot disabled (local dev)
    telegram_webhook_secret: str = ""     # openssl rand -hex 32; must match setWebhook call
    telegram_webhook_url: str = ""        # full HTTPS URL, e.g. https://api.ratiovault.com/telegram/webhook

    # ── Internal cron ───────────────────────────────────────────────────────
    internal_cron_token: str = ""

    # ── Fallback behavior (B-008) ───────────────────────────────────────────
    stooq_any_ticker_fallback: bool = True

    # ── Contact form email adapters (WU1 ajustes-overhaul) ──────────────────
    # Resend is the primary adapter; Zoho SMTP is the fallback.
    # If neither is configured the app raises ConfigError at router include time.
    resend_api_key: str = ""           # Bearer token for api.resend.com
    zoho_smtp_host: str = "smtp.zoho.eu"
    zoho_smtp_user: str = ""           # e.g. noreply@ratiovault.com
    zoho_smtp_pass: str = ""
    contact_from: str = "noreply@ratiovault.com"

    # ── Paddle Billing (supersedes LemonSqueezy 2026-04-30) ─────────────────
    # MoR processor. Webhook signature: Paddle-Signature header (ts + h1).
    # See ADR docs/decisions/2026-04-30-paddle-supersedes-lemonsqueezy.md.
    paddle_environment: Literal["sandbox", "production"] = "production"
    paddle_api_base: str = ""  # auto-derived from paddle_environment if empty
    paddle_api_key: str = ""
    paddle_notification_secret: str = ""
    paddle_price_id_monthly: str = ""
    paddle_price_id_quarterly: str = ""
    paddle_price_id_semiannual: str = ""
    paddle_price_id_yearly: str = ""
    paddle_price_id_founder: str = ""

    @field_validator("paddle_api_base", mode="before")
    @classmethod
    def _derive_paddle_api_base(cls, v, info):
        if v:
            return v
        env = info.data.get("paddle_environment", "production")
        return "https://sandbox-api.paddle.com" if env == "sandbox" else "https://api.paddle.com"

    class Config:
        env_file = ".env"
        case_sensitive = False

    @property
    def cors_origins_list(self) -> List[str]:
        """Parse CORS origins from comma-separated string"""
        return [origin.strip() for origin in self.cors_origins.split(",")]


# Global settings instance
settings = Settings()


# ── Fail fast on empty webhook signing secret ─────────────────────────────
# Paddle notification secret required for HMAC verification on
# /webhooks/paddle. Empty value would reject every webhook silently. Set
# RATIOVAULT_SKIP_SECRET_VALIDATION=1 in test envs without subscription cfg.
import os as _os  # noqa: E402

if not _os.environ.get("RATIOVAULT_SKIP_SECRET_VALIDATION"):
    if not getattr(settings, "paddle_notification_secret", "").strip():
        raise ValueError(
            "PADDLE_NOTIFICATION_SECRET must be set (non-empty) "
            "to verify Paddle webhook signatures"
        )


def validate_settings():
    """Validate critical settings on application startup.

    Hard-fails only on the SEC user-agent default. Everything else logs a
    warning so the app can still boot with reduced functionality (e.g.
    subscription endpoints fail closed when Paddle / Supabase creds missing).
    """
    if "*" in settings.cors_origins_list:
        raise ValueError(
            "CORS wildcard '*' cannot be used with allow_credentials=True"
        )

    if not settings.sec_user_agent or "contact@example.com" in settings.sec_user_agent:
        raise ValueError(
            "SEC_USER_AGENT must be configured with a valid email address. "
            "Update your .env file with: SEC_USER_AGENT='YourApp contact@youremail.com'"
        )

    if settings.chart_cache_ttl < 0:
        raise ValueError("CHART_CACHE_TTL must be a positive number")

    if settings.chart_cache_max_size < 1:
        raise ValueError("CHART_CACHE_MAX_SIZE must be at least 1")

    if (
        not settings.supabase_url
        or not settings.supabase_service_role_key
        or not settings.supabase_jwt_secret
    ):
        logger.warning(
            "Supabase credentials missing; subscription endpoints will fail"
        )

    if not settings.paddle_notification_secret:
        logger.warning(
            "Paddle notification secret missing; webhooks will reject all requests"
        )

    if not settings.internal_cron_token:
        logger.warning(
            "Internal cron token missing; retention endpoint will reject all requests"
        )

    paddle_vars_missing = [
        name
        for name, value in (
            ("PADDLE_API_KEY", settings.paddle_api_key),
            ("PADDLE_PRICE_ID_MONTHLY", settings.paddle_price_id_monthly),
            ("PADDLE_PRICE_ID_QUARTERLY", settings.paddle_price_id_quarterly),
            ("PADDLE_PRICE_ID_SEMIANNUAL", settings.paddle_price_id_semiannual),
            ("PADDLE_PRICE_ID_YEARLY", settings.paddle_price_id_yearly),
            ("PADDLE_PRICE_ID_FOUNDER", settings.paddle_price_id_founder),
        )
        if not value
    ]
    if paddle_vars_missing:
        logger.warning(
            "Paddle config incomplete (missing: %s); checkout/portal flows may fail",
            ", ".join(paddle_vars_missing),
        )

    logger.info("Configuration validated successfully")
    logger.info("CORS Origins: %s", settings.cors_origins_list)
    logger.info("Cache TTL: %ss", settings.chart_cache_ttl)
    logger.info("Cache Max Size: %s", settings.chart_cache_max_size)
    logger.info("SEC User Agent: %s", settings.sec_user_agent)
    logger.info("Paddle environment: %s (api_base=%s)",
                settings.paddle_environment, settings.paddle_api_base)
    print("✅ Configuration validated successfully")
