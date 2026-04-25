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


def should_try_stooq(yahoo_ticker: str, *, broad: bool = False) -> bool:
    """Check if this ticker should attempt Stooq fallback.

    Args:
        yahoo_ticker: Yahoo-format symbol.
        broad: When True (B-008 path), accept any plausible ticker shape,
            not just the curated metals/forex/crypto patterns. The caller
            must still verify upstream returned a zero/empty quote — this
            only decides whether Stooq is worth asking.
    """
    if yahoo_ticker in YAHOO_TO_STOOQ:
        return True
    if any(p.match(yahoo_ticker) for p in _STOOQ_PATTERNS):
        return True
    if broad:
        # Stooq covers most US-listed equities (lowercase symbol). Skip
        # incompatible shapes (index symbols starting with ^, empty input)
        # but be permissive otherwise.
        if not yahoo_ticker or yahoo_ticker.startswith("^"):
            return False
        return True
    return False


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


def fetch_stooq_history(yahoo_ticker: str, start: str, end: str) -> Optional[dict]:
    """
    Fetch historical daily close prices from Stooq.pl.

    Args:
        yahoo_ticker: Yahoo-format ticker (e.g., 'XAUCHF=X')
        start: Start date YYYY-MM-DD
        end: End date YYYY-MM-DD

    Returns:
        dict with {dates: list[str], closes: list[float]} or None if unavailable.
    """
    stooq_ticker = yahoo_to_stooq_ticker(yahoo_ticker)
    d1 = start.replace('-', '')
    d2 = end.replace('-', '')
    url = f"https://stooq.com/q/d/l/?s={stooq_ticker}&d1={d1}&d2={d2}&i=d"

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, headers={
                'User-Agent': 'Mozilla/5.0 (compatible; RatioVault/1.0)'
            })
            resp.raise_for_status()

            text = resp.text.strip()
            if not text or 'No data' in text or 'Exceeded' in text:
                logger.debug(f"Stooq history: no data for {stooq_ticker}")
                return None

            lines = text.split('\n')
            if len(lines) < 2:
                return None

            # Parse CSV header
            header = [h.strip().lower() for h in lines[0].split(',')]
            date_col = header.index('date') if 'date' in header else 0
            close_col = header.index('close') if 'close' in header else -1
            if close_col < 0:
                return None

            dates = []
            closes = []

            for line in lines[1:]:
                if not line.strip():
                    continue
                parts = line.split(',')
                if len(parts) <= max(date_col, close_col):
                    continue

                date_str = parts[date_col].strip()
                close_str = parts[close_col].strip()
                if not date_str or not close_str or close_str == 'N/D':
                    continue

                try:
                    close_val = float(close_str)
                    if close_val <= 0:
                        continue
                    # Normalize date to YYYY-MM-DD
                    if len(date_str) == 8:
                        date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
                    dates.append(date_str)
                    closes.append(close_val)
                except (ValueError, TypeError):
                    continue

            if not dates:
                return None

            # Stooq may return newest-first — ensure oldest-first
            if len(dates) > 1 and dates[0] > dates[-1]:
                dates.reverse()
                closes.reverse()

            logger.info(f"Stooq history: {yahoo_ticker} -> {stooq_ticker}, {len(dates)} data points")
            return {'dates': dates, 'closes': closes}

    except Exception as e:
        logger.warning(f"Stooq history fetch failed for {stooq_ticker}: {e}")
        return None


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
