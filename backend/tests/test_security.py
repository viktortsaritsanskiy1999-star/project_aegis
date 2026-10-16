from fastapi.testclient import TestClient
from app.config import settings


def test_no_secrets_in_health_response(client: TestClient):
    """
    Ensure that GEMINI_API_KEY, device tokens, and database credentials
    are strictly excluded from public API responses.
    """
    response = client.get("/health")
    assert response.status_code == 200

    content_str = response.text

    if settings.gemini_api_key:
        assert settings.gemini_api_key not in content_str
    assert settings.api_secret not in content_str
    assert "password" not in content_str.lower()
    assert "gemini_api_key" not in content_str.lower()


def test_unauthenticated_client_is_rejected_on_protected_endpoints(client: TestClient):
    """
    Ensure that requests to protected endpoints without Bearer token
    are rejected with 401 Unauthorized.
    """
    # 1. Without Authorization header
    resp = client.post("/api/v1/cycle/start", json={"cycle_id": "cycle_unauth_test"})
    assert resp.status_code == 401
    assert "detail" in resp.json()

    # 2. With invalid / forged token
    resp_invalid = client.post(
        "/api/v1/cycle/start",
        json={"cycle_id": "cycle_unauth_test"},
        headers={"Authorization": "Bearer forged_invalid_token_xyz"},
    )
    assert resp_invalid.status_code == 401


def test_authenticated_client_with_valid_token_is_accepted(
    client: TestClient,
    auth_headers: dict[str, str],
):
    """
    Ensure that requests with valid Bearer token are accepted.
    """
    resp = client.post(
        "/api/v1/cycle/start",
        json={"cycle_id": "cycle_auth_success"},
        headers=auth_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["cycle_id"] == "cycle_auth_success"
