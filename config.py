"""
Configuration management for the FastAPI backend.
Loads settings from environment variables with sensible defaults.
"""

import logging
from typing import List
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

    # ── Supabase (Task 1 + Task 7) ──────────────────────────────────────────
    supabase_url: str = ""
    supabase_service_role_key: str = ""
    supabase_jwt_secret: str = ""

    # ── Internal cron (Task 5) ──────────────────────────────────────────────
    internal_cron_token: str = ""

    # ── Lemon Squeezy (Task 7) ──────────────────────────────────────────────
    lemon_squeezy_webhook_secret: str = ""
    lemon_squeezy_store_id: str = ""
    lemon_squeezy_checkout_base: str = ""
    lemon_squeezy_monthly_variant_id: str = ""
    lemon_squeezy_quarterly_variant_id: str = ""
    lemon_squeezy_semiannual_variant_id: str = ""
    lemon_squeezy_yearly_variant_id: str = ""
    lemon_squeezy_founder_variant_id: str = ""
    lemon_squeezy_api_key: str = ""

    class Config:
        env_file = ".env"
        case_sensitive = False

    @property
    def cors_origins_list(self) -> List[str]:
        """Parse CORS origins from comma-separated string"""
        return [origin.strip() for origin in self.cors_origins.split(",")]


# Global settings instance
settings = Settings()


def validate_settings():
    """Validate critical settings on application startup.

    Hard-fails only on the SEC user-agent default (yfinance/SEC EDGAR break
    without a real UA). Everything else logs a warning so the app can still
    boot with reduced functionality (e.g. subscription endpoints fail closed
    when Lemon Squeezy / Supabase creds are missing).
    """
    if not settings.sec_user_agent or "contact@example.com" in settings.sec_user_agent:
        raise ValueError(
            "SEC_USER_AGENT must be configured with a valid email address. "
            "Update your .env file with: SEC_USER_AGENT='YourApp contact@youremail.com'"
        )

    if settings.chart_cache_ttl < 0:
        raise ValueError("CHART_CACHE_TTL must be a positive number")

    if settings.chart_cache_max_size < 1:
        raise ValueError("CHART_CACHE_MAX_SIZE must be at least 1")

    # ── Non-fatal warnings for subscription-related config ──────────────────
    if (
        not settings.supabase_url
        or not settings.supabase_service_role_key
        or not settings.supabase_jwt_secret
    ):
        logger.warning(
            "Supabase credentials missing; subscription endpoints will fail"
        )

    if not settings.lemon_squeezy_webhook_secret:
        logger.warning(
            "LS webhook secret missing; webhooks will reject all requests"
        )

    if not settings.internal_cron_token:
        logger.warning(
            "Internal cron token missing; retention endpoint will reject all requests"
        )

    # Grouped warning for the remaining Lemon Squeezy vars — one message is enough.
    ls_vars_missing = [
        name
        for name, value in (
            ("LEMON_SQUEEZY_STORE_ID", settings.lemon_squeezy_store_id),
            ("LEMON_SQUEEZY_CHECKOUT_BASE", settings.lemon_squeezy_checkout_base),
            ("LEMON_SQUEEZY_MONTHLY_VARIANT_ID", settings.lemon_squeezy_monthly_variant_id),
            ("LEMON_SQUEEZY_QUARTERLY_VARIANT_ID", settings.lemon_squeezy_quarterly_variant_id),
            ("LEMON_SQUEEZY_SEMIANNUAL_VARIANT_ID", settings.lemon_squeezy_semiannual_variant_id),
            ("LEMON_SQUEEZY_YEARLY_VARIANT_ID", settings.lemon_squeezy_yearly_variant_id),
            ("LEMON_SQUEEZY_API_KEY", settings.lemon_squeezy_api_key),
        )
        if not value
    ]
    if ls_vars_missing:
        logger.warning(
            "Lemon Squeezy config incomplete (missing: %s); checkout/portal flows may fail",
            ", ".join(ls_vars_missing),
        )

    logger.info("Configuration validated successfully")
    logger.info("CORS Origins: %s", settings.cors_origins_list)
    logger.info("Cache TTL: %ss", settings.chart_cache_ttl)
    logger.info("Cache Max Size: %s", settings.chart_cache_max_size)
    logger.info("SEC User Agent: %s", settings.sec_user_agent)
    # Keep a single stdout breadcrumb so boot logs remain visually obvious
    # even when the logging handler filters INFO.
    print("✅ Configuration validated successfully")
