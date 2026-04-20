"""
RatioVault API — App factory and router mounting.

All endpoint logic lives in routers/. Shared state lives in deps.py.
Pure utilities live in utils.py and services/.
"""

import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from config import settings, validate_settings
from deps import limiter

# ── App creation ─────────────────────────────────────────────────────────────

_is_prod = os.getenv("ENVIRONMENT", "production") == "production"
app = FastAPI(
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
from routers.ai_chat import router as ai_chat_router
from routers.stooq_routes import router as stooq_router
from routers.internal import router as internal_router
from routers.checkout import router as checkout_router

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
app.include_router(ai_chat_router)
app.include_router(stooq_router)
app.include_router(internal_router)
app.include_router(checkout_router)
