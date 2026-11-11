import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional, Type, TypeVar
import httpx
from pydantic import BaseModel, ValidationError
from app.config import settings
from app.services.audit_trail_service import (
    AuditContext,
    AuditRecord,
    AuditTrailService,
    get_audit_trail_service,
    get_current_audit_context,
)

logger = logging.getLogger("aegis.gemini_gateway")

T = TypeVar("T", bound=BaseModel)


class GeminiModelRole(str, Enum):
    """Standard AI model roles in Project Aegis cascade architecture."""
    FLASH_LITE = "flash_lite"
    FLASH = "flash"
    PRO = "pro"
    VISION = "vision"


class GeminiGatewayError(Exception):
    """Base exception for all Gemini Gateway errors."""


class GeminiConfigurationError(GeminiGatewayError):
    """Raised when server configuration (such as GEMINI_API_KEY) is missing or invalid."""


class GeminiAuthOrBillingError(GeminiGatewayError):
    """
    Raised on authentication (401/403), invalid API key, or billing/payment errors (402 / billing disabled).
    Strictly non-retryable.
    """


class GeminiValidationError(GeminiGatewayError):
    """
    Raised on invalid request payload (400/404/422) or invalid JSON / Pydantic schema in response.
    Strictly non-retryable.
    """


class GeminiTransientError(GeminiGatewayError):
    """
    Raised when transient errors (429 rate limit, 503 overload, network timeout)
    persist after exhausting all configured retries.
    """


# Markers in error payloads that indicate permanent auth or billing/payment issues even on 429/403
_NON_RETRYABLE_AUTH_BILLING_MARKERS = (
    "billing_disabled",
    "billing account",
    "payment_required",
    "api_key_invalid",
    "invalid api key",
    "permission_denied",
    "unauthenticated",
    "account_disabled",
)


class GeminiGateway:
    """
    Server-side gateway to Google Gemini REST API.
    Isolates API credentials on the server, enforces timeouts, performs bounded retries
    exclusively on transient 429/503/timeout failures, and validates structured JSON responses
    without mutating medical text.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        flash_lite_model: Optional[str] = None,
        flash_model: Optional[str] = None,
        pro_model: Optional[str] = None,
        vision_model: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        max_retries: Optional[int] = None,
        initial_backoff_seconds: Optional[float] = None,
        base_url: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
        audit_service: Optional[AuditTrailService] = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else settings.gemini_api_key
        self.flash_lite_model = flash_lite_model or settings.gemini_flash_lite_model
        self.flash_model = flash_model or settings.gemini_flash_model
        self.pro_model = pro_model or settings.gemini_pro_model
        self.vision_model = vision_model or getattr(settings, "gemini_vision_model", "gemini-3.8-flash")
        self.timeout_seconds = (
            timeout_seconds if timeout_seconds is not None else settings.gemini_timeout_seconds
        )
        self.max_retries = (
            max_retries if max_retries is not None else settings.gemini_max_retries
        )
        self.initial_backoff_seconds = (
            initial_backoff_seconds
            if initial_backoff_seconds is not None
            else settings.gemini_initial_backoff_seconds
        )
        self.base_url = (base_url or settings.gemini_base_url).rstrip("/")
        self._transport = transport
        self._sleep_func = sleep_func

        if audit_service is not None:
            self.audit_service = audit_service
        elif settings.enable_clinical_audit:
            self.audit_service = get_audit_trail_service()
        else:
            self.audit_service = None
        self.last_call_id: Optional[str] = None
        self.usage_records: List[Dict[str, Any]] = []

        if self.max_retries < 1:
            raise GeminiConfigurationError("max_retries must be at least 1.")
        if self.timeout_seconds <= 0:
            raise GeminiConfigurationError("timeout_seconds must be positive.")

    def is_audit_enabled(self) -> bool:
        """Checks if clinical audit logging is active."""
        return self.audit_service is not None and getattr(self.audit_service, "enabled", True)

    def _record_audit_event(
        self,
        call_id: str,
        ctx: Optional[AuditContext],
        model: GeminiModelRole | str,
        system_instruction: Optional[str],
        prompt: Optional[str],
        raw_response_text: str,
        cleaned_json_text: Optional[str],
        parsed_json: Optional[Dict[str, Any]],
        status: str,
        schema_name: Optional[str],
        validation_error: Optional[str],
        latency_ms: float,
        attempt: int = 1,
        total_attempts: int = 1,
        diagnostic_log: Optional[str] = None,
    ) -> None:
        """Internal helper to dispatch audit record creation to AuditTrailService."""
        if not self.is_audit_enabled() or not self.audit_service:
            return

        if ctx:
            ctx.last_call_id = call_id
            if hasattr(ctx, "call_ids") and ctx.call_ids is not None:
                ctx.call_ids.append(call_id)
            if self.audit_service:
                ctx.audit_service = self.audit_service

        patient_id = (ctx.patient_id if (ctx and ctx.patient_id) else "unassigned_patient")
        cycle_id = (ctx.cycle_id if (ctx and ctx.cycle_id) else "unassigned_cycle")
        sprint_id = ctx.sprint_id if ctx else None
        stage = ctx.stage if ctx else "general"
        input_summary = ctx.input_context_summary if ctx else None
        report_id = ctx.report_id if ctx else None

        role_str = model.value if isinstance(model, GeminiModelRole) else str(model)
        resolved_name = self.resolve_model_name(model)

        record = AuditRecord(
            call_id=call_id,
            patient_id=patient_id,
            cycle_id=cycle_id,
            sprint_id=sprint_id,
            stage=stage,
            model_role=role_str,
            model_name=resolved_name,
            system_instruction=system_instruction,
            prompt=prompt,
            input_context_summary=input_summary,
            raw_response_text=raw_response_text,
            cleaned_json_text=cleaned_json_text,
            parsed_json=parsed_json,
            status=status,
            schema_name=schema_name,
            validation_error=validation_error,
            latency_ms=round(latency_ms, 2),
            created_at_utc=datetime.now(timezone.utc).isoformat(),
            report_id=report_id,
            privacy_level=None,
            attempt_number=attempt,
            total_attempts=total_attempts,
            error_diagnostic_log=diagnostic_log,
        )
        self.audit_service.record_call(record)

    def resolve_model_name(self, model_or_role: GeminiModelRole | str) -> str:
        """Resolves a GeminiModelRole or explicit model identifier into a target model string."""
        if isinstance(model_or_role, GeminiModelRole):
            role = model_or_role
        else:
            cleaned = model_or_role.strip()
            if not cleaned:
                raise GeminiValidationError("Model name cannot be empty.")
            try:
                role = GeminiModelRole(cleaned)
            except ValueError:
                return cleaned

        if role == GeminiModelRole.FLASH_LITE:
            return self.flash_lite_model
        if role == GeminiModelRole.FLASH:
            return self.flash_model
        if role == GeminiModelRole.PRO:
            return self.pro_model
        if role == GeminiModelRole.VISION:
            return self.vision_model
        raise GeminiValidationError(f"Unsupported model role: {model_or_role}")

    def _require_api_key(self) -> str:
        key = (self._api_key or "").strip()
        if not key or key == "YOUR_API_KEY_HERE":
            raise GeminiConfigurationError(
                "GEMINI_API_KEY is not configured on the server."
            )
        return key

    @staticmethod
    def _strip_outer_markdown_fence(raw_text: str) -> str:
        """
        Removes only outer ```json ... ``` or ``` ... ``` markdown code fences if present.
        Never performs regex replacements inside the JSON payload or medical strings.
        """
        stripped = raw_text.strip()
        if not stripped.startswith("```"):
            return stripped

        lines = stripped.splitlines()
        if len(lines) >= 2 and lines[0].strip().startswith("```") and lines[-1].strip() == "```":
            first_Fence = lines[0].strip()
            if first_Fence in ("```", "```json", "```JSON"):
                return "\n".join(lines[1:-1]).strip()
        return stripped

    @classmethod
    def parse_json_object(cls, raw_text: str) -> Dict[str, Any]:
        """
        Parses a JSON object from model text output using standard json.loads.
        Strictly forbids aggressive regex replacements on medical content.
        """
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise GeminiValidationError("Model returned an empty text response.")

        cleaned = cls._strip_outer_markdown_fence(raw_text)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise GeminiValidationError(
                f"Model output is not valid JSON (line {e.lineno}, col {e.colno}): {e.msg}"
            ) from e

        if not isinstance(parsed, dict):
            raise GeminiValidationError(
                f"Expected top-level JSON object (dict), got {type(parsed).__name__}."
            )
        return parsed

    @staticmethod
    def _classify_http_error(status_code: int, response_text: str) -> Exception:
        """
        Classifies an HTTP error status and body into retryable vs non-retryable exceptions.
        Does not include sensitive patient data in exception messages.
        """
        lowered = (response_text or "").lower()

        # Check explicit auth / billing / payment markers first
        if status_code in (401, 402, 403):
            return GeminiAuthOrBillingError(
                f"Gemini API authentication or billing error (HTTP {status_code})."
            )

        if any(marker in lowered for marker in _NON_RETRYABLE_AUTH_BILLING_MARKERS):
            return GeminiAuthOrBillingError(
                f"Gemini API rejected request due to auth or billing status (HTTP {status_code})."
            )

        # Validation / client request errors (non-retryable)
        if status_code in (400, 404, 422):
            return GeminiValidationError(
                f"Gemini API request validation error (HTTP {status_code})."
            )

        # Transient rate limit or server unavailability (retryable)
        if status_code in (429, 502, 503, 504):
            return GeminiTransientError(
                f"Gemini API transient unavailability or rate limit (HTTP {status_code})."
            )

        # Other 4xx errors are non-retryable client errors
        if 400 <= status_code < 500:
            return GeminiValidationError(
                f"Gemini API client error (HTTP {status_code})."
            )

        # 500+ server errors
        return GeminiTransientError(
            f"Gemini API server error (HTTP {status_code})."
        )

    @staticmethod
    def _extract_text_from_response_payload(payload: Any) -> str:
        """Extracts generated text from standard Gemini generateContent JSON response."""
        if not isinstance(payload, dict):
            raise GeminiValidationError("Gemini API response payload is not a JSON object.")

        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise GeminiValidationError("Gemini API response contains no candidates.")

        first_candidate = candidates[0]
        if not isinstance(first_candidate, dict):
            raise GeminiValidationError("Invalid candidate structure in Gemini API response.")

        content = first_candidate.get("content")
        if not isinstance(content, dict):
            raise GeminiValidationError("Missing content in Gemini API candidate.")

        parts = content.get("parts")
        if not isinstance(parts, list) or not parts:
            raise GeminiValidationError("Missing parts in Gemini API candidate content.")

        text_chunks: list[str] = []
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text_chunks.append(part["text"])

        combined = "".join(text_chunks).strip()
        if not combined:
            raise GeminiValidationError("Gemini API returned empty text in candidate parts.")
        return combined

    async def generate_raw_text(
        self,
        prompt: Optional[str] = None,
        model: GeminiModelRole | str = GeminiModelRole.FLASH,
        system_instruction: Optional[str] = None,
        response_mime_type: str = "application/json",
        max_output_tokens: int = 16384,
        inline_images: Optional[List[Dict[str, str]]] = None,
        contents: Optional[List[Dict[str, Any]]] = None,
        audit_context: Optional[AuditContext] = None,
        call_id: Optional[str] = None,
        _skip_audit_record: bool = False,
    ) -> str:
        """
        Executes a call to Gemini generateContent endpoint with bounded retries on transient 429/503/timeout.
        Supports multimodal requests with inline base64 images and multi-turn contents sequences.
        Never retries auth, billing, or validation errors.
        """
        api_key = self._require_api_key()

        if contents is not None:
            if not isinstance(contents, list) or not contents:
                raise GeminiValidationError("contents must be a non-empty list of turn objects.")
            request_contents = contents
        else:
            if not isinstance(prompt, str) or not prompt.strip():
                raise GeminiValidationError("Prompt must be a non-empty string when contents is not provided.")

            parts: List[Dict[str, Any]] = []
            if inline_images:
                for img in inline_images:
                    parts.append({
                        "inlineData": {
                            "mimeType": img["mime_type"],
                            "data": img["data_base64"],
                        }
                    })
            parts.append({"text": prompt})
            request_contents = [
                {
                    "role": "user",
                    "parts": parts,
                }
            ]

        resolved_model = self.resolve_model_name(model)
        url = f"{self.base_url}/models/{resolved_model}:generateContent"

        request_body: Dict[str, Any] = {
            "contents": request_contents,
            "generationConfig": {
                "responseMimeType": response_mime_type,
                "maxOutputTokens": max_output_tokens,
            },
        }
        if system_instruction and system_instruction.strip():
            request_body["systemInstruction"] = {
                "parts": [{"text": system_instruction.strip()}]
            }

        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        }

        if call_id is None:
            call_id = f"call_{uuid.uuid4().hex}"
        self.last_call_id = call_id
        start_time = time.perf_counter()

        backoff = self.initial_backoff_seconds
        last_transient_error: Optional[Exception] = None

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds),
            transport=self._transport,
        ) as client:
            for attempt in range(1, self.max_retries + 1):
                try:
                    response = await client.post(url, json=request_body, headers=headers)
                except (httpx.TimeoutException, httpx.NetworkError) as net_err:
                    last_transient_error = GeminiTransientError(
                        f"Network or timeout error communicating with Gemini API ({type(net_err).__name__})."
                    )
                    if attempt < self.max_retries:
                        logger.warning(
                            f"Transient network error on model '{resolved_model}' "
                            f"(attempt {attempt}/{self.max_retries}). Retrying..."
                        )
                        if backoff > 0:
                            await self._sleep_func(backoff)
                            backoff *= 2
                        continue

                    if not _skip_audit_record and self.is_audit_enabled():
                        latency_ms = (time.perf_counter() - start_time) * 1000.0
                        ctx = audit_context or get_current_audit_context()
                        err_status = "TIMEOUT" if isinstance(net_err, httpx.TimeoutException) else "TRANSPORT_ERROR"
                        self._record_audit_event(
                            call_id=call_id,
                            ctx=ctx,
                            model=model,
                            system_instruction=system_instruction,
                            prompt=prompt or str(contents),
                            raw_response_text="",
                            cleaned_json_text=None,
                            parsed_json=None,
                            status=err_status,
                            schema_name=None,
                            validation_error=str(last_transient_error),
                            latency_ms=latency_ms,
                            attempt=attempt,
                            total_attempts=self.max_retries,
                            diagnostic_log=f"Exhausted retries: {last_transient_error}",
                        )
                    raise last_transient_error from net_err

                if response.status_code != 200:
                    classified_err = self._classify_http_error(
                        response.status_code,
                        response.text,
                    )
                    if isinstance(classified_err, GeminiTransientError):
                        last_transient_error = classified_err
                        if attempt < self.max_retries:
                            logger.warning(
                                f"Transient HTTP {response.status_code} on model '{resolved_model}' "
                                f"(attempt {attempt}/{self.max_retries}). Retrying..."
                            )
                            if backoff > 0:
                                await self._sleep_func(backoff)
                                backoff *= 2
                            continue
                    # Non-retryable error OR retries exhausted
                    if not _skip_audit_record and self.is_audit_enabled():
                        latency_ms = (time.perf_counter() - start_time) * 1000.0
                        ctx = audit_context or get_current_audit_context()
                        self._record_audit_event(
                            call_id=call_id,
                            ctx=ctx,
                            model=model,
                            system_instruction=system_instruction,
                            prompt=prompt or str(contents),
                            raw_response_text=response.text,
                            cleaned_json_text=None,
                            parsed_json=None,
                            status="HTTP_ERROR",
                            schema_name=None,
                            validation_error=str(classified_err),
                            latency_ms=latency_ms,
                            attempt=attempt,
                            total_attempts=self.max_retries,
                            diagnostic_log=f"HTTP {response.status_code}: {classified_err}",
                        )
                    raise classified_err

                try:
                    payload = response.json()
                except ValueError as json_err:
                    if not _skip_audit_record and self.is_audit_enabled():
                        latency_ms = (time.perf_counter() - start_time) * 1000.0
                        ctx = audit_context or get_current_audit_context()
                        self._record_audit_event(
                            call_id=call_id,
                            ctx=ctx,
                            model=model,
                            system_instruction=system_instruction,
                            prompt=prompt or str(contents),
                            raw_response_text=response.text,
                            cleaned_json_text=None,
                            parsed_json=None,
                            status="JSON_DECODE_ERROR",
                            schema_name=None,
                            validation_error="Gemini API HTTP 200 response was not valid JSON.",
                            latency_ms=latency_ms,
                            attempt=attempt,
                            total_attempts=self.max_retries,
                        )
                    raise GeminiValidationError(
                        "Gemini API HTTP 200 response was not valid JSON."
                    ) from json_err

                raw_text = self._extract_text_from_response_payload(payload)
                latency_ms = (time.perf_counter() - start_time) * 1000.0
                ctx = audit_context or get_current_audit_context()

                usage_metadata = payload.get("usageMetadata") if isinstance(payload, dict) else None
                candidates = payload.get("candidates") if isinstance(payload, dict) else None
                finish_reason = candidates[0].get("finishReason") if (candidates and isinstance(candidates, list) and isinstance(candidates[0], dict)) else None

                diagnostic_info = None
                if usage_metadata:
                    p_tok = usage_metadata.get("promptTokenCount", 0)
                    c_tok = usage_metadata.get("candidatesTokenCount", 0)
                    tot_tok = usage_metadata.get("totalTokenCount", 0)
                    cached_tok = usage_metadata.get("cachedContentTokenCount", 0)
                    diagnostic_info = f"[USAGE] prompt={p_tok}, candidates={c_tok}, total={tot_tok}, cached={cached_tok}, finish_reason={finish_reason}"
                    self._last_diagnostic_info = diagnostic_info
                    if not hasattr(self, "usage_records") or self.usage_records is None:
                        self.usage_records = []
                    rec = {
                        "call_id": call_id,
                        "model_role": model.value if isinstance(model, GeminiModelRole) else str(model),
                        "model_name": resolved_model,
                        "prompt_tokens": p_tok,
                        "candidates_tokens": c_tok,
                        "cached_tokens": cached_tok,
                        "total_tokens": tot_tok,
                        "finish_reason": finish_reason,
                        "latency_ms": round(latency_ms, 2),
                        "stage": ctx.stage if ctx else "unknown",
                    }
                    self.usage_records.append(rec)
                    try:
                        from pathlib import Path
                        usage_log_file = Path("backend/data/synthetic_patient_001_gemini_usage.jsonl")
                        usage_log_file.parent.mkdir(parents=True, exist_ok=True)
                        with open(usage_log_file, "a", encoding="utf-8") as uf:
                            uf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    except Exception:
                        pass

                if not _skip_audit_record and self.is_audit_enabled():
                    self._record_audit_event(
                        call_id=call_id,
                        ctx=ctx,
                        model=model,
                        system_instruction=system_instruction,
                        prompt=prompt or str(contents),
                        raw_response_text=raw_text,
                        cleaned_json_text=None,
                        parsed_json=None,
                        status="SUCCESS",
                        schema_name=None,
                        validation_error=None,
                        latency_ms=latency_ms,
                        attempt=attempt,
                        total_attempts=self.max_retries,
                        diagnostic_log=diagnostic_info,
                    )
                return raw_text

        final_err = last_transient_error or GeminiTransientError(
            f"Failed to receive response from Gemini after {self.max_retries} attempts."
        )
        if not _skip_audit_record and self.is_audit_enabled():
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            ctx = audit_context or get_current_audit_context()
            self._record_audit_event(
                call_id=call_id,
                ctx=ctx,
                model=model,
                system_instruction=system_instruction,
                prompt=prompt or str(contents),
                raw_response_text="",
                cleaned_json_text=None,
                parsed_json=None,
                status="TRANSIENT_EXHAUSTED",
                schema_name=None,
                validation_error=str(final_err),
                latency_ms=latency_ms,
                attempt=self.max_retries,
                total_attempts=self.max_retries,
            )
        raise final_err

    async def generate_json(
        self,
        prompt: str,
        model: GeminiModelRole | str = GeminiModelRole.FLASH,
        system_instruction: Optional[str] = None,
        max_output_tokens: int = 16384,
        inline_images: Optional[List[Dict[str, str]]] = None,
        audit_context: Optional[AuditContext] = None,
        _skip_audit_record: bool = False,
    ) -> Dict[str, Any]:
        """Generates content and parses the response into a validated Python dictionary."""
        if not self.is_audit_enabled() or _skip_audit_record:
            raw_text = await self.generate_raw_text(
                prompt=prompt,
                model=model,
                system_instruction=system_instruction,
                response_mime_type="application/json",
                max_output_tokens=max_output_tokens,
                inline_images=inline_images,
                audit_context=audit_context,
                _skip_audit_record=True,
            )
            return self.parse_json_object(raw_text)

        call_id = f"call_{uuid.uuid4().hex}"
        self.last_call_id = call_id
        start_time = time.perf_counter()
        ctx = audit_context or get_current_audit_context()

        try:
            raw_text = await self.generate_raw_text(
                prompt=prompt,
                model=model,
                system_instruction=system_instruction,
                response_mime_type="application/json",
                max_output_tokens=max_output_tokens,
                inline_images=inline_images,
                audit_context=audit_context,
                call_id=call_id,
                _skip_audit_record=True,
            )
        except Exception as net_err:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            err_status = "TIMEOUT" if "timeout" in str(net_err).lower() else "TRANSPORT_ERROR"
            self._record_audit_event(
                call_id=call_id,
                ctx=ctx,
                model=model,
                system_instruction=system_instruction,
                prompt=prompt,
                raw_response_text="",
                cleaned_json_text=None,
                parsed_json=None,
                status=err_status,
                schema_name=None,
                validation_error=str(net_err),
                latency_ms=latency_ms,
                total_attempts=self.max_retries,
            )
            raise

        cleaned = self._strip_outer_markdown_fence(raw_text)
        try:
            parsed = self.parse_json_object(raw_text)
        except GeminiValidationError as json_err:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            self._record_audit_event(
                call_id=call_id,
                ctx=ctx,
                model=model,
                system_instruction=system_instruction,
                prompt=prompt,
                raw_response_text=raw_text,
                cleaned_json_text=cleaned,
                parsed_json=None,
                status="JSON_DECODE_ERROR",
                schema_name=None,
                validation_error=str(json_err),
                latency_ms=latency_ms,
            )
            raise json_err

        latency_ms = (time.perf_counter() - start_time) * 1000.0
        self._record_audit_event(
            call_id=call_id,
            ctx=ctx,
            model=model,
            system_instruction=system_instruction,
            prompt=prompt,
            raw_response_text=raw_text,
            cleaned_json_text=cleaned,
            parsed_json=parsed,
            status="SUCCESS",
            schema_name=None,
            validation_error=None,
            latency_ms=latency_ms,
            diagnostic_log=getattr(self, "_last_diagnostic_info", None),
        )
        return parsed

    async def generate_structured(
        self,
        prompt: str,
        response_schema: Type[T],
        model: GeminiModelRole | str = GeminiModelRole.FLASH,
        system_instruction: Optional[str] = None,
        max_output_tokens: int = 16384,
        inline_images: Optional[List[Dict[str, str]]] = None,
        audit_context: Optional[AuditContext] = None,
    ) -> T:
        """
        Generates content, parses JSON strictly, and validates against a Pydantic BaseModel schema.
        Raises GeminiValidationError if the response does not conform to response_schema.
        Preserves raw text and schema validation errors for clinical auditability.
        """
        if not self.is_audit_enabled():
            parsed_dict = await self.generate_json(
                prompt=prompt,
                model=model,
                system_instruction=system_instruction,
                max_output_tokens=max_output_tokens,
                inline_images=inline_images,
                _skip_audit_record=True,
            )
            try:
                return response_schema.model_validate(parsed_dict)
            except ValidationError as val_err:
                raise GeminiValidationError(
                    f"Model response failed structured schema validation for {response_schema.__name__}: {val_err}"
                ) from val_err

        call_id = f"call_{uuid.uuid4().hex}"
        self.last_call_id = call_id
        start_time = time.perf_counter()
        ctx = audit_context or get_current_audit_context()

        try:
            raw_text = await self.generate_raw_text(
                prompt=prompt,
                model=model,
                system_instruction=system_instruction,
                response_mime_type="application/json",
                max_output_tokens=max_output_tokens,
                inline_images=inline_images,
                audit_context=audit_context,
                call_id=call_id,
                _skip_audit_record=True,
            )
        except Exception as net_err:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            err_status = "TIMEOUT" if "timeout" in str(net_err).lower() else "TRANSPORT_ERROR"
            self._record_audit_event(
                call_id=call_id,
                ctx=ctx,
                model=model,
                system_instruction=system_instruction,
                prompt=prompt,
                raw_response_text="",
                cleaned_json_text=None,
                parsed_json=None,
                status=err_status,
                schema_name=response_schema.__name__,
                validation_error=str(net_err),
                latency_ms=latency_ms,
                total_attempts=self.max_retries,
            )
            raise

        cleaned = self._strip_outer_markdown_fence(raw_text)
        try:
            parsed_dict = self.parse_json_object(raw_text)
        except GeminiValidationError as json_err:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            self._record_audit_event(
                call_id=call_id,
                ctx=ctx,
                model=model,
                system_instruction=system_instruction,
                prompt=prompt,
                raw_response_text=raw_text,
                cleaned_json_text=cleaned,
                parsed_json=None,
                status="JSON_DECODE_ERROR",
                schema_name=response_schema.__name__,
                validation_error=str(json_err),
                latency_ms=latency_ms,
            )
            raise json_err

        try:
            validated = response_schema.model_validate(parsed_dict)
        except ValidationError as val_err:
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            val_msg = (
                f"Model response failed structured schema validation for {response_schema.__name__}: {val_err}"
            )
            self._record_audit_event(
                call_id=call_id,
                ctx=ctx,
                model=model,
                system_instruction=system_instruction,
                prompt=prompt,
                raw_response_text=raw_text,
                cleaned_json_text=cleaned,
                parsed_json=parsed_dict,
                status="SCHEMA_ERROR",
                schema_name=response_schema.__name__,
                validation_error=val_msg,
                latency_ms=latency_ms,
            )
            raise GeminiValidationError(val_msg) from val_err

        latency_ms = (time.perf_counter() - start_time) * 1000.0
        self._record_audit_event(
            call_id=call_id,
            ctx=ctx,
            model=model,
            system_instruction=system_instruction,
            prompt=prompt,
            raw_response_text=raw_text,
            cleaned_json_text=cleaned,
            parsed_json=parsed_dict,
            status="SUCCESS",
            schema_name=response_schema.__name__,
            validation_error=None,
            latency_ms=latency_ms,
            diagnostic_log=getattr(self, "_last_diagnostic_info", None),
        )
        return validated

    async def run_neo_system_specialist(
        self,
        prompt: str,
    ) -> Any:
        """Executes Gemini Flash-Lite for an organ specialist in Neo mode."""
        from app.schemas.consilium import NeoSystemAgentOutput

        return await self.generate_structured(
            prompt=prompt,
            response_schema=NeoSystemAgentOutput,
            model=GeminiModelRole.FLASH_LITE,
        )

    async def run_neo_flash_master(
        self,
        prompt: str,
    ) -> Any:
        """Executes Gemini Flash in Neo mode for Deputy Chief Physician intersystem synthesis & longitudinal audit."""
        from app.schemas.consilium import NeoFlashMasterOutput

        return await self.generate_structured(
            prompt=prompt,
            response_schema=NeoFlashMasterOutput,
            model=GeminiModelRole.FLASH,
        )

    async def run_neo_pro_master(
        self,
        prompt: str,
    ) -> Any:
        """Executes Gemini Pro in Neo mode for Chief Physician clinical verdict & quality gate."""
        from app.schemas.consilium import NeoProMasterOutput

        return await self.generate_structured(
            prompt=prompt,
            response_schema=NeoProMasterOutput,
            model=GeminiModelRole.PRO,
        )
