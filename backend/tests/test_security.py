from fastapi.testclient import TestClient
from app.config import settings


def test_no_secrets_in_health_response(client: TestClient):
    """
    Ensure that GEMINI_API_KEY, device tokens, and database credentials
    are strictly excluded from all public API responses.
    """
    response = client.get("/health")
    assert response.status_code == 200

    content_str = response.text

    # Verify secret settings are not exposed in response body
    if settings.gemini_api_key:
        assert settings.gemini_api_key not in content_str
    assert settings.api_secret not in content_str
    assert "password" not in content_str.lower()
    assert "gemini_api_key" not in content_str.lower()
