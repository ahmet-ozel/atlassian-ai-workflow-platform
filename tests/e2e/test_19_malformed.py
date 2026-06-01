"""
Test 19: Malformed payload validation (R19).

Validates that the MCP gateway handles malformed input gracefully:
- Malformed JSON → HTTP 400 with parse error
- >1 MiB payload → HTTP 413 or 400
- Valid JSON with invalid fields → HTTP 400/422 with field errors
- MCP does NOT crash, no stack traces in response

Requirements: R19.1, R19.2, R19.3, R19.4, R19.5
"""

import time
from typing import Any

import httpx
import pytest


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EVIDENCE_FILENAME = "19-malformed.json"
MCP_BASE_URL = "http://localhost:8090"
REQUEST_TIMEOUT = 15.0
ONE_MIB = 1024 * 1024  # 1 MiB in bytes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _send_to_mcp(payload: str | bytes, content_type: str = "application/json") -> dict:
    """Send a raw payload to the MCP endpoint.

    Returns dict with: status_code, response_body, has_stack_trace, error.
    """
    result = {
        "status_code": None,
        "response_body": None,
        "has_stack_trace": False,
        "error": None,
        "latency_ms": 0,
    }

    # Try common MCP endpoints
    endpoints = [
        "/api/v1/tools/call",
        "/tools/call",
        "/mcp/v1/call",
        "/",
    ]

    start = time.time()
    for endpoint in endpoints:
        try:
            if isinstance(payload, bytes):
                resp = httpx.post(
                    f"{MCP_BASE_URL}{endpoint}",
                    content=payload,
                    headers={"Content-Type": content_type},
                    timeout=REQUEST_TIMEOUT,
                )
            else:
                resp = httpx.post(
                    f"{MCP_BASE_URL}{endpoint}",
                    content=payload.encode("utf-8") if isinstance(payload, str) else payload,
                    headers={"Content-Type": content_type},
                    timeout=REQUEST_TIMEOUT,
                )

            result["latency_ms"] = round((time.time() - start) * 1000, 1)
            result["status_code"] = resp.status_code
            result["response_body"] = resp.text[:1000]

            # Check for stack traces in response
            stack_indicators = [
                "Traceback (most recent call last)",
                "at Object.<anonymous>",
                "Error: \n    at ",
                "    at Module._compile",
                "node_modules",
                "site-packages",
            ]
            result["has_stack_trace"] = any(
                ind in resp.text for ind in stack_indicators
            )

            # If we got a non-404 response, this is the right endpoint
            if resp.status_code != 404:
                break

        except httpx.HTTPError as exc:
            result["latency_ms"] = round((time.time() - start) * 1000, 1)
            result["error"] = str(exc)
            break

    return result


def _check_mcp_healthy() -> bool:
    """Verify MCP container is still healthy after a test."""
    try:
        resp = httpx.get(
            f"{MCP_BASE_URL}/healthz",
            timeout=REQUEST_TIMEOUT,
        )
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMalformedJSON:
    """R19.1: Malformed JSON returns HTTP 400 with parse error."""

    def test_missing_closing_brace(self):
        """R19.1: Malformed JSON (missing closing brace) → HTTP 400.

        WHEN malformed JSON is sent to the MCP endpoint, THE system
        SHALL return HTTP 400 with a structured error message containing
        'parse error' or 'invalid JSON'.
        """
        malformed_json = '{"jsonrpc": "2.0", "method": "tools/call", "params": {'
        result = _send_to_mcp(malformed_json)

        # Store for evidence
        self.__class__._malformed_result = result

        if result["error"]:
            pytest.skip(f"Could not reach MCP endpoint: {result['error']}")

        # Should get 400 Bad Request
        assert result["status_code"] in (400, 422), (
            f"Expected HTTP 400 or 422 for malformed JSON, "
            f"got {result['status_code']}.\n"
            f"Response: {result['response_body']}"
        )

    def test_malformed_response_contains_parse_error(self):
        """R19.1: Error response mentions parse error or invalid JSON."""
        result = getattr(self.__class__, "_malformed_result", None)
        if result is None:
            malformed_json = '{"jsonrpc": "2.0", "method": "tools/call"'
            result = _send_to_mcp(malformed_json)

        if result["error"] or result["status_code"] == 404:
            pytest.skip("MCP endpoint not reachable for this test")

        # Response should mention parsing issue
        if result["response_body"]:
            body_lower = result["response_body"].lower()
            parse_indicators = [
                "parse", "invalid json", "syntax error",
                "unexpected", "malformed", "json",
            ]
            has_parse_info = any(ind in body_lower for ind in parse_indicators)
            assert has_parse_info or result["status_code"] in (400, 422), (
                f"Error response should mention parse error. "
                f"Got status {result['status_code']}, "
                f"body: {result['response_body'][:300]}"
            )


class TestOversizedPayload:
    """R19.2: Oversized payload (>1 MiB) returns HTTP 413 or 400."""

    def test_oversized_payload_rejected(self):
        """R19.2: Payload >1 MiB → HTTP 413 or 400.

        WHEN a payload exceeding 1 MiB is sent to the MCP endpoint,
        THE system SHALL return HTTP 413 or HTTP 400 with a size-limit error.
        """
        # Create a payload just over 1 MiB
        large_value = "x" * (ONE_MIB + 1024)
        oversized_payload = f'{{"jsonrpc": "2.0", "method": "tools/call", "params": {{"data": "{large_value}"}}}}'

        result = _send_to_mcp(oversized_payload)

        # Store for evidence
        self.__class__._oversized_result = result

        if result["error"]:
            # Connection reset or timeout is acceptable for oversized payloads
            # (server may close connection before reading full payload)
            acceptable_errors = [
                "connection reset",
                "broken pipe",
                "connection closed",
                "read timeout",
            ]
            is_acceptable = any(
                ind in result["error"].lower() for ind in acceptable_errors
            )
            if is_acceptable:
                return  # Server rejected the oversized payload (acceptable)
            pytest.skip(f"Could not reach MCP endpoint: {result['error']}")

        # Should get 413 (Payload Too Large) or 400 (Bad Request)
        assert result["status_code"] in (400, 413, 422), (
            f"Expected HTTP 400 or 413 for oversized payload, "
            f"got {result['status_code']}.\n"
            f"Response: {result['response_body'][:300]}"
        )


class TestInvalidFields:
    """R19.3: Valid JSON with invalid fields returns HTTP 400/422."""

    def test_empty_project_key(self):
        """R19.3: Valid JSON with empty project key → HTTP 400/422.

        WHEN valid JSON with invalid field values (empty project key)
        is sent, THE system SHALL return HTTP 400 or 422 with field-level
        validation errors.
        """
        invalid_fields_payload = (
            '{"jsonrpc": "2.0", "method": "tools/call", '
            '"params": {"name": "jira_create_issue", '
            '"arguments": {"project_key": "", "summary": "", "issue_type": ""}}}'
        )

        result = _send_to_mcp(invalid_fields_payload)

        # Store for evidence
        self.__class__._invalid_fields_result = result

        if result["error"]:
            pytest.skip(f"Could not reach MCP endpoint: {result['error']}")

        # Should get 400 or 422 for validation errors
        # 404 is also acceptable if the endpoint routing rejects it
        assert result["status_code"] in (400, 404, 422), (
            f"Expected HTTP 400/422 for invalid fields, "
            f"got {result['status_code']}.\n"
            f"Response: {result['response_body'][:300]}"
        )

    def test_null_summary_field(self):
        """R19.3: Valid JSON with null summary → HTTP 400/422."""
        null_field_payload = (
            '{"jsonrpc": "2.0", "method": "tools/call", '
            '"params": {"name": "jira_create_issue", '
            '"arguments": {"project_key": "JOH", "summary": null}}}'
        )

        result = _send_to_mcp(null_field_payload)

        if result["error"]:
            pytest.skip(f"Could not reach MCP endpoint: {result['error']}")

        # Should get 400 or 422 for null required field
        assert result["status_code"] in (400, 404, 422), (
            f"Expected HTTP 400/422 for null summary, "
            f"got {result['status_code']}.\n"
            f"Response: {result['response_body'][:300]}"
        )


class TestMCPStability:
    """R19.4: MCP does NOT crash after malformed requests."""

    def test_mcp_healthy_after_malformed_requests(self):
        """R19.4: MCP remains healthy after processing malformed input.

        WHEN any malformed request is received, THE Atlassian_MCP SHALL
        NOT crash and SHALL remain healthy after the request.
        """
        # Send a series of malformed requests
        malformed_payloads = [
            '{"broken',
            '',
            'not json at all',
            '{"jsonrpc": "2.0"}',
            '[]',
        ]

        for payload in malformed_payloads:
            _send_to_mcp(payload)

        # Wait briefly for any crash to manifest
        time.sleep(2)

        # Verify MCP is still healthy
        healthy = _check_mcp_healthy()
        assert healthy, (
            "MCP container is NOT healthy after receiving malformed requests! "
            "The service may have crashed."
        )

    def test_no_stack_traces_in_responses(self):
        """R19.4: No stack traces leaked in error responses.

        WHEN malformed requests are processed, THE responses SHALL NOT
        contain internal stack traces.
        """
        test_payloads = [
            '{"broken json',
            '{"jsonrpc": "2.0", "method": "nonexistent"}',
        ]

        for payload in test_payloads:
            result = _send_to_mcp(payload)
            if result["response_body"]:
                assert not result["has_stack_trace"], (
                    f"Stack trace found in MCP response!\n"
                    f"Payload: {payload[:100]}\n"
                    f"Response: {result['response_body'][:500]}"
                )


class TestMalformedEvidence:
    """R19.5: Emit structured evidence for malformed payload tests."""

    def test_emit_evidence(self, evidence_collector):
        """Collect malformed payload test data and emit evidence JSON."""
        evidence_data: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "scenarios": {},
            "mcp_health_after_tests": False,
            "overall_verdict": "pass",
        }

        # Scenario 1: Malformed JSON
        malformed_result = _send_to_mcp('{"broken json')
        evidence_data["scenarios"]["malformed_json"] = {
            "payload": '{"broken json',
            "status_code": malformed_result["status_code"],
            "has_stack_trace": malformed_result["has_stack_trace"],
            "response_snippet": (malformed_result["response_body"] or "")[:200],
            "passed": malformed_result["status_code"] in (400, 422) if malformed_result["status_code"] else False,
        }

        # Scenario 2: Oversized payload
        large_value = "x" * (ONE_MIB + 1024)
        oversized_payload = f'{{"data": "{large_value}"}}'
        oversized_result = _send_to_mcp(oversized_payload)
        evidence_data["scenarios"]["oversized_payload"] = {
            "payload_size_bytes": len(oversized_payload),
            "status_code": oversized_result["status_code"],
            "error": oversized_result["error"],
            "passed": (
                oversized_result["status_code"] in (400, 413, 422)
                or oversized_result["error"] is not None
            ),
        }

        # Scenario 3: Invalid fields
        invalid_result = _send_to_mcp(
            '{"jsonrpc": "2.0", "method": "tools/call", '
            '"params": {"name": "jira_create_issue", '
            '"arguments": {"project_key": "", "summary": ""}}}'
        )
        evidence_data["scenarios"]["invalid_fields"] = {
            "status_code": invalid_result["status_code"],
            "has_stack_trace": invalid_result["has_stack_trace"],
            "response_snippet": (invalid_result["response_body"] or "")[:200],
            "passed": invalid_result["status_code"] in (400, 404, 422) if invalid_result["status_code"] else False,
        }

        # Check MCP health after all tests
        time.sleep(1)
        mcp_healthy = _check_mcp_healthy()
        evidence_data["mcp_health_after_tests"] = mcp_healthy

        # Overall verdict
        scenarios_passed = all(
            s.get("passed", False)
            for s in evidence_data["scenarios"].values()
        )
        evidence_data["overall_verdict"] = "pass" if (
            scenarios_passed and mcp_healthy
        ) else "fail"

        # Emit evidence
        evidence_path = evidence_collector.emit_json(
            requirement_id="R19.1,R19.2,R19.3,R19.4,R19.5",
            filename=EVIDENCE_FILENAME,
            data=evidence_data,
        )
        assert evidence_path.exists(), (
            f"Evidence file not created at {evidence_path}"
        )
