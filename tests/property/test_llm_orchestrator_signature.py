"""LlmOrchestrator constructor signature compatibility checks.

=============================================================================
LEGACY  CURRENT KWARG MAPPING (recorded from LlmOrchestrator.__init__)
=============================================================================

Current LlmOrchestrator signature (from platform/libs/llm-orchestrator/src/
llm_orchestrator/orchestrator.py, @dataclass):

    @dataclass
    class LlmOrchestrator:
        primary: LlmProviderStream
        fallback: LlmProviderStream | None = None
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

Current kwargs:
  - primary   (required, positional-or-keyword)
  - fallback  (optional, default=None)
  - sleep     (optional, default=asyncio.sleep)

Legacy kwargs retained here as regression inputs:
  - primary=    still valid (same name, no change here)
  - fallbacks=  LEGACY (plural); current name is `fallback` (singular)
  - provider=   LEGACY (alternative name that may have been used)

=============================================================================
OBSERVED TypeError MESSAGES (integration anchor: test_llm_orchestrator.py)
=============================================================================

Running: pytest platform/tests/unit/test_llm_orchestrator.py -x --tb=short

Actual failure observed on unfixed code:
  FAILED tests/unit/test_llm_orchestrator.py::test_from_env_dispatches_real_providers_to_not_implemented[openai-OpenAIProvider]
  E   Failed: DID NOT RAISE <class 'NotImplementedError'>

The test was written when OpenAIProvider/AnthropicProvider/VLLMProvider were
stubs that raised NotImplementedError on instantiation. The production code
has since been updated to real implementations, but the test still expects
the stub behavior. The test's expected behavior (NotImplementedError) no
longer matches the current production contract.

For LlmOrchestrator itself, legacy kwargs that raise TypeError:
  - LlmOrchestrator(fallbacks=[...])   TypeError: __init__() got an
    unexpected keyword argument 'fallbacks'
  - LlmOrchestrator(provider=primary)  TypeError: __init__() got an
    unexpected keyword argument 'provider'

=============================================================================
DUAL-FORM TEST STRUCTURE
=============================================================================

This test captures both sides of the constructor compatibility check:
  1. Property half (Hypothesis): current-signature construction succeeds
      PASSES on unfixed code (production LlmOrchestrator is fine)
  2. Deterministic half: legacy-kwarg construction raises TypeError
      PASSES on unfixed code (confirms legacy kwargs are rejected)

The integration anchor (test_llm_orchestrator.py) FAILS on unfixed code,
confirming the overall bug condition exists.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from llm_orchestrator import LlmOrchestrator
from llm_orchestrator.orchestrator import LlmProviderStream


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_provider() -> Any:
    """Create a minimal mock that satisfies the LlmProviderStream protocol."""
    mock = MagicMock()
    mock.downtime.return_value = 0
    mock.stream.return_value = aiter([])
    return mock


async def aiter(items):  # type: ignore[no-untyped-def]
    """Async iterator helper for empty stream."""
    for item in items:
        yield item


# ---------------------------------------------------------------------------
# Hypothesis strategy: generate valid argument bundles from current signature
# ---------------------------------------------------------------------------

# The current LlmOrchestrator.__init__ parameters (from @dataclass):
# primary: LlmProviderStream  (required)
# fallback: LlmProviderStream | None = None  (optional)
# sleep: Callable = asyncio.sleep  (optional)
#
# We only vary the optional `fallback` parameter since `primary` must be a
# valid LlmProviderStream and `sleep` is a callable.

_CURRENT_PARAMS = list(inspect.signature(LlmOrchestrator.__init__).parameters.keys())
# Expected: ['self', 'primary', 'fallback', 'sleep']


@settings(max_examples=5, deadline=None)
@given(
    include_fallback=st.booleans(),
)
def test_surface4_llm_orchestrator_current_signature(include_fallback: bool) -> None:
    """Property: LlmOrchestrator constructs without TypeError using current signature.

    This is the current-signature half of the dual-form constructor test.
    For any valid argument bundle drawn from the current constructor
    signature, LlmOrchestrator must construct without raising TypeError.

    On UNFIXED code: this half PASSES (production LlmOrchestrator is fine).
    The integration anchor (test_llm_orchestrator.py) FAILS separately.

    Also includes a deterministic sub-test asserting that LEGACY kwargs
    (e.g. `fallbacks=`, `provider=`) raise TypeError.
    """
    # Verify the current signature has the expected parameters
    assert "primary" in _CURRENT_PARAMS, (
        f"Expected 'primary' in LlmOrchestrator params, got: {_CURRENT_PARAMS}"
    )
    assert "fallback" in _CURRENT_PARAMS, (
        f"Expected 'fallback' in LlmOrchestrator params, got: {_CURRENT_PARAMS}"
    )
    assert "fallbacks" not in _CURRENT_PARAMS, (
        f"Legacy 'fallbacks' (plural) should NOT be in current params: {_CURRENT_PARAMS}"
    )

    # Build a valid argument bundle using the CURRENT signature
    primary_mock = _make_mock_provider()

    kwargs: dict[str, Any] = {"primary": primary_mock}
    if include_fallback:
        kwargs["fallback"] = _make_mock_provider()

    # Current-signature construction MUST succeed without TypeError
    try:
        orchestrator = LlmOrchestrator(**kwargs)
        assert orchestrator.primary is primary_mock
        if include_fallback:
            assert orchestrator.fallback is not None
        else:
            assert orchestrator.fallback is None
    except TypeError as exc:
        pytest.fail(
            f"LlmOrchestrator(**{list(kwargs.keys())}) raised TypeError "
            f"unexpectedly on current-signature construction: {exc}"
        )

    # -----------------------------------------------------------------------
    # Deterministic sub-test: LEGACY kwargs MUST raise TypeError
    # Legacy-kwarg construction raises TypeError.
    # -----------------------------------------------------------------------

    # Legacy kwarg: `fallbacks=` (plural) - was the old name before rename to `fallback`
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        LlmOrchestrator(primary=primary_mock, fallbacks=[_make_mock_provider()])

    # Legacy kwarg: `provider=` - alternative legacy name for `primary`
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        LlmOrchestrator(provider=primary_mock)

    # Legacy kwarg: both legacy names together
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        LlmOrchestrator(
            provider=primary_mock,
            fallbacks=[_make_mock_provider()],
        )
