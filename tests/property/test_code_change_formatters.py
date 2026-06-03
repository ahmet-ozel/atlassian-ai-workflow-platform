"""invariant 13 — Code-change formatters ve routing.



Hypothesis-driven verification of the four pure helpers used by the
``code_change_*`` workflow family in
`/``:

*:func:`temporal_shared.code_change.compute_branch_name` — collision-free
 iteration branch picker (, design.md invariant(a)).
*:func:`temporal_shared.code_change.format_commit_message` — ``[bot]``
 prefix + ``Co-authored-by`` provenance footer (, design.md
 invariant(b)).
*:func:`mcp_client.deployment_router.select_pr_create_tool` — Bitbucket
 Cloud vs Data Center MCP tool parity (, design.md invariant(c),
 §16.15 T9).
*:func:`temporal_shared.branch_rules.route_by_branch_pattern` —
 hotfix/release deny / allowlist semantics (, design.md invariant(d), §16.15.6 U6).

Invariant statements (mirror design.md §"invariant")
-----------------------------------------------------

(P1) ``compute_branch_name(issue_key, iter, existing)`` is **collision-free**:
 the returned branch never appears in *existing* whenever the
 function takes the bare ``ai/{issue_key}`` branch (iter == 1 +
 slot free); for iter == 1 with the slot taken or any iter >= 2
 the function returns the iter-suffixed form, which the workflow
 caller is expected to keep fresh by monotonically incrementing
 ``iter`` (we therefore do not assert disjointness from
 *existing* on the iter-suffixed branch — the function does not
 consult the set in that case, and asserting otherwise would
 contradict the documented contract).

(P2) ``compute_branch_name`` always returns one of two shapes:
 ``"ai/{issue_key}"`` or ``"ai/{issue_key}-iter{iter}"`` — and the
 bare form is selected **iff** ``iter == 1`` AND the bare slot is
 free in *existing*.

(P3) ``compute_branch_name`` is deterministic and pure: given the same
 ``(issue_key, iter, existing)`` triple, two consecutive calls
 return identical strings; passing *existing* as a list, tuple,
 set or frozenset does not change the result; mutating the input
 iterable after the call cannot affect the returned value.

(P4) ``format_commit_message(message, issue_key, iter, bot_email)``
 output **starts with** the literal ``"[bot] "`` prefix.

(P5) ``format_commit_message`` output **ends with** the trailer line
 ``"Co-authored-by: ai-bot <{bot_email}>"`` and the trailer is
 separated from the body by exactly one blank line (Git
 convention so ``git log --pretty=%(trailers)`` parses it).

(P6) ``format_commit_message`` echoes the ``message`` body unchanged
 (modulo trailing whitespace) — the function does not silently
 rewrite, truncate, or re-wrap LLM output.

(P7) ``select_pr_create_tool("cloud")`` returns
 ``"bitbucket_create_pull_request_cloud"`` and
 ``select_pr_create_tool("server")`` returns
 ``"bitbucket_create_pull_request_dc"`` — the parity mapping is
 exhaustive and any other input raises:class:`KeyError` (no
 silent fallback to either side, by design).

(P8) ``route_by_branch_pattern`` denies ``code_change_commit_only`` on
 every branch matching ``hotfix/*`` (PR open mandatory).

(P9) ``route_by_branch_pattern`` allows only ``pr_review`` and
 ``confluence_doc_update`` on branches matching ``release/*`` —
 every other workflow type is denied with the rule's audit
 reason.

(P10) ``route_by_branch_pattern`` always allows on branches matching
 ``ai/*`` because no default rule matches the ``ai/`` prefix
 (open default — ``no_rule_matched``).

(P11) ``route_by_branch_pattern`` is deterministic and pure: two
 consecutive calls with the same arguments return identical
 decisions.

Hypothesis configuration
------------------------

Every property runs at ``max_examples=100`` with ``deadline=None`` per
the brief, matching the existing invariant cadence (see
``test_explain_keyword.py``, ``test_fix_keyword.py``).
"""

from __future__ import annotations

import re
from typing import Final

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_client.deployment_router import (
    BITBUCKET_CREATE_PR_CLOUD,
    BITBUCKET_CREATE_PR_DC,
    select_pr_create_tool,
)
from temporal_shared.branch_rules import (
    DEFAULT_BRANCH_PATTERN_RULES,
    DEFAULT_HOTFIX_RULE,
    DEFAULT_RELEASE_RULE,
    BranchPatternRule,
    RouteDecision,
    route_by_branch_pattern,
)
from temporal_shared.code_change import (
    BOT_COMMIT_PREFIX,
    compute_branch_name,
    format_commit_message,
)
from temporal_shared.identifiers import InvalidIssueKeyError


# ---------------------------------------------------------------------------
# Constants — pinned from the production modules
# ---------------------------------------------------------------------------

#: Closed set of workflow types referenced by the design (, 
#: ``WORKFLOW_TYPE_CAPABILITIES``). Hypothesis samples from this list
#: so the strategy stays inside the documented universe.
_WORKFLOW_TYPES: Final[tuple[str, ...]] = (
    "code_change_with_test",
    "code_change_commit_only",
    "pr_review",
    "confluence_doc_create",
    "confluence_doc_update",
    "research_publish_confluence",
    "research_summary_jira",
    "remote_ssh_test_only",
    "multi_step",
    "noop_test",
)

#: Workflow types that ``DEFAULT_RELEASE_RULE`` permits on ``release/*``
#: branches (mirrors:data:`DEFAULT_RELEASE_RULE.allowed_workflow_types`).
_RELEASE_ALLOWED: Final[frozenset[str]] = frozenset(
    {"pr_review", "confluence_doc_update"}
)

#: Hard-coded shape regexes that pin the formatter outputs. Both come
#: from the operational rule.md § / design.md invariant(a). The bare form
#: matches a Jira issue key with at least 2 chars in the project prefix
#: and a positive issue number; the iter form appends ``-iter{N}`` with
#: ``N >= 1``.
_BARE_BRANCH_RE: Final[re.Pattern[str]] = re.compile(
    r"^ai/[A-Z][A-Z0-9_]+-[1-9][0-9]*$"
)
_ITER_BRANCH_RE: Final[re.Pattern[str]] = re.compile(
    r"^ai/[A-Z][A-Z0-9_]+-[1-9][0-9]*-iter[1-9][0-9]*$"
)

#: Trailer regex (RFC-5322-ish — same shape the production validator
#: accepts in:mod:`temporal_shared.code_change`).
_TRAILER_RE: Final[re.Pattern[str]] = re.compile(
    r"^Co-authored-by: ai-bot <[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}>$"
)


# ---------------------------------------------------------------------------
# Hypothesis strategies — issue keys, iterations, branches, etc.
# ---------------------------------------------------------------------------

# ``"PROJ-NNN"`` shape: uppercase project prefix (>=2 chars, first must
# be a letter) + dash + positive integer with no leading zero. The
# upstream regex is ``^[A-Z][A-Z0-9_]+-[1-9][0-9]*$`` — see
# ``identifiers._ISSUE_KEY_RE``.
_PROJECT_PREFIX_FIRST: Final[st.SearchStrategy[str]] = st.sampled_from(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
)
_PROJECT_PREFIX_REST: Final[st.SearchStrategy[str]] = st.text(
    alphabet=st.sampled_from("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"),
    min_size=1,
    max_size=4,
)
_ISSUE_NUM: Final[st.SearchStrategy[int]] = st.integers(min_value=1, max_value=99_999)


@st.composite
def _issue_keys(draw: st.DrawFn) -> str:
    """Strategy emitting a valid ``PROJ-NNN`` issue key."""
    head = draw(_PROJECT_PREFIX_FIRST)
    tail = draw(_PROJECT_PREFIX_REST)
    num = draw(_ISSUE_NUM)
    return f"{head}{tail}-{num}"


# Iteration counter ∈ [1, 10] per the brief.
_ITERATIONS: Final[st.SearchStrategy[int]] = st.integers(min_value=1, max_value=10)

# Bot email — short ASCII local part + a small fake-TLD domain. The
# validator only checks RFC-5322-ish shape, so the strategy just needs
# to keep the body parseable.
_BOT_EMAIL_LOCAL: Final[st.SearchStrategy[str]] = st.text(
    alphabet=st.sampled_from(
        "abcdefghijklmnopqrstuvwxyz0123456789._-"
    ),
    min_size=1,
    max_size=12,
).filter(lambda s: not s.startswith(".") and not s.endswith("."))
_BOT_EMAIL_DOMAIN: Final[st.SearchStrategy[str]] = st.sampled_from(
    ("company.com", "ai-bot.local", "example.org", "bots.io")
)


@st.composite
def _bot_emails(draw: st.DrawFn) -> str:
    """Strategy emitting a parseable bot email address."""
    local = draw(_BOT_EMAIL_LOCAL)
    domain = draw(_BOT_EMAIL_DOMAIN)
    return f"{local}@{domain}"


# Commit message body — non-empty short ASCII, single-line and
# multi-line shapes both covered. ``rstrip``-equivalent characters in
# the production helper are preserved when computing the expected
# normalised body.
_MESSAGES: Final[st.SearchStrategy[str]] = st.one_of(
    st.text(min_size=1, max_size=80),
    # Multi-line body — exercises the rstrip semantic on the trailing
    # newlines and the body→trailer separator.
    st.lists(
        st.text(min_size=0, max_size=40),
        min_size=2,
        max_size=4,
    ).map(lambda parts: "\n".join(parts) + "\n"),
)


# Branch-name strategy for the routing test: produces names from each of
# the four shapes the brief calls out (ai/, hotfix/, release/, feature/)
# so every default rule's glob has a non-trivial population of matching
# *and* non-matching examples.
@st.composite
def _branches(draw: st.DrawFn) -> str:
    issue = draw(_issue_keys())
    iter_n = draw(_ITERATIONS)
    shape = draw(
        st.sampled_from(("ai_bare", "ai_iter", "hotfix", "release", "feature"))
    )
    if shape == "ai_bare":
        return f"ai/{issue}"
    if shape == "ai_iter":
        return f"ai/{issue}-iter{iter_n}"
    if shape == "hotfix":
        # Use both ``hotfix/{issue}`` and ``hotfix/{name}`` shapes so
        # the glob matcher's case-sensitivity is exercised.
        suffix = draw(st.sampled_from((issue, "ABC-123", "release-2025-04")))
        return f"hotfix/{suffix}"
    if shape == "release":
        suffix = draw(st.sampled_from(("2025-04", "v1.2.3", issue)))
        return f"release/{suffix}"
    # feature/...
    suffix = draw(st.sampled_from((issue, "AUTH-12", "experimental")))
    return f"feature/{suffix}"


# Strategy for the workflow type used by the routing test. Sampled from
# the closed universe so every default-rule branch is exercised.
_WORKFLOW_TYPE_STRATEGY: Final[st.SearchStrategy[str]] = st.sampled_from(
    _WORKFLOW_TYPES
)


# Existing-branch set strategy. Includes the bare ``ai/{issue_key}``
# slot only some of the time so the iter==1 fallback branch is
# exercised. Other entries are unrelated branches the formatter must
# ignore (they only matter on iter==1, and only the exact bare slot).
@st.composite
def _existing_branch_sets(
    draw: st.DrawFn, issue_key: str
) -> frozenset[str]:
    extras = draw(
        st.lists(
            st.sampled_from(
                (
                    "main",
                    "develop",
                    "release/2024-12",
                    "feature/foo",
                    "ai/OTHER-1",
                    "ai/OTHER-1-iter2",
                )
            ),
            min_size=0,
            max_size=4,
            unique=True,
        )
    )
    include_bare = draw(st.booleans())
    items = list(extras)
    if include_bare:
        items.append(f"ai/{issue_key}")
    return frozenset(items)


# ---------------------------------------------------------------------------
# invariant(a) — compute_branch_name 
# ---------------------------------------------------------------------------


class TestComputeBranchName:
    """invariant(a) — collision-free + shape + determinism."""

    @settings(max_examples=100, deadline=None)
    @given(
        issue_key=_issue_keys(),
        iteration=_ITERATIONS,
        data=st.data(),
    )
    def test_p1_iter1_bare_slot_free_is_collision_free(
        self,
        issue_key: str,
        iteration: int,
        data: st.DataObject,
    ) -> None:
        """P1: when the function returns the bare branch the slot is free.

 For iter == 1, the function returns ``ai/{issue_key}`` *iff* the
 bare slot is not in ``existing_branches``. We therefore assert
 the post-condition: when the output equals the bare form, the
 bare form was guaranteed absent from the input set, i.e. the
 result never collides with an existing branch.

 For iter >= 2 the function deterministically returns the
 iter-suffixed form regardless of *existing* (the workflow
 caller monotonically increments iter, so the result is fresh by
 construction).


 """
        existing = data.draw(_existing_branch_sets(issue_key))

        result = compute_branch_name(issue_key, iteration, existing)

        bare = f"ai/{issue_key}"
        if result == bare:
            # The bare branch can only be returned when the slot was
            # free — anything else would be a collision.
            assert bare not in existing
        else:
            # Otherwise we must be on the iter-suffixed branch.
            assert result == f"{bare}-iter{iteration}"

    @settings(max_examples=100, deadline=None)
    @given(
        issue_key=_issue_keys(),
        iteration=_ITERATIONS,
        data=st.data(),
    )
    def test_p2_output_shape_is_one_of_two_forms(
        self,
        issue_key: str,
        iteration: int,
        data: st.DataObject,
    ) -> None:
        """P2: output is always either the bare form or the iter form.

 The bare form is selected **iff** ``iter == 1`` AND the bare
 slot is free; every other case lands on the iter-suffixed form.


 """
        existing = data.draw(_existing_branch_sets(issue_key))
        result = compute_branch_name(issue_key, iteration, existing)

        bare = f"ai/{issue_key}"
        iter_form = f"{bare}-iter{iteration}"

        # Output is exactly one of the two documented shapes.
        assert result in (bare, iter_form)
        # The two shapes also match the pinned regexes.
        if result == bare:
            assert _BARE_BRANCH_RE.match(result) is not None
        else:
            assert _ITER_BRANCH_RE.match(result) is not None

        # Bare-form selection rule: iff iter == 1 AND bare not in existing.
        bare_selected_expected = iteration == 1 and bare not in existing
        assert (result == bare) is bare_selected_expected

    @settings(max_examples=100, deadline=None)
    @given(
        issue_key=_issue_keys(),
        iteration=_ITERATIONS,
        data=st.data(),
    )
    def test_p3_deterministic_and_iterable_shape_invariant(
        self,
        issue_key: str,
        iteration: int,
        data: st.DataObject,
    ) -> None:
        """P3: pure + iterable shape independent + post-call mutation safe.

 Two consecutive calls with the same triple return identical
 strings. Passing *existing* as a list, tuple, set or frozenset
 does not change the result. Mutating a mutable iterable after
 the call cannot retro-actively change the returned value
 (because the function consumes the iterable eagerly when iter
 == 1 and skips it otherwise).


 """
        existing_set = data.draw(_existing_branch_sets(issue_key))

        # Same input, two calls → same output.
        first = compute_branch_name(issue_key, iteration, existing_set)
        second = compute_branch_name(issue_key, iteration, existing_set)
        assert first == second

        # Different concrete iterable shapes carry the same set
        # membership → the function must return the same answer.
        as_list = list(existing_set)
        as_tuple = tuple(existing_set)
        as_frozen = frozenset(existing_set)
        as_mutable_set: set[str] = set(existing_set)

        assert (
            compute_branch_name(issue_key, iteration, as_list)
            == first
        )
        assert (
            compute_branch_name(issue_key, iteration, as_tuple)
            == first
        )
        assert (
            compute_branch_name(issue_key, iteration, as_frozen)
            == first
        )
        result_mutable = compute_branch_name(
            issue_key, iteration, as_mutable_set
        )
        assert result_mutable == first

        # Mutating the source iterable AFTER the call cannot change the
        # already-returned string — the snapshot is captured eagerly.
        as_mutable_set.add(f"ai/{issue_key}")
        assert result_mutable == first

    def test_invalid_issue_key_raises(self) -> None:
        """Non-shape issue keys raise:class:`InvalidIssueKeyError`.

 Concrete regression — keeps the invariant anchored to the
 validator contract.


 """
        with pytest.raises(InvalidIssueKeyError):
            compute_branch_name("not-an-issue-key", 1, [])


# ---------------------------------------------------------------------------
# invariant(b) — format_commit_message 
# ---------------------------------------------------------------------------


class TestFormatCommitMessage:
    """invariant(b) — ``[bot]`` prefix + ``Co-authored-by`` footer."""

    @settings(max_examples=100, deadline=None)
    @given(
        message=_MESSAGES,
        issue_key=_issue_keys(),
        iteration=_ITERATIONS,
        bot_email=_bot_emails(),
    )
    def test_p4_starts_with_bot_prefix(
        self,
        message: str,
        issue_key: str,
        iteration: int,
        bot_email: str,
    ) -> None:
        """P4: output starts with ``"[bot] "`` (note trailing space).


 """
        result = format_commit_message(message, issue_key, iteration, bot_email)

        assert result.startswith(f"{BOT_COMMIT_PREFIX} ")

    @settings(max_examples=100, deadline=None)
    @given(
        message=_MESSAGES,
        issue_key=_issue_keys(),
        iteration=_ITERATIONS,
        bot_email=_bot_emails(),
    )
    def test_p5_ends_with_co_authored_by_trailer(
        self,
        message: str,
        issue_key: str,
        iteration: int,
        bot_email: str,
    ) -> None:
        """P5: trailer line is the last line and is preceded by a blank line.

 ``Co-authored-by: ai-bot <{bot_email}>`` MUST be the last line
 of the output, separated from the body by exactly one empty
 line so Git's trailer parser recognises it.


 """
        result = format_commit_message(message, issue_key, iteration, bot_email)
        lines = result.split("\n")

        # Last line is the trailer.
        last = lines[-1]
        assert _TRAILER_RE.match(last) is not None, (
            f"Trailer line {last!r} does not match the Co-authored-by shape"
        )
        assert last == f"Co-authored-by: ai-bot <{bot_email}>"

        # Penultimate line must be empty (the blank-line separator).
        assert len(lines) >= 3
        assert lines[-2] == ""

        # And the line before that — the last line of the body — must
        # itself be non-empty, otherwise the body→trailer separator
        # would be ambiguous (multiple blank lines).
        assert lines[-3] != ""

    @settings(max_examples=100, deadline=None)
    @given(
        message=_MESSAGES,
        issue_key=_issue_keys(),
        iteration=_ITERATIONS,
        bot_email=_bot_emails(),
    )
    def test_p6_body_is_message_rstripped(
        self,
        message: str,
        issue_key: str,
        iteration: int,
        bot_email: str,
    ) -> None:
        """P6: body equals ``"[bot] " + message.rstrip``.

 The function only strips trailing whitespace from *message* so
 the inserted blank line is unambiguous; internal structure is
 preserved verbatim. The function does **not** rewrite, truncate
 or re-flow the LLM body.


 """
        result = format_commit_message(message, issue_key, iteration, bot_email)

        # Split off the trailer block (``\n\nCo-authored-by:...``).
        trailer = f"\n\nCo-authored-by: ai-bot <{bot_email}>"
        assert result.endswith(trailer)
        body = result[: -len(trailer)]

        expected_body = f"{BOT_COMMIT_PREFIX} {message.rstrip()}"
        assert body == expected_body

    def test_concrete_example_pins_shape(self) -> None:
        """Concrete regression for P4+P5+P6.


 """
        result = format_commit_message(
            "fix payment retry logic",
            "PAY-4211",
            1,
            "ai-bot@company.com",
        )
        assert result == (
            "[bot] fix payment retry logic\n\n"
            "Co-authored-by: ai-bot <ai-bot@company.com>"
        )


# ---------------------------------------------------------------------------
# invariant(c) — select_pr_create_tool 
# ---------------------------------------------------------------------------


class TestSelectPrCreateTool:
    """invariant(c) — Bitbucket Cloud ↔ Data Center parity (T9)."""

    @settings(max_examples=100, deadline=None)
    @given(deployment=st.sampled_from(("cloud", "server")))
    def test_p7_parity_mapping_is_exhaustive(
        self, deployment: str
    ) -> None:
        """P7: ``cloud → cloud_tool``, ``server → dc_tool`` — exhaustive.


 """
        tool = select_pr_create_tool(deployment)  # type: ignore[arg-type]

        if deployment == "cloud":
            assert tool == BITBUCKET_CREATE_PR_CLOUD
            assert tool == "bitbucket_create_pull_request_cloud"
        else:
            assert tool == BITBUCKET_CREATE_PR_DC
            assert tool == "bitbucket_create_pull_request_dc"

    @settings(max_examples=100, deadline=None)
    @given(
        bad_deployment=st.text(min_size=0, max_size=12).filter(
            lambda s: s not in ("cloud", "server")
        ),
    )
    def test_p7_unknown_deployment_raises_keyerror(
        self, bad_deployment: str
    ) -> None:
        """Any value outside ``{"cloud", "server"}`` raises ``KeyError``.

 The router intentionally has **no** default branch so a
 misconfigured ``departments.json`` surfaces at signal-dispatch
 time rather than silently routing a Cloud-style PR call to a
 Data Center instance (or vice versa). This test pins the
 fail-fast behaviour against the entire complement of valid
 inputs.


 """
        with pytest.raises(KeyError):
            select_pr_create_tool(bad_deployment)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# invariant(d) — route_by_branch_pattern 
# ---------------------------------------------------------------------------


class TestRouteByBranchPattern:
    """invariant(d) — hotfix deny + release allowlist + ai/ open default."""

    @settings(max_examples=100, deadline=None)
    @given(
        issue_key=_issue_keys(),
        candidate=_WORKFLOW_TYPE_STRATEGY,
    )
    def test_p8_hotfix_denies_commit_only(
        self, issue_key: str, candidate: str
    ) -> None:
        """P8: ``hotfix/*`` denies ``code_change_commit_only``.

 For any other workflow type the rule is silent — the matching
 glob still wins (first-match-wins), but the decision is
 ``allowed=True`` with the passthrough audit reason. This pins
 the the operational rule that PR open is mandatory on hotfix branches
 without accidentally widening the rule to deny unrelated
 workflows.


 """
        branch = f"hotfix/{issue_key}"
        decision = route_by_branch_pattern(
            branch, candidate, DEFAULT_BRANCH_PATTERN_RULES
        )

        if candidate == "code_change_commit_only":
            assert decision.allowed is False
            assert decision.matched_glob == "hotfix/*"
            assert decision.reason == DEFAULT_HOTFIX_RULE.reason
        else:
            # Hotfix rule is in deny mode; non-deny-set workflow types
            # are passed through as ``allowed=True`` with the rule's
            # glob recorded for audit.
            assert decision.allowed is True
            assert decision.matched_glob == "hotfix/*"

    @settings(max_examples=100, deadline=None)
    @given(
        issue_key=_issue_keys(),
        candidate=_WORKFLOW_TYPE_STRATEGY,
    )
    def test_p9_release_only_allows_pr_review_and_doc_update(
        self, issue_key: str, candidate: str
    ) -> None:
        """P9: ``release/*`` allows only ``pr_review`` + ``confluence_doc_update``.

 Every other workflow type is denied with the rule's audit
 reason ``release_branch_restricted``.


 """
        branch = f"release/{issue_key}"
        decision = route_by_branch_pattern(
            branch, candidate, DEFAULT_BRANCH_PATTERN_RULES
        )

        if candidate in _RELEASE_ALLOWED:
            assert decision.allowed is True
            assert decision.matched_glob == "release/*"
            assert decision.reason == "matched_allowlist"
        else:
            assert decision.allowed is False
            assert decision.matched_glob == "release/*"
            assert decision.reason == DEFAULT_RELEASE_RULE.reason

    @settings(max_examples=100, deadline=None)
    @given(
        issue_key=_issue_keys(),
        iteration=_ITERATIONS,
        candidate=_WORKFLOW_TYPE_STRATEGY,
        use_iter_form=st.booleans(),
    )
    def test_p10_ai_branches_are_always_allowed(
        self,
        issue_key: str,
        iteration: int,
        candidate: str,
        use_iter_form: bool,
    ) -> None:
        """P10: ``ai/*`` branches always allow (no default rule matches).

 Neither ``hotfix/*`` nor ``release/*`` matches the ``ai/``
 prefix, so:func:`route_by_branch_pattern` falls through to
 the open default — ``allowed=True``, reason
 ``no_rule_matched``, ``matched_glob=None``. This holds for the
 bare ``ai/{issue_key}`` slot and the iter-suffixed form alike.


 """
        branch = (
            f"ai/{issue_key}-iter{iteration}"
            if use_iter_form
            else f"ai/{issue_key}"
        )
        decision = route_by_branch_pattern(
            branch, candidate, DEFAULT_BRANCH_PATTERN_RULES
        )

        assert decision == RouteDecision(
            allowed=True, reason="no_rule_matched", matched_glob=None
        )

    @settings(max_examples=100, deadline=None)
    @given(
        branch=_branches(),
        candidate=_WORKFLOW_TYPE_STRATEGY,
    )
    def test_p11_decision_is_deterministic(
        self, branch: str, candidate: str
    ) -> None:
        """P11: same ``(branch, candidate, rules)`` → same decision.


 """
        first = route_by_branch_pattern(
            branch, candidate, DEFAULT_BRANCH_PATTERN_RULES
        )
        second = route_by_branch_pattern(
            branch, candidate, DEFAULT_BRANCH_PATTERN_RULES
        )

        assert first == second
        # And the result is a frozen dataclass (immutable) — two
        # decisions with the same fields are equal value-wise.
        assert isinstance(first, RouteDecision)

    @settings(max_examples=100, deadline=None)
    @given(
        branch=_branches(),
        candidate=_WORKFLOW_TYPE_STRATEGY,
    )
    def test_empty_rules_is_open_default(
        self, branch: str, candidate: str
    ) -> None:
        """An empty rule list always yields ``no_rule_matched`` (open default).

 Departments that omit ``branch_pattern_rules`` (the schema
 default per) keep working unchanged — every branch +
 workflow combination is permitted.


 """
        decision = route_by_branch_pattern(branch, candidate, [])
        assert decision.allowed is True
        assert decision.reason == "no_rule_matched"
        assert decision.matched_glob is None

    def test_concrete_examples_match_design_doc(self) -> None:
        """Concrete regressions from design.md and module docstring.


 """
        # hotfix/* + commit_only → denied
        d = route_by_branch_pattern(
            "hotfix/PAY-1",
            "code_change_commit_only",
            DEFAULT_BRANCH_PATTERN_RULES,
        )
        assert d.allowed is False
        assert d.matched_glob == "hotfix/*"

        # release/* + pr_review → allowed
        d = route_by_branch_pattern(
            "release/2025-04",
            "pr_review",
            DEFAULT_BRANCH_PATTERN_RULES,
        )
        assert d.allowed is True
        assert d.matched_glob == "release/*"

        # release/* + code_change_with_test → denied
        d = route_by_branch_pattern(
            "release/2025-04",
            "code_change_with_test",
            DEFAULT_BRANCH_PATTERN_RULES,
        )
        assert d.allowed is False
        assert d.matched_glob == "release/*"

        # feature/* + anything → no rule → allowed
        d = route_by_branch_pattern(
            "feature/AUTH-1",
            "code_change_with_test",
            DEFAULT_BRANCH_PATTERN_RULES,
        )
        assert d.allowed is True
        assert d.matched_glob is None

    def test_custom_rule_is_evaluated_in_order(self) -> None:
        """A custom prefix rule wins over the default rules when listed first.

 invariant(d) only fixes the default-rule semantics, but the
 first-match-wins ordering documented in:func:`route_by_branch_pattern` is part of the same contract —
 keeping a small concrete regression here pins the iteration
 order so a future refactor cannot silently re-order the rule
 scan.


 """
        custom = BranchPatternRule(
            glob="hotfix/special-*",
            allowed_workflow_types=frozenset({"code_change_commit_only"}),
            reason="hotfix_special_allowlist",
        )
        rules = (custom,) + DEFAULT_BRANCH_PATTERN_RULES

        # The custom allow-list rule wins for hotfix/special-* even
        # though the default hotfix deny rule would otherwise match.
        d = route_by_branch_pattern(
            "hotfix/special-PAY-1",
            "code_change_commit_only",
            rules,
        )
        assert d.allowed is True
        assert d.matched_glob == "hotfix/special-*"
        assert d.reason == "matched_allowlist"

        # An ordinary hotfix branch still lands on the default rule.
        d = route_by_branch_pattern(
            "hotfix/PAY-1",
            "code_change_commit_only",
            rules,
        )
        assert d.allowed is False
        assert d.matched_glob == "hotfix/*"
