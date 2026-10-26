from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class DevicePairRequest(BaseModel):
    """
    Request payload for `POST /api/v1/auth/pair`.
    - Strictly forbids extra fields (such as `patient_id` or `scope`).
    - `pairing_code` is marked `repr=False` so it is never included in repr/logs.
    """
    model_config = ConfigDict(extra="forbid")

    pairing_code: str = Field(
        ...,
        min_length=1,
        max_length=32,
        repr=False,
        description="Single-use 6-digit pairing code issued via local Admin CLI",
    )
    device_id: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="Optional client-generated device identifier (validated as untrusted input)",
    )
    device_name: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="Human-readable device label (e.g. 'Pixel 8 Pro')",
    )
    platform: str = Field(
        default="android",
        min_length=1,
        max_length=32,
        description="Device platform identifier",
    )

    @field_validator("pairing_code")
    @classmethod
    def _validate_six_digit_code(cls, value: str) -> str:
        clean = value.strip()
        if len(clean) != 6 or not clean.isdigit() or not clean.isascii():
            raise ValueError("pairing_code must be an exact 6-digit numeric string.")
        return clean

    @field_validator("device_name", "platform")
    @classmethod
    def _validate_non_blank(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("Field must not be blank.")
        return clean

    @field_validator("device_id")
    @classmethod
    def _validate_device_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        clean = value.strip()
        if not clean:
            raise ValueError("device_id must not be blank when provided.")
        if any(ord(ch) < 32 or ord(ch) > 126 for ch in clean):
            raise ValueError("device_id contains invalid characters.")
        return clean


class DevicePairResponse(BaseModel):
    """Response payload for `POST /api/v1/auth/pair` (`Cache-Control: no-store`)."""
    access_token: str = Field(..., repr=False)
    token_type: str = Field(default="Bearer")
    expires_in: int
    expires_at: datetime
    refresh_token: str = Field(..., repr=False)
    refresh_expires_at: datetime
    patient_id: str
    device_id: str
    scope: str


class TokenRefreshRequest(BaseModel):
    """Request payload for `POST /api/v1/auth/refresh`."""
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(..., min_length=1, max_length=64)
    refresh_token: str = Field(..., min_length=1, max_length=256, repr=False)

    @field_validator("device_id", "refresh_token")
    @classmethod
    def _validate_non_blank(cls, value: str) -> str:
        clean = value.strip()
        if not clean:
            raise ValueError("Field must not be blank.")
        return clean


class TokenRefreshResponse(BaseModel):
    """Response payload for `POST /api/v1/auth/refresh` (`Cache-Control: no-store`)."""
    access_token: str = Field(..., repr=False)
    token_type: str = Field(default="Bearer")
    expires_in: int
    expires_at: datetime
    refresh_token: str = Field(..., repr=False)
    refresh_expires_at: datetime
    patient_id: str
    device_id: str
    scope: str


class DeviceRevokeRequest(BaseModel):
    """
    Optional request payload for `POST /api/v1/auth/revoke`.
    Allows self-revocation either via Bearer `DeviceSession` (optionally verifying
    matching `device_id`) or via `(device_id, refresh_token)` when the short-lived
    access token has already expired.
    """
    model_config = ConfigDict(extra="forbid")

    device_id: Optional[str] = Field(default=None, min_length=1, max_length=64)
    refresh_token: Optional[str] = Field(default=None, min_length=1, max_length=256, repr=False)


class DeviceRevokeResponse(BaseModel):
    """Response payload for `POST /api/v1/auth/revoke` (`Cache-Control: no-store`)."""
    status: str = Field(default="revoked")
    device_id: str
    revoked: bool = Field(default=True)
