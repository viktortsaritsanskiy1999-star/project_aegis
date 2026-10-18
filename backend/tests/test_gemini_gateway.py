import json
import pytest
import httpx
from pydantic import BaseModel, ConfigDict
from app.services.gemini_gateway import (
    GeminiGateway,
    GeminiModelRole,
    GeminiConfigurationError,
    GeminiAuthOrBillingError,
    GeminiValidationError,
    GeminiTransientError,
)


class SampleClinicalOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system_key: str
    summary: str
    confidence_pct: int


def _gemini_success_payload(text_content: str) -> dict:
    return {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"text": text_content}],
                }
            }
        ]
    }


def test_resolve_model_roles_and_explicit_names():
    gateway = GeminiGateway(
        api_key="test_key_abc",
        flash_lite_model="gemini-3.1-flash-lite",
        flash_model="gemini-3.6-flash",
        pro_model="gemini-3.1-pro-preview",
    )
    assert gateway.resolve_model_name(GeminiModelRole.FLASH_LITE) == "gemini-3.1-flash-lite"
    assert gateway.resolve_model_name(GeminiModelRole.FLASH) == "gemini-3.6-flash"
    assert gateway.resolve_model_name(GeminiModelRole.PRO) == "gemini-3.1-pro-preview"
    assert gateway.resolve_model_name("flash_lite") == "gemini-3.1-flash-lite"
    assert gateway.resolve_model_name("gemini-custom-exp") == "gemini-custom-exp"

    with pytest.raises(GeminiValidationError):
        gateway.resolve_model_name("   ")


@pytest.mark.anyio
async def test_missing_or_placeholder_api_key_raises_configuration_error():
    called = False

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=_gemini_success_payload("{}"))

    transport = httpx.MockTransport(handler)

    gateway_empty = GeminiGateway(api_key="", transport=transport)
    with pytest.raises(GeminiConfigurationError):
        await gateway_empty.generate_json("Test prompt")

    gateway_placeholder = GeminiGateway(api_key="YOUR_API_KEY_HERE", transport=transport)
    with pytest.raises(GeminiConfigurationError):
        await gateway_placeholder.generate_json("Test prompt")

    # Ensure zero network requests were made when key is missing
    assert called is False


@pytest.mark.anyio
async def test_gateway_structured_validation_and_exact_medical_text_preservation():
    """
    Verifies that outer ```json fences are stripped cleanly while medical symbols,
    quotes, ratios, and units inside JSON strings are preserved 100% intact.
    """
    medical_text = (
        "BP 135/85 mmHg; troponin I < 0.01 ng/mL ('normal' range: 0.00-0.04). "
        "No ST-elevation in leads V1-V6."
    )
    fenced_json = (
        "```json\n"
        + json.dumps(
            {
                "system_key": "cvs",
                "summary": medical_text,
                "confidence_pct": 95,
            }
        )
        + "\n```"
    )

    captured_headers: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(200, json=_gemini_success_payload(fenced_json))

    gateway = GeminiGateway(
        api_key="server_isolated_key_999",
        transport=httpx.MockTransport(handler),
    )

    result = await gateway.generate_structured(
        prompt="Analyze CVS markers",
        response_schema=SampleClinicalOutput,
        model=GeminiModelRole.FLASH_LITE,
    )

    assert result.system_key == "cvs"
    assert result.summary == medical_text
    assert result.confidence_pct == 95
    assert captured_headers.get("x-goog-api-key") == "server_isolated_key_999"


@pytest.mark.anyio
async def test_gateway_rejects_malformed_json_and_schema_mismatch_without_retry():
    call_count = 0

    async def bad_json_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json=_gemini_success_payload('{"system_key": "cvs", "summary": "unclosed string}'),
        )

    gateway = GeminiGateway(
        api_key="valid_key",
        max_retries=3,
        transport=httpx.MockTransport(bad_json_handler),
    )

    with pytest.raises(GeminiValidationError):
        await gateway.generate_json("Analyze")

    # Malformed JSON must NOT trigger retries
    assert call_count == 1

    # Now test schema validation mismatch
    schema_calls = 0

    async def bad_schema_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal schema_calls
        schema_calls += 1
        return httpx.Response(
            200,
            json=_gemini_success_payload('{"system_key": "cvs", "unexpected_field": 123}'),
        )

    gateway_schema = GeminiGateway(
        api_key="valid_key",
        max_retries=3,
        transport=httpx.MockTransport(bad_schema_handler),
    )

    with pytest.raises(GeminiValidationError):
        await gateway_schema.generate_structured("Analyze", SampleClinicalOutput)

    assert schema_calls == 1


@pytest.mark.anyio
async def test_gateway_retries_transient_429_and_503_then_succeeds():
    attempts = 0
    Recorded_sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        Recorded_sleeps.append(seconds)

    async def transient_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                503,
                json={"error": {"code": 503, "message": "Service temporarily overloaded", "status": "UNAVAILABLE"}},
            )
        if attempts == 2:
            return httpx.Response(
                429,
                json={"error": {"code": 429, "message": "Rate limit exceeded per minute", "status": "RESOURCE_EXHAUSTED"}},
            )
        return httpx.Response(
            200,
            json=_gemini_success_payload('{"status": "ok", "attempt": 3}'),
        )

    gateway = GeminiGateway(
        api_key="valid_key",
        max_retries=3,
        initial_backoff_seconds=0.5,
        transport=httpx.MockTransport(transient_handler),
        sleep_func=fake_sleep,
    )

    data = await gateway.generate_json("Check status")
    assert data == {"status": "ok", "attempt": 3}
    assert attempts == 3
    assert Recorded_sleeps == [0.5, 1.0]


@pytest.mark.anyio
async def test_gateway_raises_transient_error_when_retries_exhausted():
    attempts = 0

    async def always_503_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, text="Service Unavailable")

    async def noop_sleep(_s: float) -> None:
        pass

    gateway = GeminiGateway(
        api_key="valid_key",
        max_retries=3,
        initial_backoff_seconds=0.1,
        transport=httpx.MockTransport(always_503_handler),
        sleep_func=noop_sleep,
    )

    with pytest.raises(GeminiTransientError):
        await gateway.generate_json("Prompt")

    assert attempts == 3


@pytest.mark.anyio
async def test_gateway_retries_on_network_timeout():
    attempts = 0

    async def timeout_then_ok_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("Read timed out", request=request)
        return httpx.Response(200, json=_gemini_success_payload('{"recovered": true}'))

    async def noop_sleep(_s: float) -> None:
        pass

    gateway = GeminiGateway(
        api_key="valid_key",
        max_retries=2,
        initial_backoff_seconds=0.1,
        transport=httpx.MockTransport(timeout_then_ok_handler),
        sleep_func=noop_sleep,
    )

    res = await gateway.generate_json("Prompt")
    assert res == {"recovered": True}
    assert attempts == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status_code,body,expected_exc",
    [
        (401, '{"error": {"status": "UNAUTHENTICATED", "message": "Invalid API key"}}', GeminiAuthOrBillingError),
        (402, '{"error": {"status": "PAYMENT_REQUIRED", "message": "Billing required"}}', GeminiAuthOrBillingError),
        (403, '{"error": {"status": "PERMISSION_DENIED", "message": "BILLING_DISABLED on project"}}', GeminiAuthOrBillingError),
        (429, '{"error": {"status": "RESOURCE_EXHAUSTED", "message": "Billing account disabled"}}', GeminiAuthOrBillingError),
        (400, '{"error": {"status": "INVALID_ARGUMENT", "message": "Invalid model parameter"}}', GeminiValidationError),
    ],
)
async def test_gateway_never_retries_auth_billing_or_validation_errors(
    status_code: int,
    body: str,
    expected_exc: type[Exception],
):
    attempts = 0

    async def non_retryable_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status_code, text=body)

    async def noop_sleep(_s: float) -> None:
        pass

    gateway = GeminiGateway(
        api_key="valid_key",
        max_retries=4,
        transport=httpx.MockTransport(non_retryable_handler),
        sleep_func=noop_sleep,
    )

    with pytest.raises(expected_exc):
        await gateway.generate_json("Prompt")

    # Must fail immediately on the first attempt without retrying
    assert attempts == 1
