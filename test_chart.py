#!/usr/bin/env python3
"""
Test script for chart endpoint
"""

import requests
import json

BASE_URL = "http://localhost:8000"

def test_chart_endpoint(ticker, interval):
    """Test the /chart endpoint"""
    print(f"\n{'='*60}")
    print(f"Testing: {ticker} - Interval: {interval}")
    print(f"{'='*60}")

    try:
        url = f"{BASE_URL}/chart?ticker={ticker}&interval={interval}"
        print(f"URL: {url}")

        response = requests.get(url)
        print(f"Status Code: {response.status_code}")

        if response.status_code == 200:
            data = response.json()

            if data.get('error'):
                print(f"✗ Error in response: {data['error']}")
                return False

            print(f"✓ Success!")
            print(f"  Data points: {len(data.get('timestamps', []))}")

            if len(data.get('timestamps', [])) > 0:
                print(f"\n  First timestamp: {data['timestamps'][0]}")
                print(f"  First price: ${data['prices'][0]:.2f}")
                print(f"  Last timestamp: {data['timestamps'][-1]}")
                print(f"  Last price: ${data['prices'][-1]:.2f}")

                price_change = data['prices'][-1] - data['prices'][0]
                price_change_pct = (price_change / data['prices'][0]) * 100
                print(f"\n  Price change: ${price_change:.2f} ({price_change_pct:+.2f}%)")

            return True
        else:
            print(f"✗ Error: {response.status_code}")
            print(f"Response: {response.text}")
            return False

    except Exception as e:
        print(f"✗ Exception: {e}")
        return False


def main():
    print("""
╔═══════════════════════════════════════════════════════════╗
║           Chart Endpoint Test Suite                      ║
║                                                           ║
║  Testing /chart endpoint with different intervals        ║
╚═══════════════════════════════════════════════════════════╝
""")

    tickers = ["AAPL", "MSFT"]
    intervals = ["1D", "1W", "1M", "3M", "1Y"]

    results = []

    for ticker in tickers:
        print(f"\n{'#'*60}")
        print(f"# Testing ticker: {ticker}")
        print(f"{'#'*60}")

        for interval in intervals:
            success = test_chart_endpoint(ticker, interval)
            results.append({
                'ticker': ticker,
                'interval': interval,
                'success': success
            })

    # Summary
    print(f"\n\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")

    total = len(results)
    passed = sum(1 for r in results if r['success'])
    failed = total - passed

    print(f"Total tests: {total}")
    print(f"✓ Passed: {passed}")
    print(f"✗ Failed: {failed}")

    if failed > 0:
        print(f"\nFailed tests:")
        for r in results:
            if not r['success']:
                print(f"  - {r['ticker']} / {r['interval']}")

    print("""
╔═══════════════════════════════════════════════════════════╗
║                   Test Suite Complete                     ║
╚═══════════════════════════════════════════════════════════╝

Next steps:
1. If all tests passed, the chart endpoint is working!
2. Start the frontend and test the PriceChart component
3. Verify the graph renders correctly in Fundamentales page
""")


if __name__ == "__main__":
    main()
