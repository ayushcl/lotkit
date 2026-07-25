from fastapi.testclient import TestClient

from api.main import app

client = TestClient(app)


def test_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "service": "lotkit"}


def test_root() -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "LotKit" in response.text
