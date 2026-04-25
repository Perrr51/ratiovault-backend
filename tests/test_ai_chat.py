"""B-006: /ai/chat must signal coming_soon, not the misleading mode='mock'."""

from fastapi.testclient import TestClient

from main import app


def test_ai_chat_indicates_coming_soon():
    client = TestClient(app)
    resp = client.post(
        "/ai/chat",
        json={"message": "hola", "positions": []},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("coming_soon") is True
    assert "mode" not in body
    # Sanity: the placeholder still produces a non-empty content string.
    assert isinstance(body.get("content"), str) and body["content"]
