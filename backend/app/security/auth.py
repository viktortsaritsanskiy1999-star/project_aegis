import secrets
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.config import settings

# HTTPBearer security scheme
security_scheme = HTTPBearer(auto_error=False)


def verify_client_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
) -> str:
    """
    Validates client device authorization token against server secret.
    Rejects missing, empty, or mismatched tokens with 401 Unauthorized.
    Uses constant-time comparison to protect against timing attacks.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed authorization header. Expected Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    configured_secret = settings.api_secret.strip()
    if not configured_secret:
        # Deny access if server administrator has not configured a secret
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed: server API secret is unconfigured.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Constant-time comparison
    is_valid = secrets.compare_digest(
        credentials.credentials.strip(),
        configured_secret,
    )

    if not is_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid client authentication token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return credentials.credentials
