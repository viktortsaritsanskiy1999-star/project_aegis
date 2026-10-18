import asyncio
import json
import logging
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, Optional, Type, TypeVar
import httpx
from pydantic import BaseModel, ValidationError
from app.config import settings

logger = logging.getLogger("aegis.gemini_gateway")

T = TypeVar("T", bound=BaseModel)


class GeminiModelRole(str, Enum):
    """Standard AI model roles in Project Aegis cascade architecture."""
    FLASH_LITE = "flash_lite"
    FLASH = "flash"
    PRO = "pro"


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
        timeout_seconds: Optional[float] = None,
        max_retries: Optional[int] = None,
        initial_backoff_seconds: Optional[float] = None,
        base_url: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._api_key = api_key if api_key is not None else settings.gemini_api_key
        self.flash_lite_model = flash_lite_model or settings.gemini_flash_lite_model
        self.flash_model = flash_model or settings.gemini_flash_model
        self.pro_model = pro_model or settings.gemini_pro_model
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

        if self.max_retries < 1:
            raise GeminiConfigurationError("max_retries must be at least 1.")
        if self.timeout_seconds <= 0:
            raise GeminiConfigurationError("timeout_seconds must be positive.")

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
        prompt: str,
        model: GeminiModelRole | str = GeminiModelRole.FLASH,
        system_instruction: Optional[str] = None,
        response_mime_type: str = "application/json",
        max_output_tokens: int = 8192,
    ) -> str:
        """
        Executes a call to Gemini generateContent endpoint with bounded retries on transient 429/503/timeout.
        Never retries auth, billing, or validation errors.
        """
        api_key = self._require_api_key()
        if not isinstance(prompt, str) or not prompt.strip():
            raise GeminiValidationError("Prompt must be a non-empty string.")

        resolved_model = self.resolve_model_name(model)
        url = f"{self.base_url}/models/{resolved_model}:generateContent"

        request_body: Dict[str, Any] = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ],
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
                    raise classified_err

                try:
                    payload = response.json()
                except ValueError as json_err:
                    raise GeminiValidationError(
                        "Gemini API HTTP 200 response was not valid JSON."
                    ) from json_err

                return self._extract_text_from_response_payload(payload)

        raise last_transient_error or GeminiTransientError(
            f"Failed to receive response from Gemini after {self.max_retries} attempts."
        )

    async def generate_json(
        self,
        prompt: str,
        model: GeminiModelRole | str = GeminiModelRole.FLASH,
        system_instruction: Optional[str] = None,
        max_output_tokens: int = 8192,
    ) -> Dict[str, Any]:
        """Generates content and parses the response into a validated Python dictionary."""
        raw_text = await self.generate_raw_text(
            prompt=prompt,
            model=model,
            system_instruction=system_instruction,
            response_mime_type="application/json",
            max_output_tokens=max_output_tokens,
        )
        return self.parse_json_object(raw_text)

    async def generate_structured(
        self,
        prompt: str,
        response_schema: Type[T],
        model: GeminiModelRole | str = GeminiModelRole.FLASH,
        system_instruction: Optional[str] = None,
        max_output_tokens: int = 8192,
    ) -> T:
        """
        Generates content, parses JSON strictly, and validates against a Pydantic BaseModel schema.
        Raises GeminiValidationError if the response does not conform to response_schema.
        """
        parsed_dict = await self.generate_json(
            prompt=prompt,
            model=model,
            system_instruction=system_instruction,
            max_output_tokens=max_output_tokens,
        )
        try:
            return response_schema.model_validate(parsed_dict)
        except ValidationError as val_err:
            raise GeminiValidationError(
                f"Model response failed structured schema validation for {response_schema.__name__}."
            ) from val_err
