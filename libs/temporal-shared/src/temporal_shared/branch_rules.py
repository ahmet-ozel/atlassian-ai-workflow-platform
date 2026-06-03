"""Branch-pattern routing rules — pure deny/allow gate for code-change flows.

This module is the **single source of truth** for the branch-pattern
routing function.

Public API
----------
* :class:`BranchPatternRule` — frozen dataclass mirroring one entry of
  the per-department ``departments.json.branch_pattern_rules`` array.
* :class:`RouteDecision` — frozen dataclass with an ``allowed`` flag and
  a human-readable ``reason`` (audit action).
* :func:`route_by_branch_pattern` — pure function returning a
  :class:`RouteDecision` for a ``(branch_name, candidate_workflow_type,
  rules)`` triple.
* :data:`DEFAULT_HOTFIX_RULE` — pinned default rule that denies
  ``code_change_commit_only`` on ``hotfix/*`` branches (PR open
  mandatory).
* :data:`DEFAULT_RELEASE_RULE` — pinned default rule that allows only
  ``pr_review`` and ``confluence_doc_update`` on ``release/*``
  branches.
* :data:`DEFAULT_BRANCH_PATTERN_RULES` — the two defaults above as an
  immutable tuple so the workflow_type router can load them when a
  department's config omits ``branch_pattern_rules``.

Semantics
-----------------------------------------------

The function applies the rules **in order** and returns the **first
match** as the decision. ``glob`` is interpreted with the standard
:func:`fnmatch.fnmatchcase` semantics (case-sensitive — branch names
are case-sensitive in Git).

Each rule supports two equivalent ways of expressing the deny/allow
contract:

* ``denied_workflow_types`` — set of workflow types that are *denied*
  on branches matching ``glob``. All other workflow types pass through
  this rule untouched.
* ``allowed_workflow_types`` — set of workflow types that are *allowed*
  on branches matching ``glob``. Any workflow type **outside** this set
  is denied with the rule's ``reason``.

A rule may carry **either** of these (but not both); a rule with both
or neither is rejected at construction time so the policy stays
unambiguous. When no rule's ``glob`` matches *branch_name*, the
function returns ``RouteDecision(allowed=True, reason="no_rule_matched")``
— the open default, so departments that have not configured
``branch_pattern_rules`` keep working unchanged.

Examples
--------------------------------------------------

* ``hotfix/*`` + ``code_change_commit_only`` → ``denied`` (PR open
  mandatory; commit-only would skip review on a hotfix branch).
* ``release/*`` + ``pr_review`` → ``allowed``.
* ``release/*`` + ``code_change_with_test`` → ``denied``.
* ``feature/foo`` + anything → ``allowed`` (no default rule matches).

Replay determinism
------------------

The function is **pure**: it performs only string globbing and set
membership tests. No I/O, no ``datetime`` / ``random`` / ``uuid``
calls, no mutable global state. Safe to call directly from inside
Temporal workflow code (replay
determinism).

"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Final, Iterable, Sequence

__all__ = [
    "BranchPatternRule",
    "RouteDecision",
    "route_by_branch_pattern",
    "DEFAULT_HOTFIX_RULE",
    "DEFAULT_RELEASE_RULE",
    "DEFAULT_BRANCH_PATTERN_RULES",
]


# ---------------------------------------------------------------------------
# RouteDecision — return type of :func:`route_by_branch_pattern`
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Result of a branch-pattern routing evaluation.

    Attributes
    ----------
    allowed:
        ``True`` iff the candidate workflow type is permitted on the
        given branch by the rule list.
    reason:
        A short, machine-readable token (snake_case) suitable for use
        as an audit action name. The audit reasons used by the design:

        * ``"no_rule_matched"`` — no rule's ``glob`` matched the branch
          (open default → ``allowed=True``).
        * ``"matched_allowlist"`` — a rule with
          ``allowed_workflow_types`` matched the branch and the
          candidate is in the allow set (``allowed=True``).
        * ``"branch_pattern_denied"`` — a rule with
          ``denied_workflow_types`` matched the branch and the
          candidate is in the deny set (``allowed=False``).
        * ``"branch_pattern_not_in_allowlist"`` — a rule with
          ``allowed_workflow_types`` matched the branch but the
          candidate is *outside* the allow set
          (``allowed=False``).
    matched_glob:
        The ``glob`` pattern of the matching rule, or ``None`` when no
        rule matched. Useful for audit context.
    """

    allowed: bool
    reason: str
    matched_glob: str | None = None


# ---------------------------------------------------------------------------
# BranchPatternRule — frozen dataclass mirroring a config entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BranchPatternRule:
    """One entry of ``Department.branch_pattern_rules``.

    A rule binds a branch-name glob to either a set of *denied* or a
    set of *allowed* workflow types, plus a short audit token. Exactly
    one of :attr:`denied_workflow_types` and :attr:`allowed_workflow_types`
    must be non-empty; both empty or both non-empty raises
    :class:`ValueError` at construction time so misconfigurations
    surface at boot rather than at runtime.

    Attributes
    ----------
    glob:
        :func:`fnmatch.fnmatchcase`-style branch name pattern. Examples:
        ``"hotfix/*"``, ``"release/*"``, ``"feature/AUTH-*"``. Matched
        case-sensitively against the branch name.
    denied_workflow_types:
        Frozenset of workflow types that are denied on branches
        matching :attr:`glob`. Workflow types outside this set are
        unaffected by this rule. Mutually exclusive with
        :attr:`allowed_workflow_types`.
    allowed_workflow_types:
        Frozenset of workflow types that are allowed on branches
        matching :attr:`glob`. Workflow types outside this set are
        denied with this rule's :attr:`reason`. Mutually exclusive with
        :attr:`denied_workflow_types`.
    reason:
        Short snake_case audit token attached to the resulting
        :class:`RouteDecision`. Defaults to one of
        ``"branch_pattern_denied"`` /
        ``"branch_pattern_not_in_allowlist"`` depending on which
        branch the rule takes; callers can override to surface a
        department-specific rationale (e.g. ``"hotfix_requires_pr"``).

    Raises
    ------
    ValueError
        If :attr:`glob` is empty, or if both / neither of
        :attr:`denied_workflow_types` and :attr:`allowed_workflow_types`
        are populated.
    """

    glob: str
    denied_workflow_types: frozenset[str] = field(default_factory=frozenset)
    allowed_workflow_types: frozenset[str] = field(default_factory=frozenset)
    reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.glob, str) or not self.glob:
            raise ValueError(
                f"BranchPatternRule.glob must be a non-empty string; got {self.glob!r}"
            )
        # Coerce iterables → frozenset so callers may pass a list/tuple
        # at construction time. Using object.__setattr__ because the
        # dataclass is frozen.
        if not isinstance(self.denied_workflow_types, frozenset):
            object.__setattr__(
                self,
                "denied_workflow_types",
                frozenset(self.denied_workflow_types),
            )
        if not isinstance(self.allowed_workflow_types, frozenset):
            object.__setattr__(
                self,
                "allowed_workflow_types",
                frozenset(self.allowed_workflow_types),
            )

        has_deny = bool(self.denied_workflow_types)
        has_allow = bool(self.allowed_workflow_types)
        if has_deny and has_allow:
            raise ValueError(
                "BranchPatternRule must specify either denied_workflow_types "
                "or allowed_workflow_types, not both; "
                f"glob={self.glob!r}, denied={sorted(self.denied_workflow_types)!r}, "
                f"allowed={sorted(self.allowed_workflow_types)!r}"
            )
        if not has_deny and not has_allow:
            raise ValueError(
                "BranchPatternRule must specify at least one workflow type in "
                "denied_workflow_types or allowed_workflow_types; "
                f"glob={self.glob!r}"
            )

        # Default reason depending on the rule mode.
        if not self.reason:
            object.__setattr__(
                self,
                "reason",
                "branch_pattern_denied"
                if has_deny
                else "branch_pattern_not_in_allowlist",
            )


# ---------------------------------------------------------------------------
# Default rules
# ---------------------------------------------------------------------------

#: Default deny rule for ``hotfix/*`` branches — ``code_change_commit_only``
#: skips PR review on a hotfix branch and is therefore denied; PR open is
#: mandatory for hotfixes.
DEFAULT_HOTFIX_RULE: Final[BranchPatternRule] = BranchPatternRule(
    glob="hotfix/*",
    denied_workflow_types=frozenset({"code_change_commit_only"}),
    reason="hotfix_requires_pr",
)

#: Default allowlist rule for ``release/*`` branches — only ``pr_review``
#: and ``confluence_doc_update`` are permitted; everything else (commit,
#: PR creation, research, ssh test, etc.) is denied.
DEFAULT_RELEASE_RULE: Final[BranchPatternRule] = BranchPatternRule(
    glob="release/*",
    allowed_workflow_types=frozenset({"pr_review", "confluence_doc_update"}),
    reason="release_branch_restricted",
)

#: Pinned defaults applied when a department omits ``branch_pattern_rules``.
#: Tuple ordering is significant (see :func:`route_by_branch_pattern`): the
#: hotfix rule is evaluated before the release rule, but neither glob
#: overlaps so the order is essentially cosmetic — kept stable for audit
#: trail reproducibility.
DEFAULT_BRANCH_PATTERN_RULES: Final[tuple[BranchPatternRule, ...]] = (
    DEFAULT_HOTFIX_RULE,
    DEFAULT_RELEASE_RULE,
)


# ---------------------------------------------------------------------------
# route_by_branch_pattern — pure routing function
# ---------------------------------------------------------------------------


def route_by_branch_pattern(
    branch_name: str,
    candidate_workflow_type: str,
    rules: Iterable[BranchPatternRule] | Sequence[BranchPatternRule],
) -> RouteDecision:
    """Decide whether *candidate_workflow_type* may run on *branch_name*.

    Pure function (no I/O). Iterates *rules* in order and returns the
    decision of the **first matching** rule. When no rule's ``glob``
    matches *branch_name*, returns ``RouteDecision(allowed=True,
    reason="no_rule_matched", matched_glob=None)``.

    Matching semantics:

    * Glob matching uses :func:`fnmatch.fnmatchcase` (case-sensitive,
      shell-style globbing — ``*``, ``?``, ``[…]``).
    * For a rule with :attr:`BranchPatternRule.denied_workflow_types`:
      if *candidate_workflow_type* is in the deny set →
      ``allowed=False``; otherwise ``allowed=True``. **Both branches
      stop iteration** — design treats the first matching glob as the
      authoritative scope, so a later, more permissive rule cannot
      "rescue" a deny.
    * For a rule with :attr:`BranchPatternRule.allowed_workflow_types`:
      if *candidate_workflow_type* is in the allow set →
      ``allowed=True``; otherwise ``allowed=False``. **Both branches
      stop iteration** — same first-match-wins semantics.

    Parameters
    ----------
    branch_name:
        The Git branch the workflow would run against (e.g.
        ``"hotfix/PAY-9999"``, ``"release/2025-04"``,
        ``"feature/AUTH-12"``).
    candidate_workflow_type:
        The workflow type the AutomationWorkflow router is about to
        dispatch (e.g. ``"code_change_commit_only"``, ``"pr_review"``).
        Must be one of the keys in
        :data:`temporal_shared.capabilities.WORKFLOW_TYPE_CAPABILITIES`,
        but this function does **not** validate the value — the
        capability gate handles unknown workflow types upstream.
    rules:
        Iterable of :class:`BranchPatternRule` from the department's
        ``departments.json.branch_pattern_rules`` field. May be empty;
        in that case the function returns the open default
        (``allowed=True``).

    Returns
    -------
    RouteDecision
        Frozen decision suitable for both the workflow and audit logs.

    Examples
    --------
    >>> rules = [DEFAULT_HOTFIX_RULE, DEFAULT_RELEASE_RULE]
    >>> route_by_branch_pattern("hotfix/PAY-1", "code_change_commit_only", rules)
    RouteDecision(allowed=False, reason='hotfix_requires_pr', matched_glob='hotfix/*')
    >>> route_by_branch_pattern("hotfix/PAY-1", "code_change_with_test", rules).allowed
    True
    >>> route_by_branch_pattern("release/2025-04", "pr_review", rules).allowed
    True
    >>> route_by_branch_pattern("release/2025-04", "code_change_with_test", rules)
    RouteDecision(allowed=False, reason='release_branch_restricted', matched_glob='release/*')
    >>> route_by_branch_pattern("feature/AUTH-1", "code_change_with_test", rules).allowed
    True
    >>> route_by_branch_pattern("feature/AUTH-1", "code_change_with_test", []).reason
    'no_rule_matched'
    """
    if not isinstance(branch_name, str) or not branch_name:
        # Treat empty / non-string branch as "no match" rather than
        # raising — workflow code passing an unbound branch should not
        # crash the gate, but the open default keeps the gate from
        # silently mis-routing. The capability gate already catches
        # missing-branch errors upstream.
        return RouteDecision(allowed=True, reason="no_rule_matched", matched_glob=None)

    for rule in rules:
        if not fnmatch.fnmatchcase(branch_name, rule.glob):
            continue

        # First match wins. Determine deny vs allow mode.
        if rule.denied_workflow_types:
            if candidate_workflow_type in rule.denied_workflow_types:
                return RouteDecision(
                    allowed=False,
                    reason=rule.reason,
                    matched_glob=rule.glob,
                )
            # Workflow type not in deny set → rule has nothing to say
            # about it; pass through with the rule glob noted for audit.
            return RouteDecision(
                allowed=True,
                reason="branch_pattern_passthrough",
                matched_glob=rule.glob,
            )

        # rule.allowed_workflow_types is non-empty (post-init guarantee).
        if candidate_workflow_type in rule.allowed_workflow_types:
            return RouteDecision(
                allowed=True,
                reason="matched_allowlist",
                matched_glob=rule.glob,
            )
        return RouteDecision(
            allowed=False,
            reason=rule.reason,
            matched_glob=rule.glob,
        )

    return RouteDecision(allowed=True, reason="no_rule_matched", matched_glob=None)
