from datetime import datetime, timezone
from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Health check payload showing server status and database connectivity."""
    status: str = Field(default="ok", description="Server operational status")
    app_name: str = Field(description="Name of the backend service")
    app_version: str = Field(description="Backend semantic version")
    environment: str = Field(description="Operating environment")
    database: str = Field(description="Database connectivity status")
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Current UTC timestamp",
    )
