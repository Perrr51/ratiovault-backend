from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

def test_get_quotes():
    response = client.get("/quotes?tickers=AAPL")
    assert response.status_code == 200
    data = response.json()
    assert "AAPL" in data
    assert "price" in data["AAPL"]
