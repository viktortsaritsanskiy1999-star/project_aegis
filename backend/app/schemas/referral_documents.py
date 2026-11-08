"""
Stage 8A.3 — Client Referral Document Export and Presentation Schemas.

Defines client-facing data contracts for listing, viewing, and exporting
referral doctor observation documents across all medical cycles.
Enforces strict schema validation (extra='forbid') and zero oracle leakage.
"""

from typing import Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, ConfigDict, Field


class ClientReferralDocumentItem(BaseModel):
    """Client-safe summary item representing an observation document for doctor."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    report_id: str = Field(..., description="Unique client-facing document report ID")
    cycle_id: str = Field(..., description="Parent medical cycle ID")
    sprint_id: Optional[str] = Field(default=None, description="Sprint ID in which referral was confirmed")
    referral_id: str = Field(..., description="Unique clinical referral ID")
    created_at_utc: str = Field(..., description="ISO 8601 UTC creation timestamp")
    report_date: str = Field(..., description="Date of document in YYYY-MM-DD format")
    title: str = Field(..., description="Canonical document title")
    available_formats: List[str] = Field(
        default_factory=lambda: ["pdf", "docx", "md"],
        description="Supported export formats for download",
    )
    summary: str = Field(..., description="Executive summary prepared for patient")
    urgency: str = Field(default="routine", description="Consultation urgency: routine or priority")
    target_specialties: List[str] = Field(default_factory=list, description="Recommended clinician specialties")
    cycle_status: str = Field(default="active", description="Status of the medical cycle: active, completed, or archived")
    document_version: int = Field(default=1, ge=1, description="Version of this client report")


class ClientReferralDocumentListResponse(BaseModel):
    """List of all doctor observation documents across patient's medical cycles."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    patient_id: str
    total_count: int = Field(..., ge=0)
    documents: List[ClientReferralDocumentItem] = Field(default_factory=list)


class ClientReferralDocumentSection(BaseModel):
    """Structured clinical observation section for mobile Safe UI display."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    section_number: int = Field(..., ge=1, le=7)
    section_title: str
    content: Union[str, List[str]]
    is_bullet_list: bool = False


class ClientReferralDocumentDetailResponse(BaseModel):
    """Complete client-safe structured view of doctor document for mobile Safe."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    report_id: str
    cycle_id: str
    sprint_id: Optional[str] = None
    referral_id: str
    created_at_utc: str
    report_date: str
    title: str
    urgency: str
    target_specialties: List[str] = Field(default_factory=list)
    informational_disclaimer: str
    executive_summary: str
    sections: List[ClientReferralDocumentSection] = Field(default_factory=list)
    rendered_markdown: str
    available_formats: List[str] = Field(default_factory=lambda: ["pdf", "docx", "md"])
    cycle_status: str = "active"
    document_version: int = 1
