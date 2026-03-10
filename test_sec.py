#!/usr/bin/env python3
"""
Test script for SEC EDGAR API endpoints
Tests the /sec/* endpoints with real tickers
"""

import requests
import json
from pprint import pprint

BASE_URL = "http://localhost:8000"

def test_endpoint(name, url):
    """Helper function to test an endpoint"""
    print(f"\n{'='*60}")
    print(f"Testing: {name}")
    print(f"{'='*60}")

    try:
        response = requests.get(url)

        print(f"Status Code: {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            print(f"✓ Success!")
            return data
        else:
            print(f"✗ Error: {response.status_code}")
            print(f"Response: {response.text}")
            return None

    except Exception as e:
        print(f"✗ Exception: {e}")
        return None


def main():
    tickers = ["AAPL", "MSFT", "TSLA"]

    print("""
╔═══════════════════════════════════════════════════════════╗
║         SEC EDGAR API Integration Test Suite             ║
║                                                           ║
║  Testing endpoints:                                       ║
║    • /sec/cik/{ticker}           - Get CIK number        ║
║    • /sec/fundamentals/{ticker}  - Get financial ratios  ║
║    • /sec/submissions/{ticker}   - Get recent filings    ║
╚═══════════════════════════════════════════════════════════╝
""")

    for ticker in tickers:
        print(f"\n\n{'#'*60}")
        print(f"# Testing ticker: {ticker}")
        print(f"{'#'*60}")

        # Test 1: Get CIK
        cik_data = test_endpoint(
            f"Get CIK for {ticker}",
            f"{BASE_URL}/sec/cik/{ticker}"
        )

        if cik_data:
            print(f"\nCIK for {ticker}: {cik_data.get('cik')}")

        # Test 2: Get Fundamentals
        fundamentals_data = test_endpoint(
            f"Get Fundamentals for {ticker}",
            f"{BASE_URL}/sec/fundamentals/{ticker}"
        )

        if fundamentals_data:
            print(f"\nFundamental Data for {ticker}:")
            print(f"  Source: {fundamentals_data.get('source')}")

            ratios = fundamentals_data.get('calculatedRatios', {})
            print(f"\n  Calculated Ratios:")
            print(f"    • Current Ratio:     {ratios.get('currentRatio')}")
            print(f"    • Debt to Equity:    {ratios.get('debtToEquity')}")
            print(f"    • Profit Margin:     {ratios.get('profitMargin')}%")
            print(f"    • Return on Equity:  {ratios.get('returnOnEquity')}%")

            raw = fundamentals_data.get('rawMetrics', {})
            print(f"\n  Key Metrics:")
            eps = raw.get('epsBasic')
            if eps:
                print(f"    • EPS (Basic):       ${eps}")
            revenue = raw.get('revenues')
            if revenue:
                print(f"    • Revenue:           ${revenue:,.0f}")
            net_income = raw.get('netIncome')
            if net_income:
                print(f"    • Net Income:        ${net_income:,.0f}")

        # Test 3: Get Recent Filings
        submissions_data = test_endpoint(
            f"Get Submissions for {ticker}",
            f"{BASE_URL}/sec/submissions/{ticker}"
        )

        if submissions_data:
            filings = submissions_data.get('filings', [])
            print(f"\nRecent SEC Filings for {ticker}:")
            print(f"  Company: {submissions_data.get('companyName')}")
            print(f"  Total filings shown: {len(filings)}")

            if filings:
                print(f"\n  Last 5 filings:")
                for filing in filings[:5]:
                    print(f"    • {filing.get('formType'):8} - {filing.get('filingDate')} (Report: {filing.get('reportDate')})")

        print(f"\n{'-'*60}\n")

    print("""
╔═══════════════════════════════════════════════════════════╗
║                   Test Suite Complete                     ║
╚═══════════════════════════════════════════════════════════╝

Next steps:
1. If all tests passed, the SEC EDGAR integration is working!
2. Frontend can now use these endpoints
3. Make sure to test with different tickers

Note: SEC API has rate limits. If you see 403 errors, you may
      need to wait before making more requests.
""")


if __name__ == "__main__":
    main()
