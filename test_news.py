#!/usr/bin/env python3
"""
Script de prueba para verificar que el endpoint /news funciona correctamente
"""

from main import get_news

# Probar con varios tickers
test_tickers = ["AAPL", "MSFT", "GOOGL"]

for ticker in test_tickers:
    print(f"\n{'='*60}")
    print(f"Testing news for {ticker}")
    print(f"{'='*60}")

    try:
        news = get_news(ticker)
        print(f"✓ Found {len(news)} news articles")

        if news:
            first_article = news[0]
            print(f"\nFirst article:")
            print(f"  ID: {first_article.get('id')}")
            print(f"  Headline: {first_article.get('headline')[:80]}...")
            print(f"  Source: {first_article.get('source')}")
            print(f"  URL: {first_article.get('url')}")
            print(f"  Image: {'Yes' if first_article.get('image') else 'No'}")
            print(f"  Impact Score: {first_article.get('impactScore')}")
            print(f"  Sentiment: {first_article.get('sentiment')}")
        else:
            print("✗ No news found")

    except Exception as e:
        print(f"✗ Error: {e}")
        import traceback
        traceback.print_exc()

print(f"\n{'='*60}")
print("Test completed!")
print(f"{'='*60}")
