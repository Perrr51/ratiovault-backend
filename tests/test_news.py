"""B-002: /news must not return fake sentiment/impactScore.

Until real AI-driven sentiment lands (v1.1), the fields are pinned to None
so the frontend doesn't render fabricated signal as truth.
"""

from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from main import app
from routers import asset_info


_FAKE_NEWS_PAYLOAD = [
    {
        "id": "abc123",
        "content": {
            "title": "Apple ships big thing",
            "provider": {"displayName": "Reuters"},
            "canonicalUrl": {"url": "https://example.com/a"},
            "pubDate": "2026-04-25T10:00:00Z",
            "summary": "summary text",
            "contentType": "STORY",
        },
    }
]


def _ticker_with_news():
    """Build a yfinance-Ticker-like mock with a single news item."""
    t = MagicMock()
    t.news = _FAKE_NEWS_PAYLOAD
    t.info = {"shortName": "Apple Inc."}
    return t


def test_get_news_does_not_return_random_sentiment():
    client = TestClient(app)
    with patch.object(asset_info.yf, "Ticker", return_value=_ticker_with_news()):
        resp = client.get("/news", params={"ticker": "AAPL"})

    assert resp.status_code == 200
    body = resp.json()
    # B-011: response is now an envelope with counts.
    assert isinstance(body, dict)
    articles = body["articles"]
    assert isinstance(articles, list)
    assert articles, "expected at least one article from the mocked feed"
    for article in articles:
        # Fields are still present in the response shape but explicitly null,
        # signalling "no real sentiment data" to consumers.
        assert article.get("sentiment") is None
        assert article.get("impactScore") is None


def test_news_envelope_when_filter_drops_everything():
    """B-011: when every article is off-topic, total reflects the raw feed
    size and filtered is zero — caller can decide whether to surface a
    hint, fall back to a broader query, etc."""
    # Ten articles that all relate exclusively to TSLA, queried for AAPL.
    payload = [
        {
            "id": f"abc-{i}",
            "relatedTickers": ["TSLA"],
            "content": {
                "title": "Tesla quarterly review",
                "provider": {"displayName": "Reuters"},
                "canonicalUrl": {"url": f"https://example.com/{i}"},
                "pubDate": "2026-04-25T10:00:00Z",
                "summary": "summary",
                "contentType": "STORY",
            },
        }
        for i in range(10)
    ]
    fake_ticker = MagicMock()
    fake_ticker.news = payload
    fake_ticker.info = {"shortName": "Apple Inc."}

    client = TestClient(app)
    with patch.object(asset_info.yf, "Ticker", return_value=fake_ticker):
        resp = client.get("/news", params={"ticker": "AAPL"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["articles"] == []
    assert body["total"] == 10
    assert body["filtered"] == 0


def test_news_module_does_not_import_random():
    # Defensive: the prior implementation imported `random` to fabricate
    # the values. Make sure nothing in the call path silently re-introduces it.
    import inspect

    source = inspect.getsource(asset_info.get_news)
    assert "random." not in source, "random.* must not be used in get_news (B-002)"
