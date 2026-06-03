"""Phase 0 — Scraper Validation Gate (live network, opt-in).

Run with:
    python -m pytest -m external tests/external/test_isin_gate.py -v -s

These tests require live network access to justETF.
They are excluded from the default suite via the `external` marker.

Covers: REQ-1-A, REQ-1-B, REQ-1-C.

For each of three known EU ETFs (VWCE.DE, VUSA.L, EUNL.DE) the harness:
  1. Calls JustETFScraper.search_etfs(root) and records how many distinct
     ISINs come back.
  2. If exactly 1 ISIN is found, calls get_etf_profile(isin) and records
     the ETF name + fundCurrency.
  3. Asserts that the resolved ISIN matches the known-correct value.
  4. Saves the raw HTML search response to tests/fixtures/ for use by the
     offline parse tests in Phase 2.
  5. Emits a per-ticker GO / NO-GO verdict and an overall gate verdict.

NOTE — Scraper drift detected during Phase 0 (2026-06-03):
    justETF migrated from returning HTML-snippet arrays to a Wicket/DataTables
    JSON API. The current search_etfs() implementation returns 0 results for
    all queries. The profile endpoint (get_etf_profile) still works.
    Result: GATE = NO-GO (scraper requires update before Phase 2).
    The fixture HTML is recorded for inspection.
    EUNL.DE ISIN: spec said IE00F4030284 but live API returns IE00B4L5Y983
    (iShares Core MSCI World UCITS ETF USD Acc). Spec needs correction.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Known-correct reference data (REQ-1-A)
# Note: EUNL expected ISIN corrected from spec value (IE00F4030284 does not
# exist on justETF; live data returns IE00B4L5Y983).
# ---------------------------------------------------------------------------
EXPECTED = {
    "VWCE": {
        "isin": "IE00BK5BQT80",
        "ticker_full": "VWCE.DE",
    },
    "VUSA": {
        "isin": "IE00B3XXRP09",
        "ticker_full": "VUSA.L",
    },
    "EUNL": {
        # Spec had IE00F4030284 but live justETF returns IE00B4L5Y983
        # (iShares Core MSCI World UCITS ETF USD Acc). Updated.
        "isin": "IE00B4L5Y983",
        "ticker_full": "EUNL.DE",
    },
}

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _distinct_isins(results: list[dict]) -> list[str]:
    """Return deduplicated ISINs from a search_etfs result list."""
    seen: set[str] = set()
    out: list[str] = []
    for r in results:
        isin = r.get("isin")
        if isin and isin not in seen:
            seen.add(isin)
            out.append(isin)
    return out


def _save_fixture(name: str, content: str) -> None:
    """Write raw content to tests/fixtures/<name> (overwrite on each live run)."""
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    path = FIXTURES_DIR / name
    path.write_text(content, encoding="utf-8")
    print(f"  [fixture] saved {path} ({len(content)} chars)")


def _capture_search_html(root: str, scraper) -> str | None:
    """Re-issue the search request to capture the raw justETF response.

    Note: as of 2026-06-03 justETF migrated to a Wicket/DataTables JSON API.
    The fetchCallbackUrl is session-scoped and requires a fresh GET to
    /en/search.html first to obtain the Wicket component URL.
    This helper captures whatever is returned, whether HTML or JSON.
    """
    try:
        import httpx as _httpx

        base = "https://www.justetf.com/en"
        session = scraper.session

        # Get the search page to extract the Wicket fetchCallbackUrl
        resp_page = session.get(f"{base}/search.html")
        resp_page.raise_for_status()
        page_body = resp_page.text

        # Extract the fetchCallbackUrl (Wicket DataTables source)
        cb_match = re.search(r'"fetchCallbackUrl":"([^"]+)"', page_body)

        if cb_match:
            callback_url = "https://www.justetf.com" + cb_match.group(1)
            payload = {
                "draw": "1",
                "start": "0",
                "length": "-1",
                "search[value]": "",
                "search[regex]": "false",
                "lang": "en",
                "country": "DE",
                "universeType": "private",
                "defaultCurrency": "EUR",
            }
            resp_data = session.post(callback_url, data=payload)
            resp_data.raise_for_status()
            return resp_data.text
        else:
            # Fallback: return the search page HTML itself for diagnosis
            print(f"  [fixture] WARNING: fetchCallbackUrl not found in page for {root}")
            return page_body

    except Exception as exc:  # noqa: BLE001
        print(f"  [fixture] WARNING: could not capture HTML for {root}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Parametrized gate test
# ---------------------------------------------------------------------------

@pytest.mark.external
@pytest.mark.parametrize("root", list(EXPECTED.keys()))
def test_isin_gate_per_ticker(root: str) -> None:
    """Live gate: search_etfs(root) resolves to the correct single ISIN.

    Per-ticker assertions:
      (a) search returned at least 1 result
      (b) exactly 1 distinct ISIN
      (c) the resolved ISIN matches the known-correct value
      (d) get_etf_profile returns a name + fundCurrency
    """
    from justetf import JustETFScraper

    expected = EXPECTED[root]
    scraper = JustETFScraper()

    print(f"\n{'='*60}")
    print(f"[gate] querying justETF for root={root!r} ({expected['ticker_full']})")

    # ── Step 1: search ───────────────────────────────────────────────────────
    results = scraper.search_etfs(root)
    isins = _distinct_isins(results)

    print(f"  search_etfs() results count : {len(results)}")
    print(f"  distinct ISINs found        : {isins}")

    # Save fixture for Phase 2 offline parse tests
    # Capture the raw API response (full ETF dataset as JSON)
    raw_content = _capture_search_html(root, scraper)
    if raw_content:
        # All three ETFs share the same full-list response;
        # save per-ticker for clarity (Phase 2 will use them for parse tests)
        fixture_name = f"justetf_{root.lower()}_search.html"
        _save_fixture(fixture_name, raw_content)

    # (a) at least 1 result
    assert len(results) >= 1, (
        f"NO-GO: search_etfs({root!r}) returned 0 results.\n"
        f"Scraper drift suspected: justETF migrated from HTML-snippet arrays\n"
        f"to a Wicket/DataTables JSON API. The _parse_search_result() helper\n"
        f"expects list/array items but now receives dict objects.\n"
        f"Fix required in search_etfs() before Phase 2 can proceed."
    )

    # (b) exactly 1 distinct ISIN
    assert len(isins) == 1, (
        f"NO-GO: search_etfs({root!r}) returned {len(isins)} distinct ISINs: {isins} — "
        "ambiguous result, confidence gate would REJECT"
    )

    resolved_isin = isins[0]

    # (c) ISIN matches known-correct value (REQ-1-A)
    assert resolved_isin == expected["isin"], (
        f"NO-GO: resolved ISIN {resolved_isin!r} does not match "
        f"expected {expected['isin']!r} for {root}"
    )

    print(f"  resolved ISIN : {resolved_isin}  [MATCH]")

    # ── Step 2: profile ──────────────────────────────────────────────────────
    profile = scraper.get_etf_profile(resolved_isin)

    if profile:
        name = profile.get("name", "<no name>")
        fund_currency = profile.get("fundCurrency", "<no fundCurrency>")
        print(f"  profile name  : {name}")
        print(f"  fundCurrency  : {fund_currency}")

        assert profile.get("name"), (
            f"WARNING: profile for {resolved_isin} has no name — "
            "name-token check (gate condition 3) cannot be evaluated"
        )
        assert profile.get("fundCurrency"), (
            f"NO-GO: profile for {resolved_isin} has no fundCurrency — "
            "currency check (gate condition 2) would REJECT"
        )
    else:
        pytest.fail(
            f"NO-GO: get_etf_profile({resolved_isin!r}) returned None — "
            "profile fetch failed; confidence gate would REJECT"
        )

    print(f"  verdict       : GO")


@pytest.mark.external
def test_isin_gate_overall_summary() -> None:
    """Overall gate summary — emits a consolidated GO/NO-GO verdict to stdout.

    This test also validates the fallback path: directly querying the
    justETF DataTables API (bypassing the broken search_etfs() method)
    to verify that the underlying data IS available and correct.
    This distinguishes between a scraper-code failure (fixable) vs a
    justETF data problem (harder to fix).
    """
    from justetf import JustETFScraper
    import json as _json

    scraper = JustETFScraper()

    print(f"\n{'='*60}")
    print("PHASE 0 GATE — OVERALL SUMMARY")
    print(f"{'='*60}")

    # ── Part 1: Current search_etfs() verdict ────────────────────────────────
    print("\n[A] Testing search_etfs() (current scraper implementation):")
    print(f"{'Root':<8} {'Ticker':<10} {'Results':>8} {'ISINs':>6} {'Verdict'}")
    print("-" * 50)

    search_all_pass = True
    for root, expected in EXPECTED.items():
        try:
            results = scraper.search_etfs(root)
            isins = _distinct_isins(results)
            resolved = isins[0] if len(isins) == 1 else None
            match = resolved == expected["isin"] if resolved else False
            if not match:
                search_all_pass = False
            verdict = "GO" if match else "NO-GO"
            print(f"{root:<8} {expected['ticker_full']:<10} {len(results):>8} {len(isins):>6}  {verdict}")
        except Exception as exc:  # noqa: BLE001
            search_all_pass = False
            print(f"{root:<8} {expected['ticker_full']:<10}  ERROR: {exc}")

    print("-" * 50)
    print(f"search_etfs() verdict: {'GO' if search_all_pass else 'NO-GO'}")

    # ── Part 2: Direct DataTables API fallback (validates data availability) ──
    print(f"\n[B] Testing justETF DataTables API directly (scraper-bypass):")

    try:
        page_resp = scraper.session.get("https://www.justetf.com/en/search.html")
        page_resp.raise_for_status()
        page_body = page_resp.text

        cb_match = re.search(r'"fetchCallbackUrl":"([^"]+)"', page_body)
        if not cb_match:
            print("  ERROR: fetchCallbackUrl not found — justETF page structure changed")
            api_all_pass = False
        else:
            callback_url = "https://www.justetf.com" + cb_match.group(1)
            payload = {
                "draw": "1", "start": "0", "length": "-1",
                "search[value]": "", "search[regex]": "false",
                "lang": "en", "country": "DE",
                "universeType": "private", "defaultCurrency": "EUR",
            }
            api_resp = scraper.session.post(callback_url, data=payload)
            api_resp.raise_for_status()
            api_data = api_resp.json()
            all_records = api_data.get("data", [])
            by_ticker = {r.get("ticker"): r for r in all_records if r.get("ticker")}

            print(f"  Total ETF records in API: {len(all_records)}")
            print(f"{'Root':<8} {'Ticker':<10} {'API ISIN':<16} {'Expected ISIN':<16} {'Match':>6} {'fundCurrency':<14} {'Name'}")
            print("-" * 100)

            api_all_pass = True
            rows: list[dict] = []
            for root, expected in EXPECTED.items():
                record = by_ticker.get(root)
                if record:
                    got_isin = record.get("isin", "")
                    match = got_isin == expected["isin"]
                    if not match:
                        api_all_pass = False
                    name = record.get("name", "")[:45]
                    fc = record.get("fundCurrency", "")
                    match_str = "YES" if match else "NO"
                    print(
                        f"{root:<8} {expected['ticker_full']:<10} {got_isin:<16} "
                        f"{expected['isin']:<16} {match_str:>6}  {fc:<14} {name}"
                    )
                    rows.append({
                        "root": root, "isin": got_isin, "expected": expected["isin"],
                        "match": match, "name": record.get("name", ""),
                        "fundCurrency": fc,
                    })
                else:
                    api_all_pass = False
                    print(f"{root:<8} {expected['ticker_full']:<10} NOT FOUND")

    except Exception as exc:  # noqa: BLE001
        print(f"  ERROR calling DataTables API: {exc}")
        api_all_pass = False

    # ── Final verdict ────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print("PHASE 0 GATE RESULTS:")
    print(f"  search_etfs() (current code): {'GO' if search_all_pass else 'NO-GO'}")
    print(f"  DataTables API (bypass):      {'GO' if api_all_pass else 'NO-GO'}")
    print()

    if search_all_pass:
        print("OVERALL VERDICT: GO — Phase 2 is UNBLOCKED.")
    elif api_all_pass:
        print("OVERALL VERDICT: NO-GO (scraper code broken, data is available).")
        print("REQUIRED FIX: Update search_etfs() to use the Wicket/DataTables API.")
        print("  Root cause: justETF migrated response format from HTML-snippet")
        print("  arrays to structured JSON dicts. _parse_search_result() receives")
        print("  dict objects but only handles list items (line 291-292 justetf.py).")
        print("  The new API provides isin/name/fundCurrency/ticker directly.")
    else:
        print("OVERALL VERDICT: NO-GO (data not available either).")
        print("Phase 2 implementation is BLOCKED.")

    print(f"{'='*60}\n")
