"""SEC-1 Item 1.6 — URL-encoding of Stooq ticker.

TDD RED: written BEFORE applying quote() wrapper.
RED proof: with raw f-string, a ticker containing special chars (e.g. "a&b c")
appears verbatim in the URL → assertion `"a&b c" not in captured_url` fails.
After `quote(stooq_ticker, safe='')`, the ticker is percent-encoded → GREEN.
"""
from unittest.mock import patch, MagicMock

import stooq


def test_quote_url_percent_encodes_ticker(monkeypatch):
    """fetch_stooq_quote must percent-encode special chars in the stooq ticker."""
    monkeypatch.setattr(stooq, "yahoo_to_stooq_ticker", lambda t: "a&b c")
    captured = {}

    class FakeResp:
        status_code = 200
        text = "Symbol,Date,Time,Open,High,Low,Close,Volume,Name\n"

    def fake_get(url, **kw):
        captured["url"] = url
        return FakeResp()

    fake_client = MagicMock()
    fake_client.__enter__.return_value.get.side_effect = fake_get

    with patch("stooq.httpx.Client", return_value=fake_client):
        stooq.fetch_stooq_quote("XAUUSD=X")

    assert "url" in captured, "httpx.Client.get was never called"
    assert "a%26b%20c" in captured["url"], (
        f"Expected percent-encoded ticker in URL, got: {captured['url']}"
    )
    assert "a&b c" not in captured["url"], (
        f"Raw unencoded ticker found in URL: {captured['url']}"
    )


def test_history_url_percent_encodes_ticker(monkeypatch):
    """fetch_stooq_history must percent-encode special chars in the stooq ticker."""
    monkeypatch.setattr(stooq, "yahoo_to_stooq_ticker", lambda t: "a&b c")
    captured = {}

    class FakeResp:
        status_code = 200
        text = "Symbol,Date,Time,Open,High,Low,Close,Volume,Name\n"

    def fake_get(url, **kw):
        captured["url"] = url
        return FakeResp()

    fake_client = MagicMock()
    fake_client.__enter__.return_value.get.side_effect = fake_get

    with patch("stooq.httpx.Client", return_value=fake_client):
        stooq.fetch_stooq_history("XAUUSD=X", "2024-01-01", "2024-01-31")

    assert "url" in captured, "httpx.Client.get was never called"
    assert "a%26b%20c" in captured["url"], (
        f"Expected percent-encoded ticker in URL, got: {captured['url']}"
    )
    assert "a&b c" not in captured["url"], (
        f"Raw unencoded ticker found in URL: {captured['url']}"
    )
