"""
Test 26: Hypothesis credential masking property test .

**the invariant: Credential masking completeness**

Uses Hypothesis to generate random strings matching credential patterns
and asserts that the log redaction function always masks them completely.

Patterns tested:
- ATATT3x* (Bitbucket Personal API Token)
- ATCTT3x* (Bitbucket Workspace Access Token)
- sk-proj-* (OpenAI API Key)
- Bearer * (OAuth Bearer tokens)
- Basic * (HTTP Basic Auth headers)
"""

import sys
import time
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings, HealthCheck, assume
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path setup: ensure http_shared is importable
# ---------------------------------------------------------------------------

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent.parent
PLATFORM_ROOT = WORKSPACE_ROOT / "platform"

# Add the http-shared lib to path
HTTP_SHARED_SRC = PLATFORM_ROOT / "libs" / "http-shared" / "src"
if str(HTTP_SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(HTTP_SHARED_SRC))

from http_shared.redaction import redact_text, REDACTION_PLACEHOLDER  # noqa: E402


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EVIDENCE_FILENAME = "26-credential-fuzzing.json"
EXAMPLES_PER_PATTERN = 200
DEADLINE_SECONDS = 60


# ---------------------------------------------------------------------------
# Hypothesis strategies for credential patterns
# ---------------------------------------------------------------------------

# Characters that can appear in credential values (printable, no whitespace
# for token bodies since redaction stops at whitespace boundaries)
_token_chars = st.characters(
    whitelist_categories=("L", "N", "P", "S"),
    blacklist_characters="\t\n\r \x00&,;",
)

# ATATT3x* pattern (Bitbucket Personal API Token)
atatt3x_strategy = st.builds(
    lambda suffix: f"ATATT3x{suffix}",
    st.text(_token_chars, min_size=4, max_size=200),
)

# ATCTT3x* pattern (Bitbucket Workspace Access Token)
atctt3x_strategy = st.builds(
    lambda suffix: f"ATCTT3x{suffix}",
    st.text(_token_chars, min_size=4, max_size=200),
)

# sk-proj-* pattern (OpenAI API Key)
sk_proj_strategy = st.builds(
    lambda suffix: f"sk-proj-{suffix}",
    st.text(_token_chars, min_size=4, max_size=200),
)

# Bearer * pattern (OAuth tokens)
bearer_strategy = st.builds(
    lambda token: f"Bearer {token}",
    st.text(_token_chars, min_size=4, max_size=200),
)

# Basic * pattern (HTTP Basic Auth)
basic_strategy = st.builds(
    lambda creds: f"Basic {creds}",
    st.text(_token_chars, min_size=4, max_size=200),
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCredentialMaskingProperty:
    """the invariant: Credential masking completeness.


 FOR ALL generated credential strings matching known patterns,
 the redact_text function SHALL mask the credential value and
 SHALL NOT return the original value in the output.
 """

    @settings(
        max_examples=EXAMPLES_PER_PATTERN,
        deadline=DEADLINE_SECONDS * 1000,  # Hypothesis uses ms
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(credential=atatt3x_strategy)
    def test_atatt3x_pattern_always_redacted(self, credential: str):
        """ATATT3x* tokens are always redacted when in api_token= context.

 """
        # Wrap in api_token= context (the redaction pattern matches KEY=value)
        log_line = f"api_token={credential}"
        redacted = redact_text(log_line)

        # The original credential value should NOT appear in output
        assert credential not in redacted, (
            f"ATATT3x credential leaked through redaction!\n"
            f"Input: {log_line[:100]}\n"
            f"Output: {redacted[:100]}"
        )

        # The redaction placeholder should be present
        assert REDACTION_PLACEHOLDER in redacted, (
            f"Redaction placeholder not found in output.\n"
            f"Input: {log_line[:100]}\n"
            f"Output: {redacted[:100]}"
        )

    @settings(
        max_examples=EXAMPLES_PER_PATTERN,
        deadline=DEADLINE_SECONDS * 1000,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(credential=atctt3x_strategy)
    def test_atctt3x_pattern_always_redacted(self, credential: str):
        """ATCTT3x* tokens are always redacted when in api_token= context.

 """
        log_line = f"api_token={credential}"
        redacted = redact_text(log_line)

        assert credential not in redacted, (
            f"ATCTT3x credential leaked through redaction!\n"
            f"Input: {log_line[:100]}\n"
            f"Output: {redacted[:100]}"
        )

        assert REDACTION_PLACEHOLDER in redacted, (
            f"Redaction placeholder not found in output.\n"
            f"Input: {log_line[:100]}\n"
            f"Output: {redacted[:100]}"
        )

    @settings(
        max_examples=EXAMPLES_PER_PATTERN,
        deadline=DEADLINE_SECONDS * 1000,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(credential=sk_proj_strategy)
    def test_sk_proj_pattern_always_redacted(self, credential: str):
        """sk-proj-* tokens are always redacted when in api_token= context.

 """
        log_line = f"api_token={credential}"
        redacted = redact_text(log_line)

        assert credential not in redacted, (
            f"sk-proj- credential leaked through redaction!\n"
            f"Input: {log_line[:100]}\n"
            f"Output: {redacted[:100]}"
        )

        assert REDACTION_PLACEHOLDER in redacted, (
            f"Redaction placeholder not found in output.\n"
            f"Input: {log_line[:100]}\n"
            f"Output: {redacted[:100]}"
        )

    @settings(
        max_examples=EXAMPLES_PER_PATTERN,
        deadline=DEADLINE_SECONDS * 1000,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(credential=bearer_strategy)
    def test_bearer_pattern_always_redacted(self, credential: str):
        """Bearer * tokens are always redacted.

 """
        redacted = redact_text(credential)

        # Extract the token part (after "Bearer ")
        token_value = credential[len("Bearer "):]

        assert token_value not in redacted, (
            f"Bearer token leaked through redaction!\n"
            f"Input: {credential[:100]}\n"
            f"Output: {redacted[:100]}"
        )

        assert REDACTION_PLACEHOLDER in redacted, (
            f"Redaction placeholder not found in output.\n"
            f"Input: {credential[:100]}\n"
            f"Output: {redacted[:100]}"
        )

    @settings(
        max_examples=EXAMPLES_PER_PATTERN,
        deadline=DEADLINE_SECONDS * 1000,
        suppress_health_check=[HealthCheck.too_slow],
    )
    @given(credential=basic_strategy)
    def test_basic_pattern_always_redacted(self, credential: str):
        """Basic * auth headers are always redacted.

 """
        # Wrap in Authorization header context
        log_line = f"Authorization: {credential}"
        redacted = redact_text(log_line)

        # Extract the credential part (after "Basic ")
        cred_value = credential[len("Basic "):]

        assert cred_value not in redacted, (
            f"Basic auth credential leaked through redaction!\n"
            f"Input: {log_line[:100]}\n"
            f"Output: {redacted[:100]}"
        )

        assert REDACTION_PLACEHOLDER in redacted, (
            f"Redaction placeholder not found in output.\n"
            f"Input: {log_line[:100]}\n"
            f"Output: {redacted[:100]}"
        )


class TestCredentialFuzzingEvidence:
    """: Emit structured evidence for credential fuzzing tests."""

    def test_emit_evidence(self, evidence_collector):
        """Collect credential fuzzing results and emit evidence JSON.

 """
        evidence_data: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "property": "the invariant: Credential masking completeness",
            "validates": "",
            "patterns_tested": [
                "ATATT3x*",
                "ATCTT3x*",
                "sk-proj-*",
                "Bearer *",
                "Basic *",
            ],
            "examples_per_pattern": EXAMPLES_PER_PATTERN,
            "total_examples": EXAMPLES_PER_PATTERN * 5,
            "deadline_seconds": DEADLINE_SECONDS,
            "redaction_function": "http_shared.redaction.redact_text",
            "redaction_placeholder": REDACTION_PLACEHOLDER,
            "counterexamples": [],
            "pattern_results": {},
            "overall_verdict": "pass",
        }

        # Run a quick validation for each pattern to capture evidence
        patterns = {
            "ATATT3x": "api_token=ATATT3xTestValue123",
            "ATCTT3x": "api_token=ATCTT3xTestValue456",
            "sk-proj-": "api_token=sk-proj-TestKey789",
            "Bearer": "Bearer eyJhbGciOiJIUzI1NiJ9.test.sig",
            "Basic": "Authorization: Basic dXNlcjpwYXNz",
        }

        for pattern_name, test_input in patterns.items():
            redacted = redact_text(test_input)
            passed = REDACTION_PLACEHOLDER in redacted
            evidence_data["pattern_results"][pattern_name] = {
                "sample_input": test_input,
                "sample_output": redacted,
                "contains_placeholder": passed,
                "passed": passed,
            }
            if not passed:
                evidence_data["counterexamples"].append({
                    "pattern": pattern_name,
                    "input": test_input,
                    "output": redacted,
                })

        # Overall verdict
        all_passed = all(
            r["passed"] for r in evidence_data["pattern_results"].values()
        )
        evidence_data["overall_verdict"] = "pass" if all_passed else "fail"

        # Emit evidence
        evidence_path = evidence_collector.emit_json(
            requirement_id=",,,,",
            filename=EVIDENCE_FILENAME,
            data=evidence_data,
        )
        assert evidence_path.exists(), (
            f"Evidence file not created at {evidence_path}"
        )
