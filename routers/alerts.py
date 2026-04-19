"""Price alert evaluation endpoint."""

import time
import yfinance as yf
from fastapi import APIRouter, HTTPException, Request
from deps import limiter
from validators import AlertEvaluateRequest
from utils import _safe_float

router = APIRouter(tags=["Alerts"])


@router.post("/alerts/evaluate")
@limiter.limit("10/minute")
async def evaluate_alerts(request: Request):
    """Evaluate alerts against current prices. Frontend persists the outcome in Supabase (table `alerts`: trigger_count, last_triggered_at, trigger_history)."""
    body = await request.json()
    try:
        validated = AlertEvaluateRequest(**body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request body")
    alerts = [a.dict() for a in validated.alerts]

    if not alerts:
        return {"results": []}

    # Get unique tickers
    tickers = list(set(a.get("ticker", "").upper() for a in alerts if a.get("ticker")))
    if not tickers:
        return {"results": []}

    # Fetch current prices
    prices = {}
    for ticker in tickers:
        try:
            t = yf.Ticker(ticker)
            info = t.info or {}
            prices[ticker] = {
                "price": _safe_float(info.get("currentPrice") or info.get("regularMarketPrice")),
                "change_pct": _safe_float(info.get("regularMarketChangePercent")),
            }
        except Exception:
            prices[ticker] = {"price": 0, "change_pct": 0}

    # Evaluate each alert
    results = []
    now = time.time()
    for alert in alerts:
        alert_id = alert.get("id")
        ticker = alert.get("ticker", "").upper()
        alert_type = alert.get("type")
        operator = alert.get("operator", "")
        target_value = float(alert.get("targetValue", 0))
        last_triggered = alert.get("lastTriggeredAt", 0)
        cooldown_hours = alert.get("cooldownHours", 24)

        # Skip if in cooldown
        if last_triggered and (now - last_triggered) < cooldown_hours * 3600:
            results.append({"alertId": alert_id, "triggered": False, "reason": "cooldown"})
            continue

        price_data = prices.get(ticker, {})
        current_price = price_data.get("price", 0)
        change_pct = price_data.get("change_pct", 0)

        triggered = False
        # Support legacy types (price_above, price_below, daily_change_pct)
        if alert_type == "price_above" and current_price > target_value:
            triggered = True
        elif alert_type == "price_below" and current_price < target_value and current_price > 0:
            triggered = True
        elif alert_type == "daily_change_pct" and abs(change_pct) > target_value:
            triggered = True
        # Support type+operator schema from AlertForm
        elif alert_type == "price" and current_price > 0:
            if operator == "gt" and current_price > target_value:
                triggered = True
            elif operator == "gte" and current_price >= target_value:
                triggered = True
            elif operator == "lt" and current_price < target_value:
                triggered = True
            elif operator == "lte" and current_price <= target_value:
                triggered = True
        elif alert_type == "percent_change" and operator in ("gt", "gte"):
            if abs(change_pct) > target_value:
                triggered = True

        results.append({
            "alertId": alert_id,
            "triggered": triggered,
            "currentPrice": current_price,
            "changePct": change_pct,
        })

    return {"results": results}
