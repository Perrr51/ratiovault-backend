"""justETF scraper module for ETF data enrichment."""
import httpx
import re
import time
from bs4 import BeautifulSoup
from functools import lru_cache
from datetime import datetime, timedelta

# Cache for ETF data (in-memory, 24h TTL)
_etf_cache = {}
_cache_ttl = 86400  # 24 hours

JUSTETF_BASE = "https://www.justetf.com/en"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# EU exchange suffixes stripped when normalising a ticker root.
_EU_SUFFIXES = re.compile(
    r'\.(?:DE|L|SW|AS|MI|PA|MC|F|XD)$',
    re.IGNORECASE,
)


class JustETFScraper:
    def __init__(self):
        self.session = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=15.0,
            follow_redirects=True,
        )
        self._counter = None

    def _get_fetch_callback_url(self) -> str | None:
        """Extract the Wicket DataTables fetchCallbackUrl from the justETF search page.

        justETF migrated from HTML-snippet arrays to a Wicket/DataTables JSON API.
        The DataTables source URL is session-scoped and embedded as JSON in the page.
        Format in page: {"fetchCallbackUrl":"/en/search.html?<wicket-component-path>"}

        Returns the full absolute callback URL, or None if not found.
        Cache result per session (reset on 5xx / connection error).
        """
        if self._counter:
            return self._counter

        resp = self.session.get(f"{JUSTETF_BASE}/search.html")
        resp.raise_for_status()
        page_body = resp.text

        match = re.search(r'"fetchCallbackUrl"\s*:\s*"([^"]+)"', page_body)
        if match:
            path = match.group(1)
            # Path is relative (/en/search.html?...) — make it absolute.
            if path.startswith("/"):
                self._counter = f"https://www.justetf.com{path}"
            else:
                self._counter = path
        return self._counter

    # Keep _get_counter as a backward-compatible alias (used by find_similar_etfs).
    def _get_counter(self) -> str | None:
        """Legacy alias for _get_fetch_callback_url. Returns only the query-string part
        for backward compatibility with callers that append it to the base URL.

        NOTE: callers in search_etfs and find_similar_etfs should prefer
        _get_fetch_callback_url() which returns the full absolute URL.
        """
        full_url = self._get_fetch_callback_url()
        if full_url is None:
            return None
        # Extract just the query-string portion (after the '?')
        idx = full_url.find("?")
        return full_url[idx + 1:] if idx >= 0 else None

    def fetch_all_etfs(self):
        """Fetch all ETF data from justETF. Returns list of dicts."""
        counter = self._get_counter()
        url = f"{JUSTETF_BASE}/search.html"
        if counter:
            url = f"{url}?{counter}"

        payload = {
            "draw": 1,
            "start": 0,
            "length": -1,
            "lang": "en",
            "country": "DE",
            "universeType": "private",
            "defaultCurrency": "EUR",
        }
        resp = self.session.post(url, data=payload)
        resp.raise_for_status()
        return resp.json().get("data", [])

    def get_etf_profile(self, isin: str) -> dict | None:
        """Get detailed ETF profile by ISIN. Uses cache."""
        cache_key = f"profile:{isin}"
        cached = _etf_cache.get(cache_key)
        if cached and (datetime.now() - cached["ts"]).total_seconds() < _cache_ttl:
            return cached["data"]

        try:
            # Try fetching the ETF profile page
            url = f"{JUSTETF_BASE}/etf-profile.html?isin={isin}"
            resp = self.session.get(url)
            if resp.status_code != 200:
                return None

            soup = BeautifulSoup(resp.text, "html.parser")
            data = self._parse_profile(soup, isin)

            _etf_cache[cache_key] = {"data": data, "ts": datetime.now()}
            return data
        except Exception:
            return None

    def _parse_profile(self, soup: BeautifulSoup, isin: str) -> dict:
        """Parse ETF profile page HTML."""
        result = {"isin": isin}

        # Extract name
        h1 = soup.find("h1")
        if h1:
            result["name"] = h1.get_text(strip=True)

        # Extract key data from the overview table
        # justETF uses table rows with label/value pairs
        for row in soup.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) >= 2:
                label = cells[0].get_text(strip=True).lower()
                value = cells[1].get_text(strip=True)

                if "ter" in label or "total expense" in label:
                    # Parse "0.20% p.a." -> 0.20
                    ter_match = re.search(r'([\d.]+)%', value)
                    if ter_match:
                        result["ter"] = float(ter_match.group(1))

                elif "fund size" in label:
                    result["fundSize"] = value

                elif "inception" in label or "fund launch" in label:
                    result["inceptionDate"] = value

                elif "distribution" in label or "use of profits" in label:
                    val_lower = value.lower()
                    if "accum" in val_lower:
                        result["distributionPolicy"] = "Accumulating"
                    elif "distrib" in val_lower:
                        result["distributionPolicy"] = "Distributing"
                    else:
                        result["distributionPolicy"] = value

                elif "replication" in label:
                    result["replication"] = value

                elif "fund currency" in label:
                    result["fundCurrency"] = value

                elif "fund domicile" in label:
                    result["domicile"] = value

        # Extract tracked index
        for td in soup.find_all("td"):
            text = td.get_text(strip=True)
            if "index" in text.lower() and td.find_next_sibling("td"):
                sibling = td.find_next_sibling("td")
                if sibling:
                    result["trackedIndex"] = sibling.get_text(strip=True)
                    break

        # Extract dividend yield if available
        for span in soup.find_all("span"):
            text = span.get_text(strip=True)
            if "dividend yield" in text.lower():
                parent = span.parent
                if parent:
                    yield_match = re.search(r'([\d.]+)%', parent.get_text())
                    if yield_match:
                        result["dividendYield"] = float(yield_match.group(1))

        # Extract sector allocation table.
        # pct stored as percentage points (0-100), consistent with etf_sector_cache
        # and calcETFSectorBreakdown frontend consumer.
        # Selector: data-testid="tl_etf-holdings_sectors_value_name" on the name cell;
        # the sibling value cell (next <td> in the same <tr>) holds the pct string.
        sectors: dict[str, float] = {}
        for name_cell in soup.find_all(attrs={"data-testid": "tl_etf-holdings_sectors_value_name"}):
            sector_name = name_cell.get_text(strip=True)
            if not sector_name:
                continue
            # Walk to the parent row and find the next sibling td with the pct value.
            row = name_cell.parent
            if row is None:
                continue
            cells = row.find_all("td")
            # The name cell may be first or last in the row — find it and get the
            # next sibling td that contains a percentage-like string.
            found_name = False
            for cell in cells:
                if found_name:
                    pct_text = cell.get_text(strip=True)
                    try:
                        pct_value = float(pct_text.rstrip("%").strip())
                        sectors[sector_name] = pct_value
                    except (ValueError, AttributeError):
                        # Skip malformed rows (e.g. "n/a") — do not crash
                        pass
                    break
                if cell is name_cell:
                    found_name = True
        # Only emit the key when at least one sector was successfully parsed.
        # Absent key → frontend treats ticker as fully unresolved → Otros bucket.
        if sectors:
            result["sectors"] = sectors

        return result

    def find_similar_etfs(self, isin: str) -> list:
        """Find ETFs tracking the same index."""
        profile = self.get_etf_profile(isin)
        if not profile or "trackedIndex" not in profile:
            return []

        index_name = profile["trackedIndex"]
        cache_key = f"similar:{isin}"
        cached = _etf_cache.get(cache_key)
        if cached and (datetime.now() - cached["ts"]).total_seconds() < _cache_ttl:
            return cached["data"]

        try:
            # Search for ETFs with the same index
            url = f"{JUSTETF_BASE}/search.html"
            counter = self._get_counter()
            if counter:
                url = f"{url}?{counter}"

            payload = {
                "draw": 1,
                "start": 0,
                "length": 20,
                "lang": "en",
                "country": "DE",
                "universeType": "private",
                "defaultCurrency": "EUR",
                "search": index_name[:50],  # Limit search query length
            }

            time.sleep(2)  # Rate limit
            resp = self.session.post(url, data=payload)
            resp.raise_for_status()

            results = []
            for etf in resp.json().get("data", []):
                etf_isin = None
                etf_name = None
                etf_ter = None

                # Parse the HTML snippets in data fields
                if isinstance(etf, list) and len(etf) > 0:
                    # justETF returns arrays with HTML content
                    for field in etf:
                        if isinstance(field, str):
                            if "isin" in field.lower() or len(field) == 12:
                                isin_match = re.search(r'[A-Z]{2}[A-Z0-9]{10}', field)
                                if isin_match:
                                    etf_isin = isin_match.group(0)
                            ter_match = re.search(r'([\d.]+)%\s*p\.a\.', field)
                            if ter_match:
                                etf_ter = float(ter_match.group(1))

                if etf_isin and etf_isin != isin:
                    results.append({
                        "isin": etf_isin,
                        "ter": etf_ter,
                    })

            # Sort by TER (cheapest first)
            results.sort(key=lambda x: x.get("ter", 999))

            _etf_cache[cache_key] = {"data": results[:10], "ts": datetime.now()}
            return results[:10]
        except Exception:
            return []

    def search_etfs(self, query: str) -> list:
        """Search ETFs by ticker root using the justETF Wicket/DataTables JSON API.

        Changed in Phase 2: justETF migrated from HTML-snippet arrays to a
        Wicket/DataTables JSON API. This implementation:
          1. GETs /en/search.html to extract the session-scoped fetchCallbackUrl.
          2. POSTs a DataTables payload to that URL requesting the full ETF list.
          3. Filters the returned rows client-side by exact ticker match.

        The API returns the full ETF catalogue (~3400 records) as structured JSON
        dicts. Each row includes: ticker, isin, name, fundCurrency, domicileCountry,
        distributionPolicy, ter, etc. No HTML parsing is required.

        Args:
            query: ticker root (suffix already stripped; e.g. "VWCE", "VUSA").

        Returns:
            List of matching row dicts (may be empty on no match or scraper failure).
        """
        cache_key = f"search:{query.lower()}"
        cached = _etf_cache.get(cache_key)
        if cached and (datetime.now() - cached["ts"]).total_seconds() < 3600:
            return cached["data"]

        try:
            callback_url = self._get_fetch_callback_url()
            if not callback_url:
                return []

            payload = {
                "draw": "1",
                "start": "0",
                "length": "-1",          # full catalogue
                "search[value]": "",      # no server-side filter; we filter client-side
                "search[regex]": "false",
                "lang": "en",
                "country": "DE",
                "universeType": "private",
                "defaultCurrency": "EUR",
            }

            resp = self.session.post(callback_url, data=payload)
            resp.raise_for_status()
            all_rows: list[dict] = resp.json().get("data", [])

            # Filter rows by exact ticker match (case-insensitive)
            q_upper = query.upper()
            results = [r for r in all_rows if str(r.get("ticker", "")).upper() == q_upper]

            _etf_cache[cache_key] = {"data": results, "ts": datetime.now()}
            return results
        except Exception:
            return []

    def _parse_search_result(self, item) -> dict | None:
        """Legacy HTML-snippet parser — DEPRECATED.

        The justETF Wicket/DataTables API now returns structured JSON dicts
        directly. This method is retained only so existing callers that passed
        raw list items through find_similar_etfs() do not crash, but it should
        not be called in new code.
        """
        if not isinstance(item, (list, dict)):
            return None

        # New API: row is already a clean dict — pass through if it has an ISIN.
        if isinstance(item, dict):
            return item if item.get("isin") else None

        result = {}
        raw = item  # list of HTML-snippet strings (legacy format)

        for field in raw:
            if not isinstance(field, str):
                continue

            # Extract ISIN
            isin_match = re.search(r'[A-Z]{2}[A-Z0-9]{10}', field)
            if isin_match and "isin" not in result:
                result["isin"] = isin_match.group(0)

            # Extract TER
            ter_match = re.search(r'([\d.]+)%\s*p\.a\.', field)
            if ter_match:
                result["ter"] = float(ter_match.group(1))

            # Extract name from anchor tags
            name_match = re.search(r'title="([^"]+)"', field)
            if name_match and "name" not in result:
                result["name"] = name_match.group(1)

            # Extract ticker
            ticker_match = re.search(r'<span[^>]*>([A-Z0-9]{2,6})</span>', field)
            if ticker_match and "ticker" not in result:
                candidate = ticker_match.group(1)
                if len(candidate) >= 2 and candidate != result.get("isin", "")[:6]:
                    result["ticker"] = candidate

        return result if result.get("isin") else None


# Singleton instance
_scraper = None


def get_scraper() -> JustETFScraper:
    global _scraper
    if _scraper is None:
        _scraper = JustETFScraper()
    return _scraper


# ---------------------------------------------------------------------------
# Pure confidence gate (no I/O, fully unit-testable)
# ---------------------------------------------------------------------------

def compute_isin_confidence(ticker_root: str, search_rows: "list[dict] | None") -> dict:
    """Pure function: compute ISIN resolution confidence from justETF search rows.

    Normalises ticker_root (strips EU exchange suffix, uppercases), then:
      - Filters rows whose 'ticker' field matches the normalised root.
      - Collects distinct ISINs from matched rows.
      - Returns confidence 'high' iff exactly 1 distinct ISIN is found.
      - Returns confidence 'low' with candidates iff 2+ distinct ISINs (ambiguous).
      - Returns confidence 'none' if no match or empty/None input.

    The currency cross-check from the original design is intentionally ABSENT:
    justETF 'fundCurrency' is the fund base currency (USD for most EU UCITS ETFs)
    and does NOT match the yfinance trading currency (EUR for .DE, GBP for .L).
    Using it would mis-reject nearly every EU ETF. Removed per Phase 0 spike.

    Args:
        ticker_root: ticker with or without EU exchange suffix (e.g. "VWCE" or
                     "VWCE.DE"). Suffix is stripped internally.
        search_rows: list of row dicts from search_etfs(). May be None (scraper
                     failure) or empty (no results). Each row must have at least
                     'ticker', 'isin', 'name' keys.

    Returns:
        dict with keys:
            isin (str | None): resolved ISIN or None
            confidence ('high' | 'low' | 'none')
            name (str | None): fund name for the resolved ISIN (high only)
            candidates (list[dict]): [{isin, name}, ...] for low/ambiguous results;
                                     empty list for none; single-entry list for high
    """
    # Normalise: strip EU exchange suffix, uppercase
    root = _EU_SUFFIXES.sub("", ticker_root.strip()).upper()

    # Degenerate input
    if not search_rows:
        return {"isin": None, "confidence": "none", "name": None, "candidates": []}

    # Step 1 — filter rows by exact ticker match
    matched = [
        r for r in search_rows
        if str(r.get("ticker") or r.get("symbol") or "").upper() == root
    ]

    if not matched:
        return {"isin": None, "confidence": "none", "name": None, "candidates": []}

    # Step 2 — collect distinct ISINs (preserving first-occurrence order)
    seen_isins: dict[str, str] = {}  # isin → name
    for row in matched:
        isin = row.get("isin") or ""
        if isin and isin not in seen_isins:
            seen_isins[isin] = row.get("name") or ""

    distinct_isins = list(seen_isins.items())  # [(isin, name), ...]

    # Step 3 — gate decision
    if len(distinct_isins) == 1:
        isin, name = distinct_isins[0]
        return {
            "isin": isin,
            "confidence": "high",
            "name": name or None,
            "candidates": [{"isin": isin, "name": name}],
        }

    if len(distinct_isins) >= 2:
        candidates = [{"isin": i, "name": n} for i, n in distinct_isins]
        return {
            "isin": None,
            "confidence": "low",
            "name": None,
            "candidates": candidates,
        }

    # Matched rows all had empty ISINs — treat as no match
    return {"isin": None, "confidence": "none", "name": None, "candidates": []}


# ---------------------------------------------------------------------------
# Resolver: strip suffix → search → gate → cached result
# ---------------------------------------------------------------------------

def resolve_isin_from_ticker(ticker: str) -> dict:
    """Resolve ISIN for a ticker using the justETF Wicket search + confidence gate.

    Thin orchestrator: normalise ticker root → call search_etfs → compute_isin_confidence.
    Result is cached in _etf_cache with a 1-hour TTL (same as search_etfs).

    Args:
        ticker: full ticker with or without exchange suffix (e.g. "VWCE.DE" or "VWCE").

    Returns:
        dict from compute_isin_confidence: {isin, confidence, name, candidates}
    """
    root = _EU_SUFFIXES.sub("", ticker.strip()).upper()
    cache_key = f"resolve:{root}"
    cached = _etf_cache.get(cache_key)
    if cached and (datetime.now() - cached["ts"]).total_seconds() < 3600:
        return cached["data"]

    scraper = get_scraper()
    rows = scraper.search_etfs(root)
    result = compute_isin_confidence(root, rows)

    _etf_cache[cache_key] = {"data": result, "ts": datetime.now()}
    return result
