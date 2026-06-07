"""Workflow-type → required capability set mapping and capability gate.

This module is the **single source of truth** for the workflow-type →
capability mapping. Other modules import these
symbols rather than redefining them. The constant
:data:`WORKFLOW_TYPE_CAPABILITIES` is wrapped in
:class:`types.MappingProxyType` so it cannot be mutated at runtime
.

Capability vocabulary (closed set):

* ``jira_read``, ``jira_write``
* ``bitbucket_read``, ``bitbucket_write``
* ``confluence_read``, ``confluence_write``
* ``execution`` - at least one ``SSH_HOST`` env variable is defined
  (canonical) - ``SSH_HOST_1`` is accepted as a deprecated alias for
  backwards compatibility
* ``web_search`` - both ``Department.web_search_enabled`` and
  ``FIRECRAWL_ENABLED == "true"``

Public API:

* :data:`WORKFLOW_TYPE_CAPABILITIES` - immutable mapping (13 entries -
  includes ``script_execute``, ``research_publish_confluence``,
  and ``research_summary_jira`` to match
  ``task_analyzer.VALID_WORKFLOW_TYPES``)
* :func:`derive_capabilities` - pure function (no I/O)
* :func:`gate` - pure function returning :class:`GateDecision`
* :class:`GateDecision` - frozen dataclass with ``allowed`` and ``missing``
* :class:`HasCredential` - structural protocol for bot entries
* :class:`SupportsBot`, :class:`SupportsDepartment` - structural protocols
  used by ``derive_capabilities`` (duck-typed; the concrete ``Department``
  loader is intentionally not required here)

"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Mapping, Protocol, runtime_checkable

__all__ = [
    "WORKFLOW_TYPE_CAPABILITIES",
    "GateDecision",
    "HasCredential",
    "SupportsBot",
    "SupportsDepartment",
    "derive_capabilities",
    "gate",
    "required_capabilities",
    "missing_capabilities",
    "has_jira_credential",
]


# ---------------------------------------------------------------------------
# Workflow-type → required capability set (single source of truth)
# ---------------------------------------------------------------------------

#: Workflow type → frozenset of required capabilities. Mirrors
#: the workflow capability table.
#: Wrapped in ``MappingProxyType`` so callers cannot mutate the shared
#: dictionary at runtime.
WORKFLOW_TYPE_CAPABILITIES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "code_change_with_test": frozenset(
            {
                "jira_read",
                "jira_write",
                "bitbucket_read",
                "bitbucket_write",
                "execution",
            }
        ),
        "code_change_commit_only": frozenset(
            {
                "jira_read",
                "jira_write",
                "bitbucket_read",
                "bitbucket_write",
            }
        ),
        "pr_review": frozenset(
            {
                "jira_read",
                "jira_write",
                "bitbucket_read",
            }
        ),
        "confluence_doc_create": frozenset(
            {
                "jira_read",
                "jira_write",
                "confluence_read",
                "confluence_write",
            }
        ),
        "confluence_doc_update": frozenset(
            {
                "jira_read",
                "jira_write",
                "confluence_read",
                "confluence_write",
            }
        ),
        "research_basic": frozenset(
            {
                "jira_read",
                "jira_write",
            }
        ),
        "research_with_web": frozenset(
            {
                "jira_read",
                "jira_write",
                "web_search",
            }
        ),
        "multi_step": frozenset(
            {
                "jira_read",
                "jira_write",
            }
        ),
        "noop_test": frozenset(
            {
                "jira_read",
            }
        ),
        "remote_ssh_test_only": frozenset(
            {
                "jira_read",
                "execution",
            }
        ),
        # The three entries below were previously in
        # ``task_analyzer.VALID_WORKFLOW_TYPES`` (13 types)
        # but missing from this table (10 types). When the analyzer
        # produced them, ``required_capabilities()`` raised KeyError and
        # the gateway denied the task as ``unknown_workflow_type`` even
        # though the prompt advertised them.
        "script_execute": frozenset(
            {
                "jira_read",
                "jira_write",
                "execution",
            }
        ),
        "research_publish_confluence": frozenset(
            {
                "jira_read",
                "jira_write",
                "confluence_read",
                "confluence_write",
                "web_search",
            }
        ),
        "research_summary_jira": frozenset(
            {
                "jira_read",
                "jira_write",
            }
        ),
    }
)


# ---------------------------------------------------------------------------
# Structural protocols for duck typing
# ---------------------------------------------------------------------------


@runtime_checkable
class HasCredential(Protocol):
    """A bot entry that can report whether it has a credential bound."""

    def has_credential(self) -> bool:  # pragma: no cover - protocol
        ...


class SupportsBot(Protocol):
    """The bot section of a department (jira/bitbucket/confluence)."""

    jira: HasCredential | None
    bitbucket: HasCredential | None
    confluence: HasCredential | None


class SupportsDepartment(Protocol):
    """Minimal structural shape required by :func:`derive_capabilities`.

    The full ``Department`` dataclass / loader is not required here; this
    Protocol lets us write the pure capability logic without depending on
    a concrete schema and keeps the function trivially unit-testable.
    """

    web_search_enabled: bool
    bot: SupportsBot


# ---------------------------------------------------------------------------
# GateDecision - return type of :func:`gate`
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Result of a capability-gate evaluation.

    Attributes:
        allowed: ``True`` iff the department satisfies every required
            capability for the workflow type.
        missing: The set of capability strings that are required by the
            workflow type but absent from the department's derived
            capability set. Empty when ``allowed`` is ``True``.
    """

    allowed: bool
    missing: frozenset[str]


# ---------------------------------------------------------------------------
# derive_capabilities - pure function
# ---------------------------------------------------------------------------


def derive_capabilities(
    dept: SupportsDepartment,
    env: Mapping[str, str],
) -> frozenset[str]:
    """Derive the capability frozenset for *dept* given the environment.

    The function is **pure**: it performs no network or filesystem I/O.
    All input must be supplied via ``dept`` and ``env``.

    Rules:

    1. ``bot.jira`` has a credential → ``jira_read``, ``jira_write``.
    2. ``bot.bitbucket`` has a credential → ``bitbucket_read``,
       ``bitbucket_write``.
    3. ``bot.confluence`` has a credential → ``confluence_read``,
       ``confluence_write``.
    4. ``SSH_HOST`` (canonical) or any ``SSH_HOST_<n>`` (deprecated
       legacy alias) key in *env* → ``execution``. Single-runner
       canonical contract: the platform runs **exactly one** SSH host
       shared by all departments under ``RUNNER_BASE_PATH``; per-dept
       host overrides are not supported.
       ``SSH_RUNNER_DEPT_PINNING_ENABLED`` is *not* consulted here; the
       flag is now a deprecated no-op under the
       single-runner contract.)
    5. ``dept.web_search_enabled`` and ``env["FIRECRAWL_ENABLED"] == "true"``
       → ``web_search``.

    Args:
        dept: Object exposing :class:`SupportsDepartment` shape.
        env: Mapping of environment variables (typically ``os.environ``
            or a test fixture). Only the keys ``FIRECRAWL_ENABLED`` and
            those starting with ``SSH_HOST_`` are consulted.

    Returns:
        A ``frozenset[str]`` of capabilities the department holds. Never
        contains capability strings outside the closed vocabulary
        documented at the module level.

    Examples::

        >>> from dataclasses import dataclass
        >>> @dataclass
        ... class _Cred:
        ...     present: bool = True
        ...     def has_credential(self) -> bool:
        ...         return self.present
        >>> @dataclass
        ... class _Bot:
        ...     jira: object = None
        ...     bitbucket: object = None
        ...     confluence: object = None
        >>> @dataclass
        ... class _Dept:
        ...     web_search_enabled: bool = False
        ...     bot: object = None
        >>> d = _Dept(web_search_enabled=False, bot=_Bot(jira=_Cred()))
        >>> sorted(derive_capabilities(d, {}))
        ['jira_read', 'jira_write']
    """
    caps: set[str] = set()

    bot = dept.bot

    jira_entry = getattr(bot, "jira", None)
    if jira_entry is not None and jira_entry.has_credential():
        caps.add("jira_read")
        caps.add("jira_write")

    bitbucket_entry = getattr(bot, "bitbucket", None)
    if bitbucket_entry is not None and bitbucket_entry.has_credential():
        caps.add("bitbucket_read")
        caps.add("bitbucket_write")

    confluence_entry = getattr(bot, "confluence", None)
    if confluence_entry is not None and confluence_entry.has_credential():
        caps.add("confluence_read")
        caps.add("confluence_write")

    # Execution is driven by admin-managed runner assignment. This pure
    # helper cannot query Postgres, so callers pass the assignment as
    # env-like flags. SSH_HOST remains a legacy fallback.
    runner_assigned = any(
        str(env.get(key, "")).strip().lower() in {"1", "true", "yes", "on"}
        for key in ("EXECUTION_RUNNER_ASSIGNED", "EXECUTION_RUNNER_AVAILABLE")
    )
    legacy_ssh = any(
        (key == "SSH_HOST" or key.startswith("SSH_HOST_"))
        and str(value).strip()
        for key, value in env.items()
    )
    if runner_assigned or legacy_ssh:
        caps.add("execution")

    # Web search requires both the dept opt-in and the global firecrawl flag.
    if dept.web_search_enabled and env.get("FIRECRAWL_ENABLED", "false") == "true":
        caps.add("web_search")

    return frozenset(caps)


# ---------------------------------------------------------------------------
# gate - pure function
# ---------------------------------------------------------------------------


def gate(
    workflow_type: str,
    dept: SupportsDepartment,
    env: Mapping[str, str],
) -> GateDecision:
    """Decide whether *dept* may start a workflow of *workflow_type*.

    Computes the set difference ``required - have`` and packages the
    answer into a :class:`GateDecision`. Pure function - no I/O
    .

    Args:
        workflow_type: Key into :data:`WORKFLOW_TYPE_CAPABILITIES`.
        dept: Department object (see :class:`SupportsDepartment`).
        env: Environment mapping (see :func:`derive_capabilities`).

    Returns:
        A :class:`GateDecision`. ``allowed=True`` iff every required
        capability is present; otherwise ``missing`` lists the absent
        capabilities and ``allowed`` is ``False``.

    Raises:
        KeyError: If *workflow_type* is not a recognised key in
            :data:`WORKFLOW_TYPE_CAPABILITIES`.
    """
    required = WORKFLOW_TYPE_CAPABILITIES[workflow_type]
    have = derive_capabilities(dept, env)
    missing = required - have
    return GateDecision(allowed=not missing, missing=frozenset(missing))


# ---------------------------------------------------------------------------
# Simple-name helpers
# ---------------------------------------------------------------------------
#
# The :data:`WORKFLOW_TYPE_CAPABILITIES` table uses the *split* capability
# vocabulary (``"jira_read"`` / ``"jira_write"`` / ``"bitbucket_read"``…)
# that mirrors the workflow capability table. Most call
# sites - :class:`AutomationWorkflow.run`, the automation-service webhook
# decision layer, and the integration tests - talk in the simpler service
# vocabulary (``"jira"`` / ``"bitbucket"`` / ``"confluence"`` / ``"execution"``
# / ``"web_search"``) because that is what departments register and what
# user-facing comments name. The helpers below collapse the split form to
# the simple form so callers can do straightforward set difference without
# having to re-implement the mapping at every site.
#
# The collapse is one-directional and lossless for the gate decision
# (`required - available`): if a workflow needs `jira_read` *or* `jira_write`
# the department only ever holds them as a pair (a Jira credential grants
# both), so collapsing both to `"jira"` preserves the gate semantics.

#: Split capability suffix → simple capability name. Used by
#: :func:`_collapse_capability` to fold the split vocabulary down to the
#: simple service vocabulary.
_SPLIT_TO_SIMPLE: Final[Mapping[str, str]] = {
    "jira_read": "jira",
    "jira_write": "jira",
    "bitbucket_read": "bitbucket",
    "bitbucket_write": "bitbucket",
    "confluence_read": "confluence",
    "confluence_write": "confluence",
}


def _collapse_capability(cap: str) -> str:
    """Collapse a split capability name (``"jira_read"``) to its simple form (``"jira"``).

    Capabilities that are already simple (``"execution"``, ``"web_search"``)
    pass through unchanged.
    """

    return _SPLIT_TO_SIMPLE.get(cap, cap)


def required_capabilities(workflow_type: str) -> frozenset[str]:
    """Return the simple-name capability set required by *workflow_type*.

    Looks up *workflow_type* in :data:`WORKFLOW_TYPE_CAPABILITIES` and
    folds the split-vocabulary entries (``"jira_read"`` / ``"jira_write"``
    / ``"bitbucket_read"`` / ``"bitbucket_write"`` /
    ``"confluence_read"`` / ``"confluence_write"``) down to their simple
    service names so that callers can compare directly against the simple
    capability strings carried in :class:`AutomationInput.available_capabilities`
    and produced by ``resolve_dept_capabilities`` in the automation-service.

    Parameters
    ----------
    workflow_type:
        Key into :data:`WORKFLOW_TYPE_CAPABILITIES`.

    Returns
    -------
    frozenset[str]
        The required capabilities expressed in the simple vocabulary
        (``"jira"``, ``"bitbucket"``, ``"confluence"``, ``"execution"``,
        ``"web_search"``).

    Raises
    ------
    KeyError
        If *workflow_type* is not a key of
        :data:`WORKFLOW_TYPE_CAPABILITIES`. ``"multi_step"`` and unknown
        workflow types raise here by design - callers must handle the
        exception (see the workflow-type guard in
        :class:`AutomationWorkflow.run`).

    Examples
    --------
    >>> sorted(required_capabilities("code_change_with_test"))
    ['bitbucket', 'execution', 'jira']
    >>> sorted(required_capabilities("pr_review"))
    ['bitbucket', 'jira']
    >>> required_capabilities("unknown_workflow")
    Traceback (most recent call last):
        ...
    KeyError: 'unknown_workflow'
    """

    split = WORKFLOW_TYPE_CAPABILITIES[workflow_type]
    return frozenset(_collapse_capability(c) for c in split)


def missing_capabilities(
    required: frozenset[str],
    available: "frozenset[str] | set[str]",
) -> set[str]:
    """Return the capabilities that are *required* but not in *available*.

    Pure set-difference helper. Both inputs are expected in the simple
    vocabulary (use :func:`required_capabilities` to obtain *required*
    from a workflow type).

    Parameters
    ----------
    required:
        Capabilities the workflow type demands.
    available:
        Capabilities the department holds. Accepts either ``frozenset``
        or ``set`` for ergonomics.

    Returns
    -------
    set[str]
        Empty set when the gate is satisfied (``required ⊆ available``);
        otherwise the elements of *required* missing from *available*.

    Examples
    --------
    >>> missing_capabilities(frozenset({"jira", "bitbucket"}), {"jira"})
    {'bitbucket'}
    >>> missing_capabilities(frozenset({"jira"}), {"jira", "bitbucket"})
    set()
    """

    return set(required) - set(available)


def has_jira_credential(dept_caps: "frozenset[str] | set[str]") -> bool:
    """Return ``True`` iff *dept_caps* includes the ``"jira"`` capability.

    The webhook handler's Phase 1 gate. Cheapest possible check: a
    department without Jira can never act on a Jira-triggered event, so
    the handler short-circuits before starting any Temporal workflow.

    Parameters
    ----------
    dept_caps:
        Department capability set in the simple vocabulary.

    Returns
    -------
    bool
        ``True`` when ``"jira"`` is present, otherwise ``False``.

    Examples
    --------
    >>> has_jira_credential({"jira", "execution"})
    True
    >>> has_jira_credential({"bitbucket"})
    False
    """

    return "jira" in dept_caps
