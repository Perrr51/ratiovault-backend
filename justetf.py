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


class JustETFScraper:
    def __init__(self):
        self.session = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=15.0,
            follow_redirects=True,
        )
        self._counter = None

    def _get_counter(self):
        """Extract dynamic counter from justETF search page."""
        if self._counter:
            return self._counter
        resp = self.session.get(f"{JUSTETF_BASE}/search.html")
        resp.raise_for_status()
        match = re.search(r'search\.html\?(\d+)', resp.text)
        if match:
            self._counter = match.group(1)
        return self._counter

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
        """Search ETFs by name or keyword."""
        cache_key = f"search:{query.lower()}"
        cached = _etf_cache.get(cache_key)
        if cached and (datetime.now() - cached["ts"]).total_seconds() < 3600:  # 1h cache for searches
            return cached["data"]

        try:
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
                "search": query,
            }

            resp = self.session.post(url, data=payload)
            resp.raise_for_status()
            raw = resp.json().get("data", [])

            # Parse results - justETF returns HTML snippets in arrays
            results = []
            for item in raw:
                parsed = self._parse_search_result(item)
                if parsed:
                    results.append(parsed)

            _etf_cache[cache_key] = {"data": results, "ts": datetime.now()}
            return results
        except Exception:
            return []

    def _parse_search_result(self, item) -> dict | None:
        """Parse a single search result from justETF API response."""
        if not isinstance(item, (list, dict)):
            return None

        result = {}
        raw = item if isinstance(item, list) else [item]

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
