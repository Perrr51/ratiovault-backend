"""
Pattern-based asset type classification.
Fallback when yfinance can't identify a ticker.
"""


def infer_asset_type(ticker: str) -> dict:
    """
    Fallback classification when yfinance can't identify a ticker.
    Uses ticker patterns to guess the asset type.
    """
    t = ticker.upper()

    # Commodity patterns: precious metals, oil, etc.
    commodity_prefixes = ("XAG", "XAU", "XPT", "XPD")  # Silver, Gold, Platinum, Palladium
    commodity_keywords = ("CRUDE", "OIL", "GAS", "WHEAT", "CORN", "COFFEE", "SUGAR", "COTTON", "COPPER")
    if any(t.startswith(p) for p in commodity_prefixes):
        metal_names = {"XAG": "Plata (Silver)", "XAU": "Oro (Gold)", "XPT": "Platino", "XPD": "Paladio"}
        prefix = t[:3]
        return {"quoteType": "COMMODITY", "sector": "Precious Metals", "industry": "Precious Metals", "name": metal_names.get(prefix, t)}
    if any(kw in t for kw in commodity_keywords):
        return {"quoteType": "COMMODITY", "sector": "Commodities", "industry": "Commodities", "name": t}

    # Crypto patterns
    crypto_suffixes = ("-USD", "-EUR", "-BTC")
    crypto_exact = {"BTC-USD", "ETH-USD", "SOL-USD", "ADA-USD", "DOGE-USD", "XRP-USD", "DOT-USD", "AVAX-USD", "MATIC-USD", "LINK-USD",
                     "BTC-EUR", "ETH-EUR", "SOL-EUR", "ADA-EUR", "DOGE-EUR", "XRP-EUR"}
    if t in crypto_exact:
        return {"quoteType": "CRYPTOCURRENCY", "sector": None, "industry": "Cryptocurrency", "name": t}
    if any(t.endswith(s) for s in crypto_suffixes):
        base = t.split("-")[0]
        # If it's a known commodity prefix, skip (already handled above)
        if not any(base.startswith(p) for p in commodity_prefixes):
            return {"quoteType": "CRYPTOCURRENCY", "sector": None, "industry": "Cryptocurrency", "name": t}

    # Currency pairs
    if "=X" in t:
        return {"quoteType": "CURRENCY", "sector": None, "industry": "Forex", "name": t}

    # Futures
    if t.endswith("=F"):
        return {"quoteType": "FUTURE", "sector": "Futures", "industry": "Futures", "name": t}

    # Index
    if t.startswith("^"):
        return {"quoteType": "INDEX", "sector": None, "industry": "Index", "name": t}

    # Cash / Unlisted
    if "=CASH" in t:
        return {"quoteType": "CASH", "sector": None, "industry": None, "name": t.replace("=CASH", " Cash")}
    if "=UNLISTED" in t:
        return {"quoteType": "UNLISTED", "sector": None, "industry": None, "name": t.replace("=UNLISTED", "")}

    return {"quoteType": "UNKNOWN", "sector": None, "industry": None, "name": t}
