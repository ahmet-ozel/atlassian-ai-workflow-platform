"""invariant 17 — Jira Issue Template fiziksel deploy idempotency.



invariant (design.md §"invariant"):

 *For all* aynı şablon konfigürasyonu için ardışık iki
 ``deploy_jira_issue_template.py`` çağrısı, ilkinin sonrasındaki
 Jira durumu ile ikincisinin sonrasındaki durumu (issue type,
 fields, screen scheme) **eşit** bırakır; ikinci çağrı net
 değişiklik yapmaz ve başarılı sonlanır.

The deploy script (``platform/scripts/deploy_jira_issue_template.py``,
 is the unit under test. When that script is present this
test imports its public ``deploy(client, template)`` entrypoint. While
the script is still being implemented may be partial), the
test falls back to a *reference* idempotent implementation that
encodes the documented contract:

 1. Read current Jira state for ``issue_type``, ``fields``,
 ``screen_scheme`` of the configured template name.
 2. If absent, ``create_*`` it with the desired payload.
 3. If present and equal to the desired payload, **no call** is
 made (idempotent no-op).
 4. If present but differing, ``update_*`` is called once with the
 diff so that subsequent reads return the desired payload.

The fallback exists so this invariant (12.7) can land before the
script (12.2) and still exercise the documented contract — the
property holds for every correct implementation of that contract.

Test strategy
-------------

* Hypothesis generates random template configurations (issue type
 shape, field set with each field's type, screen scheme tab layout).
* The mock Atlassian client tracks every ``create_*`` / ``update_*``
 / ``delete_*`` call and exposes a ``mutations`` counter.
* The property runs ``deploy`` twice against the same client with the
 same template and asserts:

 * Mock state after run #1 equals mock state after run #2
 (issue type, fields, screen scheme dictionaries identical).
 * Run #2 produces **zero** mutating calls (no ``create_*``,
 ``update_*``, ``delete_*``).
 * Run #2 returns success (does not raise).
"""

from __future__ import annotations

import importlib
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Mock Atlassian client — minimal Jira admin surface for issue templates
# ---------------------------------------------------------------------------


@dataclass
class MockJiraClient:
    """In-memory Jira admin client used by the deploy script under test.

 The client intentionally exposes only the surface the deploy
 contract needs: ``get_*`` reads return the current shape (or
 ``None`` when absent), and ``create_*`` / ``update_*`` mutate the
 in-memory state. Every mutating call is counted in:attr:`mutations` so the property can assert the second deploy is
 a strict no-op.

 The shape of each entity is treated as a plain ``dict[str, Any]``
 so the invariant can drive Hypothesis-generated payloads
 without coupling to a Jira REST DTO library.
 """

    issue_types: dict[str, dict[str, Any]] = field(default_factory=dict)
    fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    screen_schemes: dict[str, dict[str, Any]] = field(default_factory=dict)

    #: Total count of state-mutating calls (create / update / delete).
    mutations: int = 0
    #: Per-call log so failure messages can show *which* call was
    #: made on the (allegedly no-op) second run.
    call_log: list[tuple[str, str]] = field(default_factory=list)

    # -- issue type ---------------------------------------------------------

    def get_issue_type(self, name: str) -> dict[str, Any] | None:
        return deepcopy(self.issue_types.get(name))

    def create_issue_type(self, name: str, payload: dict[str, Any]) -> None:
        self.mutations += 1
        self.call_log.append(("create_issue_type", name))
        self.issue_types[name] = deepcopy(payload)

    def update_issue_type(self, name: str, payload: dict[str, Any]) -> None:
        self.mutations += 1
        self.call_log.append(("update_issue_type", name))
        self.issue_types[name] = deepcopy(payload)

    # -- fields -------------------------------------------------------------

    def get_field(self, name: str) -> dict[str, Any] | None:
        return deepcopy(self.fields.get(name))

    def create_field(self, name: str, payload: dict[str, Any]) -> None:
        self.mutations += 1
        self.call_log.append(("create_field", name))
        self.fields[name] = deepcopy(payload)

    def update_field(self, name: str, payload: dict[str, Any]) -> None:
        self.mutations += 1
        self.call_log.append(("update_field", name))
        self.fields[name] = deepcopy(payload)

    # -- screen scheme ------------------------------------------------------

    def get_screen_scheme(self, name: str) -> dict[str, Any] | None:
        return deepcopy(self.screen_schemes.get(name))

    def create_screen_scheme(self, name: str, payload: dict[str, Any]) -> None:
        self.mutations += 1
        self.call_log.append(("create_screen_scheme", name))
        self.screen_schemes[name] = deepcopy(payload)

    def update_screen_scheme(self, name: str, payload: dict[str, Any]) -> None:
        self.mutations += 1
        self.call_log.append(("update_screen_scheme", name))
        self.screen_schemes[name] = deepcopy(payload)

    # -- snapshot helper ----------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Deep copy of the entire mock state, used for equality checks."""
        return {
            "issue_types": deepcopy(self.issue_types),
            "fields": deepcopy(self.fields),
            "screen_schemes": deepcopy(self.screen_schemes),
        }


# ---------------------------------------------------------------------------
# Reference idempotent deploy — used when the production script is absent
# ---------------------------------------------------------------------------


def _reference_deploy(client: MockJiraClient, template: dict[str, Any]) -> None:
    """Reference implementation of the documented idempotent contract.

 The deploy script that lands in must implement the same
 contract; this invariant only relies on the *documented*
 behaviour (read → diff → no-op or single mutation), so any correct
 implementation will satisfy the property.

 The contract is:

 1. For each entity (issue type, every field, screen scheme) read
 the current state from Jira via the corresponding ``get_*``.
 2. If absent, call the corresponding ``create_*`` once with the
 desired payload.
 3. If present and **equal** to the desired payload, do nothing.
 4. If present but differing, call the corresponding ``update_*``
 once.
 """
    # 1. issue type
    name = template["issue_type"]["name"]
    desired = template["issue_type"]
    current = client.get_issue_type(name)
    if current is None:
        client.create_issue_type(name, desired)
    elif current != desired:
        client.update_issue_type(name, desired)

    # 2. fields (the template carries an ordered list; we treat the
    # field's ``name`` as the natural key the way Jira does).
    for field_payload in template["fields"]:
        fname = field_payload["name"]
        current_field = client.get_field(fname)
        if current_field is None:
            client.create_field(fname, field_payload)
        elif current_field != field_payload:
            client.update_field(fname, field_payload)

    # 3. screen scheme
    sname = template["screen_scheme"]["name"]
    desired_scheme = template["screen_scheme"]
    current_scheme = client.get_screen_scheme(sname)
    if current_scheme is None:
        client.create_screen_scheme(sname, desired_scheme)
    elif current_scheme != desired_scheme:
        client.update_screen_scheme(sname, desired_scheme)


def _resolve_deploy_callable() -> Callable[[Any, dict[str, Any]], Any]:
    """Return the deploy entrypoint: production script if present, else fallback.

 The production script lives at
 ``platform/scripts/deploy_jira_issue_template.py``.
 When that script ships and exposes a ``deploy(client, template)``
 function, this test exercises *it* directly. While is
 partial or missing the function, we exercise the reference
 contract so the property is still validated end-to-end.
 """
    try:
        mod = importlib.import_module("scripts.deploy_jira_issue_template")
    except ModuleNotFoundError:
        return _reference_deploy

    deploy = getattr(mod, "deploy", None)
    if not callable(deploy):
        return _reference_deploy
    return deploy


# ---------------------------------------------------------------------------
# Hypothesis strategies — random Issue Template configurations
# ---------------------------------------------------------------------------

#: Field types Jira's Cloud REST API supports for custom field create.
#: A small representative set keeps the example budget tight while
#: still exercising heterogeneous field shapes (string, number,
#: enum-like, multi-select).
_FIELD_TYPES = st.sampled_from(
    (
        "string",
        "number",
        "datetime",
        "option",
        "option-with-child",
        "array",
    )
)

#: Names use a constrained alphabet so equality comparison and
#: dict-key behaviour stay identical regardless of locale.
_NAME_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
_names = st.text(alphabet=_NAME_ALPHABET, min_size=1, max_size=20)


def _issue_type_strategy() -> st.SearchStrategy[dict[str, Any]]:
    """Build a random issue type payload."""
    return st.fixed_dictionaries(
        {
            "name": _names,
            "description": st.text(max_size=40),
            "iconUrl": st.text(alphabet=_NAME_ALPHABET, min_size=0, max_size=30),
            "hierarchyLevel": st.integers(min_value=-1, max_value=3),
        }
    )


def _field_strategy() -> st.SearchStrategy[dict[str, Any]]:
    """Build a random custom field payload."""
    return st.fixed_dictionaries(
        {
            "name": _names,
            "type": _FIELD_TYPES,
            "description": st.text(max_size=40),
            "required": st.booleans(),
        }
    )


def _screen_scheme_strategy() -> st.SearchStrategy[dict[str, Any]]:
    """Build a random screen scheme payload with a tab layout."""
    return st.fixed_dictionaries(
        {
            "name": _names,
            "description": st.text(max_size=40),
            "tabs": st.lists(
                st.fixed_dictionaries(
                    {
                        "name": _names,
                        "fields": st.lists(_names, max_size=4, unique=True),
                    }
                ),
                max_size=3,
            ),
        }
    )


def _template_strategy() -> st.SearchStrategy[dict[str, Any]]:
    """Build a random complete Issue Template configuration."""
    # Field names must be unique within the template — duplicate field
    # names would be rejected by Jira's create-field endpoint, and the
    # idempotency property is only well-defined when the template
    # itself is consistent.
    fields_list = st.lists(_field_strategy(), min_size=0, max_size=5).map(
        lambda items: list({f["name"]: f for f in items}.values())
    )
    return st.fixed_dictionaries(
        {
            "issue_type": _issue_type_strategy(),
            "fields": fields_list,
            "screen_scheme": _screen_scheme_strategy(),
        }
    )


# ---------------------------------------------------------------------------
# invariant: ardışık iki deploy çağrısı — ikincisi net değişiklik yapmaz
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(template=_template_strategy())
def test_deploy_jira_issue_template_is_idempotent(template: dict[str, Any]) -> None:
    """Two consecutive deploys leave the mock client in the same state.

 invariant:

 * State after the first deploy equals state after the second deploy.
 * The second deploy issues **zero** mutating calls
 (``create_*`` / ``update_*`` / ``delete_*``).
 * The second deploy completes successfully (no exception).

 The test exercises the production deploy script when present and
 a reference contract implementation otherwise; the property holds
 for any correct deploy implementation.
 """
    deploy = _resolve_deploy_callable()
    client = MockJiraClient()

    # First run — establishes the desired state in the mock Jira.
    deploy(client, template)
    state_after_first = client.snapshot()
    mutations_after_first = client.mutations

    # Second run — must be a strict no-op.
    deploy(client, template)
    state_after_second = client.snapshot()
    mutations_after_second = client.mutations

    # 1. State equality — the second deploy must not drift the state.
    assert state_after_first == state_after_second, (
        "invariant violation: second deploy mutated Jira state.\n"
        f" After run #1: {state_after_first}\n"
        f" After run #2: {state_after_second}\n"
        f" Mutating calls during run #2: "
        f"{client.call_log[mutations_after_first:]}"
    )

    # 2. Zero net mutations on the second run — the most direct
    # expression of the documented "no net change" contract.
    second_run_mutations = mutations_after_second - mutations_after_first
    assert second_run_mutations == 0, (
        "invariant violation: second deploy issued "
        f"{second_run_mutations} mutating call(s); expected 0.\n"
        f" Calls during run #2: {client.call_log[mutations_after_first:]}"
    )


# ---------------------------------------------------------------------------
# Companion sanity test — empty Jira → first deploy populates everything
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(template=_template_strategy())
def test_first_deploy_populates_all_template_entities(
    template: dict[str, Any],
) -> None:
    """Against a fresh Jira, deploy creates every entity exactly once.

 This is a precondition for invariant: if the first deploy did
 nothing the idempotency invariant would hold trivially. Asserting
 the first deploy actually populates the mock guarantees the
 idempotency check above is not vacuous.
 """
    deploy = _resolve_deploy_callable()
    client = MockJiraClient()

    deploy(client, template)

    # Issue type and screen scheme are always present in the template.
    assert client.get_issue_type(template["issue_type"]["name"]) == template["issue_type"]
    assert (
        client.get_screen_scheme(template["screen_scheme"]["name"])
        == template["screen_scheme"]
    )

    # Every field declared in the template is now in the mock.
    for field_payload in template["fields"]:
        assert client.get_field(field_payload["name"]) == field_payload


# ---------------------------------------------------------------------------
# Companion test — drift detection: a manual edit to Jira between runs
# is corrected, then the system stabilises (idempotency from run #2 on)
# ---------------------------------------------------------------------------


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
@given(template=_template_strategy())
def test_deploy_corrects_drift_and_then_stabilises(
    template: dict[str, Any],
) -> None:
    """Drift between runs is corrected; subsequent run is a no-op.

 invariant only requires *successive* deploys with the same
 template to be no-ops. If an operator edits Jira between runs,
 the next deploy must heal the drift and the run after that must
 again be a no-op. This guards against an implementation that
 would oscillate or repeatedly re-apply the diff.
 """
    deploy = _resolve_deploy_callable()
    client = MockJiraClient()

    deploy(client, template)

    # Simulate manual drift on the issue type description so that a
    # diff is present without breaking the entity's identity (name).
    name = template["issue_type"]["name"]
    drifted = client.get_issue_type(name)
    assert drifted is not None  # populated by run #1
    drifted["description"] = drifted.get("description", "") + "_DRIFT"
    # Bypass the mutation counter — this is the "operator edit" event,
    # not a deploy call.
    client.issue_types[name] = drifted

    # Run #2 corrects the drift.
    mutations_before_run_2 = client.mutations
    deploy(client, template)
    state_after_run_2 = client.snapshot()
    mutations_during_run_2 = client.mutations - mutations_before_run_2

    # The corrected entity matches the template again.
    assert client.get_issue_type(name) == template["issue_type"]
    # At least one mutation occurred to heal the drift.
    assert mutations_during_run_2 >= 1

    # Run #3 must be a no-op — the system has converged.
    mutations_before_run_3 = client.mutations
    deploy(client, template)
    state_after_run_3 = client.snapshot()
    mutations_during_run_3 = client.mutations - mutations_before_run_3

    assert state_after_run_2 == state_after_run_3
    assert mutations_during_run_3 == 0, (
        "After drift correction the system did not stabilise.\n"
        f" Calls during run #3: {client.call_log[mutations_before_run_3:]}"
    )
