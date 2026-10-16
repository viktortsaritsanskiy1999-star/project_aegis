from fastapi.testclient import TestClient


def test_health_endpoint(client: TestClient):
    """Verify that GET /health returns 200 with operational status and database connected."""
    response = client.get("/health")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "ok"
    assert data["database"] == "connected"
    assert "app_name" in data
    assert "app_version" in data
    assert "environment" in data
    assert "timestamp" in data
