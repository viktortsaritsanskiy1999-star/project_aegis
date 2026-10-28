from typing import Optional
from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.chat import (
    AthenaChatRequest,
    AthenaChatResponse,
    AthenaChatHistoryResponse,
)
from app.security.auth import DeviceSession, require_device_session, require_scopes
from app.security.tokens import SCOPE_CHAT_READ, SCOPE_CHAT_WRITE
from app.services.athena_chat_service import AthenaChatService
from app.services.gemini_gateway import GeminiGateway

router = APIRouter(
    prefix="/api/v1/client/chat",
    tags=["Client Athena Chat"],
)

# Dependency injection for GeminiGateway
def get_gemini_gateway() -> GeminiGateway:
    return GeminiGateway()


@router.post(
    "/athena",
    response_model=AthenaChatResponse,
    status_code=status.HTTP_200_OK,
    summary="Interactive conversational turn with Athena wellness curator",
    description=(
        "Sends a user message, pulse alert, or daily check-in to Athena. "
        "Server strictly resolves patientId and active clinical cycle from authenticated DeviceSession. "
        "Client cannot forge or substitute foreign patient or cycle IDs. "
        "Separately persists hidden clinical reports and returns strictly sanitized conversational text."
    ),
)
async def athena_chat_turn_endpoint(
    payload: AthenaChatRequest,
    session: DeviceSession = Depends(require_device_session),
    _scope: DeviceSession = Depends(require_scopes(SCOPE_CHAT_WRITE)),
    db: Session = Depends(get_db),
    gateway: GeminiGateway = Depends(get_gemini_gateway),
) -> AthenaChatResponse:
    service = AthenaChatService(db=db, gateway=gateway)
    return await service.handle_turn(
        patient_id=session.patient_id,
        request=payload,
    )


@router.get(
    "/athena/history",
    response_model=AthenaChatHistoryResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve sanitized dialogue history for Athena curator",
    description=(
        "Returns the chronological conversation history of the patient's active cycle. "
        "Strictly contains user and curator dialogue turns, with zero leakage of hidden "
        "clinical reports, internal prompts, or medical grading."
    ),
)
def athena_chat_history_endpoint(
    session: DeviceSession = Depends(require_device_session),
    _scope: DeviceSession = Depends(require_scopes(SCOPE_CHAT_READ)),
    db: Session = Depends(get_db),
) -> AthenaChatHistoryResponse:
    service = AthenaChatService(db=db)
    return service.get_sanitized_history(patient_id=session.patient_id)
