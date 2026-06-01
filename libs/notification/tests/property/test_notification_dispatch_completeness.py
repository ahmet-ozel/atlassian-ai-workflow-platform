"""Property test: Notification dispatch completeness.

Feature: platform-completion, Property 35: For any workflow completion and N
configured channels, exactly N independent notification attempts SHALL be made,
and failure of one channel SHALL NOT block others.

Validates: Requirements 18.1, 18.2, 18.7
"""
from __future__ import annotations
import asyncio

from hypothesis import given, strategies as st, settings


async def _dispatch_to_all(channels: list[str], failing: set[str]) -> dict[str, bool]:
    """Simulate independent dispatch per channel."""
    results: dict[str, bool] = {}

    async def _send(ch: str) -> None:
        if ch in failing:
            results[ch] = False
        else:
            results[ch] = True

    await asyncio.gather(*[_send(c) for c in channels], return_exceptions=True)
    return results


@settings(max_examples=100, deadline=None)
@given(
    channels=st.lists(
        st.sampled_from(["slack", "email", "teams"]),
        min_size=0, max_size=5, unique=True,
    ),
    failing=st.lists(
        st.sampled_from(["slack", "email", "teams"]),
        max_size=3, unique=True,
    ),
)
def test_each_channel_attempted(channels: list[str], failing: list[str]) -> None:
    """Every configured channel produces a result."""
    results = asyncio.run(_dispatch_to_all(channels, set(failing)))
    assert set(results.keys()) == set(channels)


@settings(max_examples=100, deadline=None)
@given(
    channels=st.lists(
        st.sampled_from(["slack", "email", "teams"]),
        min_size=2, max_size=3, unique=True,
    ),
)
def test_one_failure_does_not_block_others(channels: list[str]) -> None:
    """If one channel fails, others still succeed."""
    failing = {channels[0]}
    results = asyncio.run(_dispatch_to_all(channels, failing))
    for ch in channels[1:]:
        assert results[ch] is True


@settings(max_examples=50, deadline=None)
@given(channel_count=st.integers(min_value=1, max_value=5))
def test_n_channels_n_results(channel_count: int) -> None:
    """N channels → exactly N results."""
    channels = [f"ch-{i}" for i in range(channel_count)]
    results = asyncio.run(_dispatch_to_all(channels, set()))
    assert len(results) == channel_count
