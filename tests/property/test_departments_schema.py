"""Property tests for ``config/departments.schema.json`` accept/reject behaviour.

Validates: Requirements 7.2, 7.4, 7.5 (multi-service-scaffold spec) and
Requirements 3.1, 3.2, 3.3, 3.7, 3.8, 3.9, 6.1
(platform-mimari-foundation spec — Property 5: Departments schema and
``credential_ref`` formatı).

Property 8 (multi-service-scaffold): ``departments.schema.json`` validates
iff bot has at least one of ``{jira, bitbucket, confluence}`` and ``id``
is kebab-case.

Property 5 (platform-mimari-foundation, design §"Property 5"):

    For all hypothesis-generated ``Department`` variants:
    (a) every object that conforms to ``departments.schema.json`` loads;
        every non-conforming object fails boot.
    (b) ``bot.<service>.credential_ref`` matches
        ``^vault:[a-zA-Z0-9/_-]+$``.
    (c) the department object MUST NOT contain extra ``has_jira``,
        ``has_bitbucket``, or ``has_confluence`` flags; capabilities are
        derived solely from credential presence under ``bot``
        (Requirement 3.7 — "Credential var = Servis var").
    (d) ``departments.json`` MUST NOT contain two departments with the
        same ``id``; the second entry is rejected (Requirement 3.9).
        ``additionalProperties: false`` on every nested object enforces
        the schema's closed-world contract (Requirement 3.1).

The schema lives at ``<repo_root>/config/departments.schema.json`` and is
the single doc that both ``automation-service`` and ``admin-dashboard-api``
load at startup (Requirement 7.6 / 3.1). This module exercises the JSON
Schema 2020-12 dialect via ``jsonschema.Draft202012Validator`` and
asserts:

1. **Accept** — every department whose ``id`` matches
   ``^[a-z][a-z0-9-]{1,30}$`` AND whose ``bot`` has ≥1 of
   ``{jira, bitbucket, confluence}`` populated SHALL validate without
   raising.
2. **Reject** — every department whose ``id`` violates the regex OR whose
   ``bot`` is empty (``{}``) SHALL raise ``ValidationError``.
3. **Subset coverage** — all 7 non-empty subsets of
   ``{jira, bitbucket, confluence}`` are accepted; the empty subset is
   rejected.
4. **credential_ref regex** — every randomly drawn ``vault:<...>`` path
   that matches the schema's character class is accepted; every random
   non-conforming string is rejected.
5. **No has_* flags** — any department object carrying a top-level
   ``has_jira`` / ``has_bitbucket`` / ``has_confluence`` flag is rejected
   thanks to ``additionalProperties: false``.
6. **Duplicate id rejection** — at the loader level, two departments
   with the same ``id`` in a single document are rejected before the
   schema-validated payload is committed.

The composite generators (``valid_id``, ``invalid_id``,
``bot_object(min_services=N)``) follow design §6.4 of
``.kiro/specs/multi-service-scaffold/design.md`` literally.
"""

from __future__ import annotations

import json
import re
import string
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

# ---------------------------------------------------------------------------
# Schema loader (module-scoped to avoid re-reading on every Hypothesis draw)
# ---------------------------------------------------------------------------

#: Compiled regex matching the kebab-case ``id`` rule from the schema and
#: from Requirement 7.4 (no underscores, lowercase-letter-first, 2..31
#: characters total). Used to filter Hypothesis-generated invalid ids so
#: a defensive ``assume()`` can drop accidental collisions if the regex
#: engine produces ambiguous output.
VALID_ID_RE: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9-]{1,30}$")

#: The three Atlassian bot service slots referenced by Requirement 7.5.
BOT_SERVICES: tuple[str, ...] = ("jira", "bitbucket", "confluence")


@pytest.fixture(scope="module")
def departments_schema(repo_root: Path) -> dict:
    """Loads ``config/departments.schema.json`` once per test module."""

    path = repo_root / "config" / "departments.schema.json"
    assert path.is_file(), f"missing schema file: {path}"
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def validator(departments_schema: dict) -> Draft202012Validator:
    """Pre-built JSON Schema 2020-12 validator for the departments schema.

    Building the validator once per module shaves ~ms-per-draw off the
    Hypothesis loops below; ``Draft202012Validator`` instances are
    immutable so reuse is safe across hundreds of generated examples.
    """

    Draft202012Validator.check_schema(departments_schema)
    return Draft202012Validator(departments_schema)


# ---------------------------------------------------------------------------
# Hypothesis composite strategies (design §6.4)
# ---------------------------------------------------------------------------

# Matches the schema regex exactly. ``fullmatch=True`` ensures no leading
# or trailing junk slips through.
valid_id = st.from_regex(r"^[a-z][a-z0-9-]{1,30}$", fullmatch=True)

# Each branch produces a string that is guaranteed *not* to satisfy the
# valid regex. The five branches mirror design §6.4 verbatim.
invalid_id = st.one_of(
    # Empty string — fails the leading-char anchor.
    st.just(""),
    # Starts with an uppercase letter; the valid pattern requires [a-z].
    st.from_regex(r"^[A-Z].*", fullmatch=True),
    # Starts with a digit; the valid pattern requires [a-z] first.
    st.from_regex(r"^[0-9].*", fullmatch=True),
    # Contains an underscore; the valid character class is [a-z0-9-].
    st.from_regex(r".*_.*", fullmatch=True),
    # Too long: 32–64 characters of allowed alphabet (max valid length is 31).
    st.text(
        min_size=32,
        max_size=64,
        alphabet=string.ascii_lowercase + "-",
    ),
).filter(lambda s: not VALID_ID_RE.fullmatch(s))


@st.composite
def bot_object(draw: st.DrawFn, *, min_services: int = 0) -> dict[str, Any]:
    """Generate a ``bot`` sub-object with ``min_services``..3 entries.

    Each chosen service slot is populated with a ``credential_ref``
    matching the schema's vault-path regex
    (``^vault:[a-zA-Z0-9/_-]+$``) and the empty ``account_id`` /
    placeholder ``username`` shape used by ``config/departments.json``
    (Requirement 7.2 + MIMARI §2.5.4 item 4).
    """

    services = draw(
        st.lists(
            st.sampled_from(list(BOT_SERVICES)),
            min_size=min_services,
            max_size=3,
            unique=True,
        )
    )
    return {
        svc: {
            "credential_ref": f"vault:atlassian/x/{svc}",
            "account_id": "",
            "username": f"x-bot-{svc}@example.com",
        }
        for svc in services
    }


def _wrap(dept: dict[str, Any]) -> dict[str, Any]:
    """Wrap a single department dict as the top-level schema document."""

    return {"version": 1, "departments": [dept]}


def _make_dept(
    *, dept_id: str, bot: dict[str, Any], display_name: str = "Example Dept"
) -> dict[str, Any]:
    """Build a minimum-valid department, parameterised on ``id`` and ``bot``.

    Every other required field (``display_name``, ``jira_project_keys``,
    ``budget_caps``) is set to a fixed, schema-valid default so the only
    axes under test are the two named in Property 8.

    The ``budget_caps`` block was added by ``platform-mimari-ops`` task
    1.2 (made required at the schema level alongside the dept-level cost
    cap policy R5.5); we therefore inject a deterministic default here so
    the foundation property tests keep generating schema-conforming
    departments without coupling to the ops spec's budget semantics.
    """

    return {
        "id": dept_id,
        "display_name": display_name,
        "jira_project_keys": ["EXAMPLE"],
        "bot": bot,
        "budget_caps": {
            "weekly_usd_dept": 0,
            "weekly_usd_user": 0,
            "monthly_usd_dept": 0,
            "monthly_usd_user": 0,
        },
    }


# ---------------------------------------------------------------------------
# Property 8a — ACCEPT: valid id AND bot has ≥1 of {jira, bitbucket, confluence}
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(dept_id=valid_id, bot=bot_object(min_services=1))
def test_accept_when_id_valid_and_bot_nonempty(
    validator: Draft202012Validator, dept_id: str, bot: dict[str, Any]
) -> None:
    """Schema MUST accept any dept with a valid kebab id and ≥1 bot service.

    Validates Requirements 7.4 (kebab-case id) and 7.5 (any non-empty
    subset of ``{jira, bitbucket, confluence}`` is acceptable).
    """

    # Defensive: ``bot_object(min_services=1)`` cannot return ``{}`` but
    # the assertion makes the property's pre-condition explicit.
    assert bot, "bot_object(min_services=1) must produce a non-empty dict"
    assert VALID_ID_RE.fullmatch(dept_id) is not None

    document = _wrap(_make_dept(dept_id=dept_id, bot=bot))

    # Should not raise.
    validator.validate(document)


# ---------------------------------------------------------------------------
# Property 8b — REJECT: id violates regex OR bot is {}
# ---------------------------------------------------------------------------


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(dept_id=invalid_id, bot=bot_object(min_services=1))
def test_reject_when_id_invalid(
    validator: Draft202012Validator, dept_id: str, bot: dict[str, Any]
) -> None:
    """Any dept with an invalid id MUST be rejected, even with a full bot.

    Validates Requirement 7.4 (kebab-case enforcement).
    """

    assume(not VALID_ID_RE.fullmatch(dept_id))

    document = _wrap(_make_dept(dept_id=dept_id, bot=bot))

    with pytest.raises(ValidationError):
        validator.validate(document)


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(dept_id=valid_id)
def test_reject_when_bot_empty(
    validator: Draft202012Validator, dept_id: str
) -> None:
    """Any dept with an empty ``bot`` MUST be rejected, even with valid id.

    Validates Requirement 7.5 (``minProperties: 1`` + ``anyOf`` on
    ``{jira, bitbucket, confluence}``).
    """

    document = _wrap(_make_dept(dept_id=dept_id, bot={}))

    with pytest.raises(ValidationError):
        validator.validate(document)


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(dept_id=invalid_id)
def test_reject_when_id_invalid_and_bot_empty(
    validator: Draft202012Validator, dept_id: str
) -> None:
    """Both invariants violated → still rejected (with at least one error)."""

    assume(not VALID_ID_RE.fullmatch(dept_id))

    document = _wrap(_make_dept(dept_id=dept_id, bot={}))

    with pytest.raises(ValidationError):
        validator.validate(document)


# ---------------------------------------------------------------------------
# Subset coverage (Requirement 7.5 explicit enumeration)
# ---------------------------------------------------------------------------


def _all_non_empty_subsets() -> list[tuple[str, ...]]:
    """Return all 7 non-empty subsets of ``{jira, bitbucket, confluence}``.

    Order is fixed (size-ascending then alphabetical within size) so
    pytest's parameterised ids are stable across runs.
    """

    subsets: list[tuple[str, ...]] = []
    for size in range(1, len(BOT_SERVICES) + 1):
        for combo in combinations(BOT_SERVICES, size):
            subsets.append(combo)
    return subsets


def _bot_for_subset(subset: tuple[str, ...]) -> dict[str, Any]:
    """Construct a minimum-valid ``bot`` populated with exactly ``subset``."""

    return {
        svc: {
            "credential_ref": f"vault:atlassian/example/{svc}",
            "account_id": "",
            "username": f"x-bot-{svc}@example.com",
        }
        for svc in subset
    }


@pytest.mark.parametrize(
    "subset",
    _all_non_empty_subsets(),
    ids=lambda subset: "+".join(subset),
)
def test_all_seven_non_empty_subsets_are_accepted(
    validator: Draft202012Validator, subset: tuple[str, ...]
) -> None:
    """Every non-empty subset of ``{jira, bitbucket, confluence}`` validates.

    Validates Requirement 7.5 — the schema's ``anyOf`` over the three
    services must accept Jira-only, Bitbucket-only, Confluence-only,
    every two-service combination, and the full triple.
    """

    document = _wrap(
        _make_dept(dept_id="example-dept", bot=_bot_for_subset(subset))
    )

    # Should not raise; ``iter_errors`` surfaces every breach in one go.
    errors = sorted(
        validator.iter_errors(document),
        key=lambda e: list(e.absolute_path),
    )
    assert not errors, (
        f"subset {subset!r} unexpectedly rejected:\n  "
        + "\n  ".join(
            f"at {list(err.absolute_path) or '<root>'}: {err.message}"
            for err in errors
        )
    )


def test_empty_subset_is_rejected(validator: Draft202012Validator) -> None:
    """The empty bot subset (``{}``) is the one case that MUST fail.

    Validates Requirement 7.5 — ``minProperties: 1`` + the ``anyOf``
    block both fire on an empty ``bot``.
    """

    document = _wrap(_make_dept(dept_id="example-dept", bot={}))

    with pytest.raises(ValidationError):
        validator.validate(document)


# ===========================================================================
# Property 5 (platform-mimari-foundation) extension
#
# The four sub-properties below exercise the parts of design §"Property 5"
# not already covered by the kebab-id / non-empty-bot tests above:
#
#   (b) ``credential_ref`` regex  — ``^vault:[a-zA-Z0-9/_-]+$``
#   (c) ``has_*`` flag prohibition (closed-world via additionalProperties)
#   (d) duplicate ``id`` rejection at the loader level
#
# Sub-property (a) — "schema-conforming objects validate" — is covered by
# the existing accept/reject pair; we add an explicit smoke check that
# the on-disk ``config/departments.json`` document validates as written
# (Requirement 3.1: "schema dışı her durum servis başlangıcını başarısız
# sayar").
# ===========================================================================


# ---------------------------------------------------------------------------
# Vault path strategies (Property 5b) — schema regex ^vault:[a-zA-Z0-9/_-]+$
# ---------------------------------------------------------------------------

#: Compiled regex matching the schema's ``credential_ref`` pattern. The
#: schema deliberately accepts a broader (mixed-case) character class
#: than the requirements text (R3.3 says lowercase) — see design.md
#: "Tasarım Kararları" table: the lowercase rule is enforced in the
#: style guide, the schema enforces the broader regex.
VAULT_REF_RE: re.Pattern[str] = re.compile(r"^vault:[a-zA-Z0-9/_-]+$")

#: Character class allowed *after* the ``vault:`` prefix.
_VAULT_BODY_ALPHABET: str = (
    string.ascii_letters + string.digits + "/_-"
)

# Strategy that always produces a string conforming to the regex.
valid_credential_ref = st.from_regex(
    r"^vault:[a-zA-Z0-9/_-]+$", fullmatch=True
)

# Strategy that produces credential_ref-shaped strings that *fail* the
# regex. Each branch picks one specific failure mode so the failure is
# explainable when a counter-example is shrunk.
invalid_credential_ref = st.one_of(
    # Empty string — fails both the prefix anchor and minimum-length.
    st.just(""),
    # Wrong scheme prefix.
    st.text(
        alphabet=string.ascii_lowercase, min_size=1, max_size=8
    ).map(lambda s: f"{s}:atlassian/example/jira").filter(
        lambda s: not s.startswith("vault:")
    ),
    # Plain-text token (no scheme).
    st.text(
        alphabet=string.ascii_letters + string.digits, min_size=8, max_size=32
    ),
    # Empty body after prefix.
    st.just("vault:"),
    # Body containing forbidden characters (space, ``:``, ``@``, ``.``).
    st.text(
        alphabet=" :@.\t", min_size=1, max_size=4
    ).map(lambda s: f"vault:atlassian/{s}/jira"),
    # Mixed-valid + invalid characters.
    st.from_regex(r"^vault:[a-zA-Z0-9/_-]*[ :@.\t!*?]+[a-zA-Z0-9/_-]*$",
                  fullmatch=True),
).filter(lambda s: not VAULT_REF_RE.fullmatch(s))


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(dept_id=valid_id, ref=valid_credential_ref)
def test_credential_ref_accepts_when_matches_vault_regex(
    validator: Draft202012Validator, dept_id: str, ref: str
) -> None:
    """``bot.<service>.credential_ref`` MUST be accepted when the value
    matches ``^vault:[a-zA-Z0-9/_-]+$``.

    Validates Requirement 3.3 (credential_ref regex) and Requirement 6.1
    (Vault path convention) — Property 5(b) of design.md.
    """

    assert VAULT_REF_RE.fullmatch(ref) is not None, (
        f"strategy invariant broken: {ref!r}"
    )

    bot = {
        "jira": {
            "credential_ref": ref,
            "account_id": "",
            "username": "x-bot-jira@example.com",
        }
    }
    document = _wrap(_make_dept(dept_id=dept_id, bot=bot))

    # Should not raise.
    validator.validate(document)


@settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(dept_id=valid_id, ref=invalid_credential_ref)
def test_credential_ref_rejects_when_violates_vault_regex(
    validator: Draft202012Validator, dept_id: str, ref: str
) -> None:
    """``bot.<service>.credential_ref`` MUST be rejected when the value
    does not match ``^vault:[a-zA-Z0-9/_-]+$``.

    The schema's ``BotEntry`` has an ``anyOf`` between ``credential_ref``
    and ``email + api_token_ref``; we only populate the first branch so
    the regex is the sole gating constraint, then assert that the
    overall document fails validation.

    Validates Requirement 3.3 (no plain-text token, no basic-auth, no
    base64 fallback in the credential_ref slot) — Property 5(b).
    """

    assume(not VAULT_REF_RE.fullmatch(ref))

    bot = {
        "jira": {
            "credential_ref": ref,
            "account_id": "",
            "username": "x-bot-jira@example.com",
        }
    }
    document = _wrap(_make_dept(dept_id=dept_id, bot=bot))

    with pytest.raises(ValidationError):
        validator.validate(document)


# ---------------------------------------------------------------------------
# Property 5(c) — has_* flag prohibition (Requirement 3.7)
# ---------------------------------------------------------------------------

#: The three forbidden flag names that some legacy configs add at the
#: department level. Capabilities are derived solely from credential
#: presence; emitting these flags is a code smell that the schema must
#: reject mechanically (design §"Tasarım Kararları" → "credential var =
#: servis var").
FORBIDDEN_HAS_FLAGS: tuple[str, ...] = (
    "has_jira",
    "has_bitbucket",
    "has_confluence",
)


@pytest.mark.parametrize("flag_name", FORBIDDEN_HAS_FLAGS)
@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    dept_id=valid_id,
    bot=bot_object(min_services=1),
    flag_value=st.booleans(),
)
def test_reject_when_dept_has_forbidden_has_flag(
    validator: Draft202012Validator,
    dept_id: str,
    bot: dict[str, Any],
    flag_name: str,
    flag_value: bool,
) -> None:
    """Adding ``has_jira``/``has_bitbucket``/``has_confluence`` to a
    department MUST be rejected by the schema.

    The ``Department`` object declares ``additionalProperties: false``,
    so any unknown top-level key surfaces as a ``ValidationError``.
    This is the schema-level enforcement of Requirement 3.7 — capability
    derivation is single-sourced from ``bot.<svc>.credential_ref``.

    Validates Requirement 3.7 — Property 5(c).
    """

    dept = _make_dept(dept_id=dept_id, bot=bot)
    dept[flag_name] = flag_value
    document = _wrap(dept)

    with pytest.raises(ValidationError):
        validator.validate(document)


@pytest.mark.parametrize("flag_name", FORBIDDEN_HAS_FLAGS)
def test_on_disk_departments_carry_no_has_flags(
    repo_root: Path, flag_name: str
) -> None:
    """The committed ``config/departments.json`` MUST NOT carry any
    ``has_*`` flag at the department level.

    A regression here would mean a hand-authored config drifted from
    Requirement 3.7. We check the document instead of the schema so the
    tooling guard catches manual edits even before Hypothesis fires.
    """

    departments_path = repo_root / "config" / "departments.json"
    document = json.loads(departments_path.read_text(encoding="utf-8"))

    for entry in document["departments"]:
        assert flag_name not in entry, (
            f"department {entry.get('id', '?')!r} carries forbidden "
            f"flag {flag_name!r}; capabilities must be derived from "
            f"bot.<service>.credential_ref alone (Requirement 3.7)."
        )


# ---------------------------------------------------------------------------
# Property 5(d) — duplicate id rejection (Requirement 3.9)
# ---------------------------------------------------------------------------


def _detect_duplicate_dept_ids(document: dict[str, Any]) -> list[str]:
    """Loader-level duplicate detector mirroring automation-service.

    Returns the list of duplicated ``id`` values in
    ``document["departments"]`` (empty when there are no duplicates).
    The function exists in this test module rather than in a shared lib
    because (a) the loader implementation lands in a later task
    (1.1/3.x) and (b) the property must hold *prior* to that work — the
    contract is "duplicate ids are detectable at parse time".
    """

    seen: dict[str, int] = {}
    duplicates: list[str] = []
    for entry in document.get("departments", []):
        dept_id = entry.get("id")
        if dept_id is None:
            continue
        if dept_id in seen:
            duplicates.append(dept_id)
        else:
            seen[dept_id] = 1
    return duplicates


@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    dept_id=valid_id,
    bot=bot_object(min_services=1),
)
def test_duplicate_dept_id_is_detected(
    dept_id: str, bot: dict[str, Any]
) -> None:
    """A document with two departments sharing the same ``id`` MUST be
    flagged as duplicate.

    Per Requirement 3.9 (and design §"Property 5(d)"), the loader is
    expected to return HTTP 409 / fail boot rather than silently overwrite
    one entry with the other. Here we assert the *detection* invariant
    that any future loader implementation can wire to its 409 response.

    Note: JSON Schema 2020-12 cannot express "unique by sub-key", so this
    check lives at the loader layer; the property still belongs to the
    schema test module because the same data shape is the input.

    Validates Requirement 3.9 — Property 5(d).
    """

    dept_a = _make_dept(dept_id=dept_id, bot=bot, display_name="A")
    dept_b = _make_dept(dept_id=dept_id, bot=bot, display_name="B")
    document = {"version": 1, "departments": [dept_a, dept_b]}

    duplicates = _detect_duplicate_dept_ids(document)

    assert duplicates == [dept_id], (
        f"expected duplicate detector to flag {dept_id!r} exactly once "
        f"but got {duplicates!r}"
    )


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(
    ids=st.lists(valid_id, min_size=2, max_size=6, unique=True),
    bot=bot_object(min_services=1),
)
def test_unique_dept_ids_are_not_flagged(
    ids: list[str], bot: dict[str, Any]
) -> None:
    """A document with all-distinct department ``id`` values MUST NOT be
    flagged as duplicate.

    Companion of the previous test: the detector is *exact* (no false
    positives) so legitimate multi-department configs pass cleanly.
    """

    departments = [
        _make_dept(dept_id=did, bot=bot, display_name=f"Dept {did}")
        for did in ids
    ]
    document = {"version": 1, "departments": departments}

    duplicates = _detect_duplicate_dept_ids(document)

    assert duplicates == [], (
        f"unexpected duplicate flag for distinct ids {ids!r}: {duplicates!r}"
    )


def test_on_disk_departments_have_unique_ids(repo_root: Path) -> None:
    """The committed ``config/departments.json`` MUST carry unique ids.

    Mirrors Requirement 3.9 at the file level: a regression here would
    let two departments race for the same Vault path namespace
    (``vault:atlassian/<dept_id>/<service>``) which is the precise
    failure mode the duplicate guard is designed to prevent.
    """

    departments_path = repo_root / "config" / "departments.json"
    document = json.loads(departments_path.read_text(encoding="utf-8"))

    duplicates = _detect_duplicate_dept_ids(document)
    assert duplicates == [], (
        f"config/departments.json contains duplicate dept ids: "
        f"{duplicates!r}"
    )


# ---------------------------------------------------------------------------
# Property 5(a) — on-disk smoke check
# ---------------------------------------------------------------------------


def test_on_disk_departments_validate_against_schema(
    validator: Draft202012Validator, repo_root: Path
) -> None:
    """The committed ``config/departments.json`` MUST validate against
    the committed schema.

    Mechanises Requirement 3.1: "servis başlatıldığında departments.json
    schema'ya göre doğrulanır ve schema dışı her durum servis
    başlangıcını başarısız sayar." A failure here would block boot in
    every environment, so the regression net is on the test side.
    """

    departments_path = repo_root / "config" / "departments.json"
    assert departments_path.is_file(), (
        f"missing departments.json at {departments_path}"
    )
    document = json.loads(departments_path.read_text(encoding="utf-8"))

    errors = sorted(
        validator.iter_errors(document),
        key=lambda e: list(e.absolute_path),
    )
    assert not errors, (
        "config/departments.json failed schema validation:\n  "
        + "\n  ".join(
            f"at {list(err.absolute_path) or '<root>'}: {err.message}"
            for err in errors
        )
    )
