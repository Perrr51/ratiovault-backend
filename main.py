"""
RatioVault API — App factory and router mounting.

All endpoint logic lives in routers/. Shared state lives in deps.py.
Pure utilities live in utils.py and services/.
"""

import logging
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from config import settings, validate_settings
from deps import limiter

logger = logging.getLogger(__name__)

# ── Telegram setWebhook lifespan ──────────────────────────────────────────────


async def _register_telegram_webhook() -> None:
    """Call Telegram setWebhook on startup. Idempotent — safe to repeat.

    Skipped silently if TELEGRAM_BOT_TOKEN is empty (local dev / CI).
    Errors are caught and logged as warnings so the app starts regardless.
    """
    token = settings.telegram_bot_token
    if not token:
        logger.info("TELEGRAM_BOT_TOKEN not set; skipping setWebhook")
        return

    webhook_url = settings.telegram_webhook_url
    secret = settings.telegram_webhook_secret

    if not webhook_url:
        logger.warning("TELEGRAM_WEBHOOK_URL not set; skipping setWebhook")
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/setWebhook",
                json={
                    "url": webhook_url,
                    "secret_token": secret,
                    "allowed_updates": ["message", "callback_query"],
                },
            )
            data = resp.json()
            if data.get("ok"):
                logger.info("Telegram webhook registered: %s", webhook_url)
            else:
                logger.warning("Telegram setWebhook returned not-ok: %s", data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram setWebhook failed (bot offline?): %s — app continues", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001
    """FastAPI lifespan: startup hooks before yield, shutdown hooks after."""
    await _register_telegram_webhook()
    yield
    # future shutdown hooks go here


# ── App creation ─────────────────────────────────────────────────────────────

_is_prod = os.getenv("ENVIRONMENT", "production") == "production"
app = FastAPI(
    lifespan=lifespan,
    title="RatioVault API",
    version="1.0.0",
    docs_url=None if _is_prod else "/docs",
    redoc_url=None if _is_prod else "/redoc",
    openapi_url=None if _is_prod else "/openapi.json",
)

# Validate configuration on startup
validate_settings()

# ── Rate limiting ────────────────────────────────────────────────────────────

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── Health check (for Coolify/Docker healthcheck and uptime monitoring) ─────

@app.get("/health")
def health():
    return {"status": "ok"}


# ── Version info (populated from build-args in Dockerfile) ──────────────────

@app.get("/version")
def version():
    return {
        "sha": os.getenv("GIT_SHA", "unknown"),
        "built_at": os.getenv("BUILT_AT", "unknown"),
        "environment": os.getenv("ENVIRONMENT", "unknown"),
    }


# ── CORS ─────────────────────────────────────────────────────────────────────

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)

# ── Router mounting ──────────────────────────────────────────────────────────

from routers.market import router as market_router
from routers.charts import router as charts_router
from routers.sp500 import router as sp500_router
from routers.analytics import router as analytics_router
from routers.asset_info import router as asset_info_router
from routers.sec import router as sec_router
from routers.history import router as history_router
from routers.dividends_funds import router as dividends_funds_router
from routers.alerts import router as alerts_router
from routers.justetf_routes import router as justetf_router
from routers.stooq_routes import router as stooq_router
from routers.internal import router as internal_router
from routers.checkout import router as checkout_router
from routers.webhooks import router as webhooks_router
from routers.portal import router as portal_router
from routers.health import router as health_router
from routers.telegram import router as telegram_router
from routers.telegram_bot import router as telegram_bot_router

app.include_router(market_router)
app.include_router(charts_router)
app.include_router(sp500_router)
app.include_router(analytics_router)
app.include_router(asset_info_router)
app.include_router(sec_router)
app.include_router(history_router)
app.include_router(dividends_funds_router)
app.include_router(alerts_router)
app.include_router(justetf_router)
app.include_router(stooq_router)
app.include_router(internal_router)
app.include_router(checkout_router)
app.include_router(webhooks_router)
app.include_router(portal_router)
app.include_router(health_router)
app.include_router(telegram_router)
app.include_router(telegram_bot_router)
