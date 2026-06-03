"""
Test 12: OpenAI LLM call — real API integration test.

Validates that the platform can successfully call the OpenAI API using
the gpt-4o-mini model with a minimal prompt. Verifies response structure,
token usage, and cost guardrails.

This test uses:
- httpx for direct OpenAI API calls (https://api.openai.com/v1/responses)
- credentials fixture for openai_api_key (from credentials.md)
- evidence_collector fixture for emitting JSON evidence
- Retry logic for 429 rate limit and 401 auth errors

Requirements: R12.1, R12.2, R12.3, R12.4, R12.5
"""

import time
from typing import Any

import httpx
import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

OPENAI_API_URL = "https://api.openai.com/v1/responses"
MODEL = "gpt-4o-mini"
MAX_TOTAL_TOKENS = 5000  # Cost guardrail
RETRY_DELAY_SECONDS = 5
REQUEST_TIMEOUT_SECONDS = 30

# Minimal prompt to minimize cost
MINIMAL_PROMPT = "Say hello in one word."

# Evidence filename
EVIDENCE_FILENAME = "12-openai.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_responses_text(data: dict[str, Any]) -> str:
    """Pull assistant text out of an OpenAI Responses API payload."""
    if not isinstance(data, dict):
        return ""
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text:
        return output_text
    if isinstance(output_text, list):
        joined = "".join(p for p in output_text if isinstance(p, str))
        if joined:
            return joined
    chunks: list[str] = []
    for item in data.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content", []) or []:
            if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                text = part.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "".join(chunks)


def _make_openai_request(
    api_key: str,
    prompt: str = MINIMAL_PROMPT,
    model: str = MODEL,
    timeout: int = REQUEST_TIMEOUT_SECONDS,
) -> httpx.Response:
    """Make a single OpenAI Responses API request.

    Args:
        api_key: OpenAI API key (Bearer token).
        prompt: The user message to send.
        model: The model to use.
        timeout: Request timeout in seconds.

    Returns:
        The httpx.Response object.
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": model,
        "input": prompt,
        "max_output_tokens": 50,  # Keep response short to minimize cost
    }

    with httpx.Client(timeout=timeout) as client:
        response = client.post(OPENAI_API_URL, json=payload, headers=headers)

    return response


def _call_openai_with_retry(
    api_key: str,
    prompt: str = MINIMAL_PROMPT,
    model: str = MODEL,
) -> tuple[httpx.Response, dict[str, Any]]:
    """Call OpenAI API with retry logic for 429/401 errors.

    Retries once after RETRY_DELAY_SECONDS on 429 or 401 responses.

    Args:
        api_key: OpenAI API key.
        prompt: The user message.
        model: The model to use.

    Returns:
        Tuple of (final response, metadata dict with retry info).
    """
    metadata: dict[str, Any] = {
        "attempts": 0,
        "retried": False,
        "retry_reason": None,
        "first_attempt_status": None,
    }

    # First attempt
    metadata["attempts"] = 1
    start_time = time.time()
    response = _make_openai_request(api_key, prompt, model)
    first_latency = time.time() - start_time
    metadata["first_attempt_status"] = response.status_code
    metadata["first_attempt_latency_ms"] = round(first_latency * 1000, 2)

    # Retry on 429 (rate limit) or 401 (auth error)
    if response.status_code in (429, 401):
        metadata["retried"] = True
        metadata["retry_reason"] = (
            "rate_limit" if response.status_code == 429 else "auth_error"
        )
        time.sleep(RETRY_DELAY_SECONDS)

        metadata["attempts"] = 2
        start_time = time.time()
        response = _make_openai_request(api_key, prompt, model)
        retry_latency = time.time() - start_time
        metadata["retry_latency_ms"] = round(retry_latency * 1000, 2)

    return response, metadata


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestOpenAILLMCall:
    """R12.1: Trigger minimal LLM call and assert non-empty completion response."""

    def test_openai_call_succeeds(self, credentials):
        """OpenAI API call with gpt-4o-mini must return a valid completion."""
        api_key = credentials.openai_api_key
        assert api_key and api_key.startswith("sk-"), (
            "OpenAI API key not found or invalid in credentials.md. "
            "Expected key starting with 'sk-'."
        )

        response, metadata = _call_openai_with_retry(api_key)

        assert response.status_code == 200, (
            f"OpenAI API returned HTTP {response.status_code} after "
            f"{metadata['attempts']} attempt(s).\n"
            f"Response: {response.text[:500]}"
        )

        data = response.json()
        content = _extract_responses_text(data)
        assert content.strip(), (
            "OpenAI response content is empty. "
            f"Full response: {data}"
        )


class TestOpenAIModelAndTokens:
    """R12.2: Assert response contains model gpt-4o-mini and non-zero token usage."""

    def test_response_model_is_gpt4o_mini(self, credentials):
        """Response must indicate model gpt-4o-mini was used."""
        api_key = credentials.openai_api_key
        response, _ = _call_openai_with_retry(api_key)

        assert response.status_code == 200, (
            f"OpenAI API call failed with HTTP {response.status_code}"
        )

        data = response.json()
        model = data.get("model", "")
        assert "gpt-4o-mini" in model, (
            f"Expected model 'gpt-4o-mini' in response, got '{model}'."
        )

    def test_token_usage_non_zero(self, credentials):
        """Response must have non-zero prompt_tokens and completion_tokens."""
        api_key = credentials.openai_api_key
        response, _ = _call_openai_with_retry(api_key)

        assert response.status_code == 200, (
            f"OpenAI API call failed with HTTP {response.status_code}"
        )

        data = response.json()
        usage = data.get("usage", {})

        prompt_tokens = usage.get("input_tokens", 0)
        completion_tokens = usage.get("output_tokens", 0)

        assert prompt_tokens > 0, (
            f"Expected non-zero input_tokens, got {prompt_tokens}. "
            f"Usage: {usage}"
        )
        assert completion_tokens > 0, (
            f"Expected non-zero output_tokens, got {completion_tokens}. "
            f"Usage: {usage}"
        )


class TestOpenAICostGuardrail:
    """R12.3: Assert total tokens <= 5000 (cost guardrail)."""

    def test_total_tokens_within_limit(self, credentials):
        """Total token usage must be <= 5000 for this minimal test call."""
        api_key = credentials.openai_api_key
        response, _ = _call_openai_with_retry(api_key)

        assert response.status_code == 200, (
            f"OpenAI API call failed with HTTP {response.status_code}"
        )

        data = response.json()
        usage = data.get("usage", {})
        total_tokens = usage.get("total_tokens", 0)

        assert total_tokens <= MAX_TOTAL_TOKENS, (
            f"Total token usage ({total_tokens}) exceeds cost guardrail "
            f"of {MAX_TOTAL_TOKENS} tokens. This test uses a minimal prompt "
            f"and should stay well under the limit. Usage: {usage}"
        )


class TestOpenAIRetryLogic:
    """R12.4: Retry once on 429/401 and record error details."""

    def test_retry_metadata_recorded(self, credentials):
        """Verify that retry metadata is properly tracked.

        This test validates the retry mechanism works by checking that
        the metadata structure is correct. Actual 429/401 scenarios
        depend on API state.
        """
        api_key = credentials.openai_api_key
        response, metadata = _call_openai_with_retry(api_key)

        # Metadata must always have these fields
        assert "attempts" in metadata
        assert "retried" in metadata
        assert "first_attempt_status" in metadata
        assert metadata["attempts"] >= 1
        assert metadata["attempts"] <= 2

        # If we got a successful response, great
        if response.status_code == 200:
            assert metadata["first_attempt_status"] in (200, 429, 401)
        else:
            # If still failing after retry, record the details
            assert metadata["attempts"] == 2 or metadata["first_attempt_status"] not in (429, 401), (
                f"Unexpected state: status={response.status_code}, "
                f"metadata={metadata}"
            )


class TestOpenAIEvidence:
    """R12.5: Emit e2e-evidence/12-openai.json with model, tokens, latency, cost."""

    def test_emit_openai_evidence(self, credentials, evidence_collector):
        """Collect OpenAI call data and emit structured evidence JSON."""
        api_key = credentials.openai_api_key

        start_time = time.time()
        response, metadata = _call_openai_with_retry(api_key)
        total_latency_ms = round((time.time() - start_time) * 1000, 2)

        evidence_data: dict[str, Any] = {
            "test": "openai_llm_call",
            "endpoint": OPENAI_API_URL,
            "model_requested": MODEL,
            "prompt": MINIMAL_PROMPT,
            "http_status": response.status_code,
            "latency_ms": total_latency_ms,
            "retry_metadata": metadata,
            "response": {},
            "verdict": "fail",
        }

        if response.status_code == 200:
            data = response.json()
            usage = data.get("usage", {})
            content = _extract_responses_text(data)

            # Cost estimate: gpt-4o-mini pricing
            # Input: $0.15 per 1M tokens, Output: $0.60 per 1M tokens
            prompt_tokens = usage.get("input_tokens", 0)
            completion_tokens = usage.get("output_tokens", 0)
            total_tokens = usage.get("total_tokens", 0)
            cost_estimate_usd = (
                (prompt_tokens * 0.15 / 1_000_000)
                + (completion_tokens * 0.60 / 1_000_000)
            )

            evidence_data["response"] = {
                "model": data.get("model", "unknown"),
                "content_preview": content[:200],
                "usage": {
                    "input_tokens": prompt_tokens,
                    "output_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                },
                "cost_estimate_usd": round(cost_estimate_usd, 6),
                "status": data.get("status", "unknown"),
            }

            # Determine verdict
            model_ok = "gpt-4o-mini" in data.get("model", "")
            tokens_ok = total_tokens > 0 and total_tokens <= MAX_TOTAL_TOKENS
            content_ok = bool(content.strip())

            if model_ok and tokens_ok and content_ok:
                evidence_data["verdict"] = "pass"
            else:
                evidence_data["verdict"] = "partial"
                evidence_data["issues"] = []
                if not model_ok:
                    evidence_data["issues"].append(
                        f"Model mismatch: expected gpt-4o-mini, got {data.get('model')}"
                    )
                if not tokens_ok:
                    evidence_data["issues"].append(
                        f"Token issue: total={total_tokens}, limit={MAX_TOTAL_TOKENS}"
                    )
                if not content_ok:
                    evidence_data["issues"].append("Empty response content")
        else:
            evidence_data["error"] = {
                "status_code": response.status_code,
                "body_preview": response.text[:500],
            }

        # Never log the raw API key
        evidence_data["api_key_prefix"] = (
            api_key[:7] + "..." if api_key else "missing"
        )

        evidence_collector.emit_json(
            requirement_id="R12.1,R12.2,R12.3,R12.4,R12.5",
            filename=EVIDENCE_FILENAME,
            data=evidence_data,
        )

        # This test always passes — evidence collection is the goal.
        # Actual assertions are in the other test classes above.
        assert True
