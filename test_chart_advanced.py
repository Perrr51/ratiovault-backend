#!/usr/bin/env python3
"""
Test script for advanced chart features:
- Technical indicators (SMA 20, SMA 50, RSI, MACD, Bollinger Bands)
- Cache functionality
- Ticker comparison
- CSV export
"""

import requests
import time
import json

BASE_URL = "http://localhost:8000"

def test_basic_chart():
    """Test basic chart without indicators"""
    print("\n" + "="*60)
    print("TEST 1: Basic Chart (AAPL, 1M)")
    print("="*60)

    url = f"{BASE_URL}/chart?ticker=AAPL&interval=1M"
    start = time.time()
    response = requests.get(url)
    elapsed = time.time() - start

    if response.status_code == 200:
        data = response.json()
        print(f"✅ Status: {response.status_code}")
        print(f"⏱️  Response time: {elapsed:.3f}s")
        print(f"📊 Data points: {len(data['timestamps'])}")
        print(f"💰 First price: ${data['prices'][0]:.2f}")
        print(f"💰 Last price: ${data['prices'][-1]:.2f}")
        print(f"📈 Indicators: {list(data.get('indicators', {}).keys())}")
        return True
    else:
        print(f"❌ Failed: {response.status_code}")
        print(response.text)
        return False


def test_chart_with_sma():
    """Test chart with SMA indicators"""
    print("\n" + "="*60)
    print("TEST 2: Chart with SMA 20 + SMA 50")
    print("="*60)

    url = f"{BASE_URL}/chart?ticker=TSLA&interval=3M&indicators=sma20,sma50"
    start = time.time()
    response = requests.get(url)
    elapsed = time.time() - start

    if response.status_code == 200:
        data = response.json()
        print(f"✅ Status: {response.status_code}")
        print(f"⏱️  Response time: {elapsed:.3f}s")
        print(f"📊 Data points: {len(data['timestamps'])}")

        # Check SMA indicators
        if 'sma20' in data['indicators']:
            sma20_vals = [v for v in data['indicators']['sma20'] if v is not None]
            print(f"📈 SMA 20: {len(sma20_vals)} values, last = ${sma20_vals[-1]:.2f}")

        if 'sma50' in data['indicators']:
            sma50_vals = [v for v in data['indicators']['sma50'] if v is not None]
            print(f"📈 SMA 50: {len(sma50_vals)} values, last = ${sma50_vals[-1]:.2f}")

        return True
    else:
        print(f"❌ Failed: {response.status_code}")
        print(response.text)
        return False


def test_chart_with_bollinger():
    """Test chart with Bollinger Bands"""
    print("\n" + "="*60)
    print("TEST 3: Chart with Bollinger Bands")
    print("="*60)

    url = f"{BASE_URL}/chart?ticker=MSFT&interval=1M&indicators=bb"
    start = time.time()
    response = requests.get(url)
    elapsed = time.time() - start

    if response.status_code == 200:
        data = response.json()
        print(f"✅ Status: {response.status_code}")
        print(f"⏱️  Response time: {elapsed:.3f}s")
        print(f"📊 Data points: {len(data['timestamps'])}")

        # Check Bollinger Bands
        if 'bb_upper' in data['indicators']:
            bb_upper_vals = [v for v in data['indicators']['bb_upper'] if v is not None]
            bb_middle_vals = [v for v in data['indicators']['bb_middle'] if v is not None]
            bb_lower_vals = [v for v in data['indicators']['bb_lower'] if v is not None]
            print(f"📈 BB Upper: last = ${bb_upper_vals[-1]:.2f}")
            print(f"📈 BB Middle: last = ${bb_middle_vals[-1]:.2f}")
            print(f"📈 BB Lower: last = ${bb_lower_vals[-1]:.2f}")
            print(f"📏 BB Width: ${bb_upper_vals[-1] - bb_lower_vals[-1]:.2f}")

        return True
    else:
        print(f"❌ Failed: {response.status_code}")
        print(response.text)
        return False


def test_chart_with_all_indicators():
    """Test chart with all indicators"""
    print("\n" + "="*60)
    print("TEST 4: Chart with ALL Indicators")
    print("="*60)

    url = f"{BASE_URL}/chart?ticker=AAPL&interval=1Y&indicators=sma20,sma50,bb,rsi,macd"
    start = time.time()
    response = requests.get(url)
    elapsed = time.time() - start

    if response.status_code == 200:
        data = response.json()
        print(f"✅ Status: {response.status_code}")
        print(f"⏱️  Response time: {elapsed:.3f}s")
        print(f"📊 Data points: {len(data['timestamps'])}")
        print(f"📈 Indicators loaded: {list(data.get('indicators', {}).keys())}")

        # Check each indicator
        indicators = data.get('indicators', {})

        if 'rsi' in indicators:
            rsi_vals = [v for v in indicators['rsi'] if v is not None]
            print(f"📊 RSI: last = {rsi_vals[-1]:.2f}")

        if 'macd' in indicators:
            macd_data = indicators['macd']
            if isinstance(macd_data, dict):
                macd_vals = [v for v in macd_data.get('macd', []) if v is not None]
                signal_vals = [v for v in macd_data.get('signal', []) if v is not None]
                if macd_vals:
                    print(f"📊 MACD: last = {macd_vals[-1]:.4f}")
                if signal_vals:
                    print(f"📊 MACD Signal: last = {signal_vals[-1]:.4f}")

        return True
    else:
        print(f"❌ Failed: {response.status_code}")
        print(response.text)
        return False


def test_cache_functionality():
    """Test that cache works (second request should be faster)"""
    print("\n" + "="*60)
    print("TEST 5: Cache Functionality")
    print("="*60)

    url = f"{BASE_URL}/chart?ticker=NVDA&interval=1M&indicators=sma20,sma50"

    # First request (cache miss)
    print("🔄 First request (cache miss expected)...")
    start1 = time.time()
    response1 = requests.get(url)
    elapsed1 = time.time() - start1

    if response1.status_code == 200:
        print(f"✅ First request: {elapsed1:.3f}s")
    else:
        print(f"❌ First request failed: {response1.status_code}")
        return False

    # Second request (cache hit)
    time.sleep(0.5)  # Small delay
    print("🔄 Second request (cache hit expected)...")
    start2 = time.time()
    response2 = requests.get(url)
    elapsed2 = time.time() - start2

    if response2.status_code == 200:
        print(f"✅ Second request: {elapsed2:.3f}s")
        speedup = elapsed1 / elapsed2 if elapsed2 > 0 else 0
        print(f"🚀 Speedup: {speedup:.1f}x faster")

        if elapsed2 < elapsed1:
            print("✅ Cache is working! (second request faster)")
            return True
        else:
            print("⚠️  Cache might not be working (second request not faster)")
            return False
    else:
        print(f"❌ Second request failed: {response2.status_code}")
        return False


def test_comparison_endpoint():
    """Test ticker comparison endpoint"""
    print("\n" + "="*60)
    print("TEST 6: Ticker Comparison (AAPL, MSFT, GOOGL)")
    print("="*60)

    url = f"{BASE_URL}/chart/compare?tickers=AAPL,MSFT,GOOGL&interval=1M"
    start = time.time()
    response = requests.get(url)
    elapsed = time.time() - start

    if response.status_code == 200:
        data = response.json()
        print(f"✅ Status: {response.status_code}")
        print(f"⏱️  Response time: {elapsed:.3f}s")
        print(f"📊 Tickers: {len(data)}")

        for ticker, ticker_data in data.items():
            if 'prices' in ticker_data and ticker_data['prices']:
                prices = ticker_data['prices']
                print(f"  • {ticker}: {len(prices)} points, last = ${prices[-1]:.2f}")
            else:
                print(f"  • {ticker}: No data")

        return True
    else:
        print(f"❌ Failed: {response.status_code}")
        print(response.text)
        return False


def test_export_endpoint():
    """Test CSV export endpoint"""
    print("\n" + "="*60)
    print("TEST 7: CSV Export")
    print("="*60)

    url = f"{BASE_URL}/chart/export?ticker=AAPL&interval=1M"
    start = time.time()
    response = requests.get(url)
    elapsed = time.time() - start

    if response.status_code == 200:
        print(f"✅ Status: {response.status_code}")
        print(f"⏱️  Response time: {elapsed:.3f}s")
        print(f"📄 Content-Type: {response.headers.get('Content-Type')}")
        print(f"📦 Content size: {len(response.content)} bytes")

        # Show first few lines
        lines = response.text.split('\n')[:5]
        print(f"\n📝 First 5 lines of CSV:")
        for line in lines:
            print(f"   {line}")

        return True
    else:
        print(f"❌ Failed: {response.status_code}")
        print(response.text)
        return False


def main():
    print("\n" + "🎯 "* 30)
    print("CHART ADVANCED FEATURES - COMPREHENSIVE TEST SUITE")
    print("🎯 " * 30)

    tests = [
        ("Basic Chart", test_basic_chart),
        ("SMA Indicators", test_chart_with_sma),
        ("Bollinger Bands", test_chart_with_bollinger),
        ("All Indicators", test_chart_with_all_indicators),
        ("Cache", test_cache_functionality),
        ("Comparison", test_comparison_endpoint),
        ("CSV Export", test_export_endpoint),
    ]

    results = []

    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
        except Exception as e:
            print(f"❌ Exception in {name}: {e}")
            results.append((name, False))

    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status}: {name}")

    print(f"\n📊 Total: {passed}/{total} tests passed ({passed/total*100:.1f}%)")

    if passed == total:
        print("\n🎉 ALL TESTS PASSED! 🎉")
    else:
        print(f"\n⚠️  {total - passed} test(s) failed")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️  Tests interrupted by user")
    except requests.exceptions.ConnectionError:
        print("\n\n❌ ERROR: Could not connect to backend server")
        print("Make sure the backend is running:")
        print("  cd api")
        print("  uvicorn main:app --reload --port 8000")
