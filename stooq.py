"""
Stooq.pl client for spot prices of precious metals and crypto.
Used as fallback when yfinance fails for specific asset types.

Stooq CSV endpoint: https://stooq.com/q/l/?s=TICKER&f=sd2t2ohlcvn&h&e=csv
Format fields: s=Symbol, d2=Date, t2=Time, o=Open, h=High, l=Low, c=Close, v=Volume, n=Name
"""

import httpx
import logging
import re
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Yahoo ticker -> Stooq ticker mapping
# Stooq uses lowercase, no special suffixes
YAHOO_TO_STOOQ = {
    # Precious metals spot (Yahoo =X tickers are broken)
    'XAUUSD=X': 'xauusd',
    'XAUEUR=X': 'xaueur',
    'XAUCHF=X': 'xauchf',
    'XAUGBP=X': 'xaugbp',
    'XAGUSD=X': 'xagusd',
    'XAGEUR=X': 'xageur',
    'XAGCHF=X': 'xagchf',
    'XAGGBP=X': 'xaggbp',
    'XPTUSD=X': 'xptusd',
    'XPTEUR=X': 'xpteur',
    'XPTCHF=X': 'xptchf',
    'XPDUSD=X': 'xpdusd',
    'XPDEUR=X': 'xpdeur',
    'XPDCHF=X': 'xpdchf',
    # Forex pairs also available on Stooq as fallback
    'EURUSD=X': 'eurusd',
    'USDCHF=X': 'usdchf',
    'GBPUSD=X': 'gbpusd',
    'USDJPY=X': 'usdjpy',
    'USDCAD=X': 'usdcad',
    'USDSEK=X': 'usdsek',
    'USDNOK=X': 'usdnok',
    'USDDKK=X': 'usddkk',
    'AUDUSD=X': 'audusd',
}

# Patterns that should try Stooq when Yahoo fails
STOOQ_FALLBACK_PATTERNS = [
    # Metal spot crosses (XAU, XAG, XPT, XPD + currency)
    r'^X(AU|AG|PT|PD)(USD|EUR|CHF|GBP|JPY)=X$',
    # BTC cross-currency (BTC-CHF, BTC-GBP, etc. -- Yahoo often fails)
    r'^BTC-(CHF|GBP|JPY|CAD|AUD|SEK|NOK|DKK)$',
    # Forex pairs
    r'^[A-Z]{6}=X$',
]

_STOOQ_PATTERNS = [re.compile(p) for p in STOOQ_FALLBACK_PATTERNS]


def should_try_stooq(yahoo_ticker: str) -> bool:
    """Check if this ticker should attempt Stooq fallback."""
    if yahoo_ticker in YAHOO_TO_STOOQ:
        return True
    return any(p.match(yahoo_ticker) for p in _STOOQ_PATTERNS)


def yahoo_to_stooq_ticker(yahoo_ticker: str) -> str:
    """Convert Yahoo ticker format to Stooq format."""
    # Direct mapping
    if yahoo_ticker in YAHOO_TO_STOOQ:
        return YAHOO_TO_STOOQ[yahoo_ticker]

    # Metal spot: XAUCHF=X -> xauchf
    metal_match = re.match(r'^(X(?:AU|AG|PT|PD))([A-Z]{3})=X$', yahoo_ticker)
    if metal_match:
        return (metal_match.group(1) + metal_match.group(2)).lower()

    # BTC cross: BTC-CHF -> btcchf
    btc_match = re.match(r'^BTC-([A-Z]{3})$', yahoo_ticker)
    if btc_match:
        return ('btc' + btc_match.group(1)).lower()

    # Generic forex: EURUSD=X -> eurusd
    forex_match = re.match(r'^([A-Z]{6})=X$', yahoo_ticker)
    if forex_match:
        return forex_match.group(1).lower()

    # Generic: lowercase, remove =X and dashes
    return yahoo_ticker.replace('=X', '').replace('-', '').lower()


def fetch_stooq_quote(yahoo_ticker: str) -> Optional[dict]:
    """
    Fetch a quote from Stooq.pl for the given Yahoo-format ticker.
    Returns dict with {price, name, currency, source, open, high, low, previousClose}
    or None if unavailable.

    Uses synchronous httpx.Client for compatibility with sync endpoints.
    """
    stooq_ticker = yahoo_to_stooq_ticker(yahoo_ticker)
    url = f"https://stooq.com/q/l/?s={stooq_ticker}&f=sd2t2ohlcvn&h&e=csv"

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; RatioVault/1.0)'
            })
            resp.raise_for_status()

            text = resp.text.strip()
            if not text or 'No data' in text or 'Exceeded' in text:
                logger.debug(f"Stooq: no data for {stooq_ticker}")
                return None

            lines = text.split('\n')
            if len(lines) < 2:
                return None

            # Parse CSV: Symbol,Date,Time,Open,High,Low,Close,Volume,Name
            headers = [h.strip().lower() for h in lines[0].split(',')]
            values = [v.strip().strip('"') for v in lines[1].split(',')]

            if len(values) < len(headers):
                return None

            row = dict(zip(headers, values))

            close = row.get('close', row.get('c', ''))
            if not close or close == 'N/D':
                logger.debug(f"Stooq: N/D for {stooq_ticker}")
                return None

            price = float(close)
            if price <= 0:
                return None

            name = row.get('name', row.get('n', '')).strip()
            if name == 'N/D':
                name = ''

            # Parse OHLC values
            open_price = _parse_float(row.get('open', row.get('o', '')))
            high_price = _parse_float(row.get('high', row.get('h', '')))
            low_price = _parse_float(row.get('low', row.get('l', '')))

            # Infer currency from ticker pattern
            currency = _infer_currency(yahoo_ticker)

            logger.info(f"Stooq: {yahoo_ticker} -> {stooq_ticker} = {price} {currency}")

            return {
                'price': price,
                'open': open_price or price,
                'high': high_price or price,
                'low': low_price or price,
                'previousClose': price,  # Stooq CSV doesn't provide prev close
                'name': name or yahoo_ticker,
                'currency': currency,
                'source': 'stooq',
                'stooq_ticker': stooq_ticker,
            }

    except Exception as e:
        logger.warning(f"Stooq fetch failed for {stooq_ticker}: {e}")
        return None


def _parse_float(value: str) -> Optional[float]:
    """Safely parse a float from a CSV value."""
    if not value or value == 'N/D':
        return None
    try:
        f = float(value)
        return f if f > 0 else None
    except (ValueError, TypeError):
        return None


def _infer_currency(yahoo_ticker: str) -> str:
    """Infer the quote currency from the ticker pattern."""
    # XAUCHF=X -> CHF
    metal_match = re.match(r'^X(?:AU|AG|PT|PD)([A-Z]{3})=X$', yahoo_ticker)
    if metal_match:
        return metal_match.group(1)

    # BTC-CHF -> CHF
    crypto_match = re.match(r'^[A-Z]+-([A-Z]{3})$', yahoo_ticker)
    if crypto_match:
        return crypto_match.group(1)

    # Forex: EURUSD=X -> USD (quote currency is the second 3 letters)
    forex_match = re.match(r'^[A-Z]{3}([A-Z]{3})=X$', yahoo_ticker)
    if forex_match:
        return forex_match.group(1)

    return 'USD'


# In-memory cache for Stooq quotes (avoid hammering the service)
_stooq_cache: dict = {}
_STOOQ_CACHE_TTL = 300  # 5 minutes


def fetch_stooq_quote_cached(yahoo_ticker: str) -> Optional[dict]:
    """Cached version of fetch_stooq_quote with 5-minute TTL."""
    now = time.time()
    cached = _stooq_cache.get(yahoo_ticker)
    if cached and (now - cached['_ts']) < _STOOQ_CACHE_TTL:
        return cached['data']

    result = fetch_stooq_quote(yahoo_ticker)
    _stooq_cache[yahoo_ticker] = {'data': result, '_ts': now}
    return result
