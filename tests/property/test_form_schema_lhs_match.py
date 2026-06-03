"""invariant — Form schema = Component_Env_Example LHS key set.



Invariant statement
------------------
For *every* Managed_Service ``S`` declared in
``config/services.manifest.json``, the LHS key set produced by

 parse_env_example(read_text(S.env_example_path))

MUST equal the LHS key set returned by

 LifecycleService.get_form_schema(S.name)

as **two equal sets** — no missing keys, no extra keys, ordering
preserved: "form, dosyada görünen sırada üretilir";: "yorum metni input alanının yardım metnine bağlanır";: form_schema LHS =.env.example LHS as exact set
equality).

This is the property-level proof that the *data path* between
``parse_env_example`` (the pure parser) and
``LifecycleService.get_form_schema`` (the form-rendering surface)
cannot drift: every operator-facing form field
corresponds to exactly one ``.env.example`` LHS key, and every
``.env.example`` LHS key surfaces as exactly one form field.

Edge case — parser determinism
------------------------------
The parser must stay deterministic across "comment + blank line +
assignment" combinations.
We exercise that by injecting Hypothesis-generated comment and blank
lines around the real ``.env.example`` content for one randomly
sampled service and asserting that

 parse_env_example(perturbed_text)

returns the *same* LHS key set as the unperturbed file. Comment lines
and blank lines must not change the set of fields the parser
recognises — they only change the comment-buffer state, which the
LHS-set equality check is insensitive to.

Strategy
--------
* Manifest discovery: ``load_manifest(workspace_root)`` is called
 once per session; the resulting tuple of ``ManagedServiceEntry``
 values drives ``st.sampled_from(...)``. This guarantees the
 property is exercised against every real Managed_Service.
* Parser-determinism axis: ``st.text`` over a constrained alphabet
 builds plausible comment lines (``#`` prefix) interleaved with
 blank lines. The injection points are ``st.lists(st.integers)``
 so Hypothesis can shrink towards minimal perturbation patterns
 when a counterexample shows up.

Stub fakes
----------
The orchestrator only exercises ``get_form_schema`` here, which is a
*synchronous* read-side method. It does not touch Vault, Compose,
Audit or HealthProbe — but:class:`LifecycleService.__init__`
requires those clients regardless. We therefore wire the same
``_FakeAuditWriter`` / ``_FakeVaultClient`` / ``_FakeComposeRunner``
/ ``_FakeHealthProbe`` shells used by invariant
(``test_log_redaction.py``); their bodies are unused on this code
path.
"""

from __future__ import annotations

import string
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ``conftest.py`` lives one directory up; pytest auto-loads it but we
# add ``tests/`` to ``sys.path`` defensively so this module also imports
# cleanly under a direct ``python -m pytest tests/property`` invocation
# (mirrors the pattern used by every other invariant in this folder).
_TESTS_DIR = Path(__file__).resolve().parent.parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

# The ``admin-dashboard-api`` package is not pip-installed inside the
# test environment, so we expose its source tree on ``sys.path`` the
# same way the per-service unit tests do. This lets us
# ``import src.lifecycle.service`` directly (mirrors
# ``test_stop_idempotent.py`` / ``test_log_redaction.py``).
_WORKSPACE_ROOT: Path = Path(__file__).resolve().parents[2]
_SERVICE_ROOT: Path = (
    _WORKSPACE_ROOT / "services" / "admin-dashboard-api"
)
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from src.lifecycle.audit_writer import (  # noqa: E402
    AuditEntry,
    AuditWriteOutcome,
)
from src.lifecycle.compose_runner import ComposeResult  # noqa: E402
from src.lifecycle.env_parser import parse_env_example  # noqa: E402
from src.lifecycle.health_probe import HealthSnapshot  # noqa: E402
from src.lifecycle.service import LifecycleService  # noqa: E402
from src.manifest import (  # noqa: E402
    ManagedServiceEntry,
    load_manifest,
)


# ---------------------------------------------------------------------------
# Manifest discovery — drives the ``service`` axis of the property.
# ---------------------------------------------------------------------------

# Loaded once at import time so Hypothesis can build the
# ``st.sampled_from`` strategy without paying the JSON-parse +
# schema-validation cost on every example.
_MANAGED_SERVICES: tuple[ManagedServiceEntry, ...] = load_manifest(_WORKSPACE_ROOT)
assert _MANAGED_SERVICES, (
    "config/services.manifest.json must declare at least one Managed_Service "
    "for invariant to be meaningful"
)


# ---------------------------------------------------------------------------
# Fakes (deliberately green — get_form_schema does not touch them)
# ---------------------------------------------------------------------------


@dataclass
class _FakeAuditWriter:
    """No-op audit writer; ``get_form_schema`` does not touch the audit path."""

    write_with_retry_calls: list[AuditEntry] = field(default_factory=list)

    async def precheck(self) -> None:  # pragma: no cover - unused by get_form_schema
        return None

    async def write(self, entry: AuditEntry) -> None:  # pragma: no cover - unused
        return None

    async def write_with_retry(  # pragma: no cover - unused
        self, entry: AuditEntry
    ) -> AuditWriteOutcome:
        self.write_with_retry_calls.append(entry)
        return AuditWriteOutcome(deferred=False)


@dataclass
class _FakeVaultClient:
    """No-op Vault client; ``get_form_schema`` neither reads nor writes secrets."""

    async def write_env_override(  # pragma: no cover - unused
        self, *, service_name: str, key: str, value: str
    ) -> None:
        return None

    async def read_env_overrides(  # pragma: no cover - unused
        self, *, service_name: str
    ) -> dict[str, str]:
        return {}

    async def delete_env_override(  # pragma: no cover - unused
        self, *, service_name: str, key: str
    ) -> None:
        return None


@dataclass
class _FakeComposeRunner:
    """No-op Compose runner; ``get_form_schema`` does not invoke docker."""

    async def up(  # pragma: no cover - unused by invariant
        self,
        *,
        profile: str,
        service_name: str,
        env_overrides: dict[str, str] | None = None,
    ) -> ComposeResult:
        return ComposeResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=("docker", "compose", "up", "-d", service_name),
        )

    async def stop(  # pragma: no cover - unused by invariant
        self, *, service_name: str, remove_volumes: bool = False
    ) -> ComposeResult:
        return ComposeResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=("docker", "compose", "stop", service_name),
        )

    async def logs(  # pragma: no cover - unused by invariant
        self, *, service_name: str, tail: int, follow: bool
    ) -> ComposeResult:
        return ComposeResult(
            exit_code=0,
            stdout="",
            stderr="",
            argv=("docker", "compose", "logs", service_name),
        )

    async def exec_test(  # pragma: no cover - unused by invariant
        self,
        *,
        service_name: str,
        argv: Sequence[str],
        stream: bool = False,
    ) -> Any:
        raise NotImplementedError


@dataclass
class _FakeHealthProbe:
    """Stable ``healthy`` snapshot; ``get_form_schema`` never invokes it."""

    async def probe(  # pragma: no cover - unused by invariant
        self, entry: ManagedServiceEntry
    ) -> HealthSnapshot:
        return HealthSnapshot(
            ts=datetime.now(timezone.utc),
            healthz_status=200,
            healthz_body="ok",
            readyz_status=200,
            readyz_body="ok",
            state="healthy",
        )


# ---------------------------------------------------------------------------
# LifecycleService factory — bound to the real manifest + workspace_root
# ---------------------------------------------------------------------------


def _make_real_service() -> LifecycleService:
    """Wire a:class:`LifecycleService` against the real manifest + workspace.

 ``get_form_schema`` resolves ``entry.env_example_path`` against
 ``workspace_root`` and reads the file from disk, so the property
 *must* point both at the real values shipped with the repo. Every
 other dependency is the no-op fake from above.
 """

    audit = _FakeAuditWriter()
    vault = _FakeVaultClient()
    compose = _FakeComposeRunner()
    health = _FakeHealthProbe()

    async def _no_sleep(_seconds: float) -> None:  # pragma: no cover - unused
        return None

    return LifecycleService(
        manifest=_MANAGED_SERVICES,
        state=None,
        audit=audit,  # type: ignore[arg-type]
        vault=vault,  # type: ignore[arg-type]
        compose=compose,  # type: ignore[arg-type]
        health=health,  # type: ignore[arg-type]
        workspace_root=_WORKSPACE_ROOT,
        health_ready_timeout_seconds=1.0,
        sleep=_no_sleep,
    )


#: Build the orchestrator once per session — it is a pure object whose
#: ``get_form_schema`` is referentially transparent (modulo a per-path
#: cache built on first call). Reusing it across Hypothesis examples
#: avoids re-validating the manifest 20× per test run.
_SERVICE: LifecycleService = _make_real_service()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_env_example(entry: ManagedServiceEntry) -> str:
    """Read the raw bytes of the ``.env.example`` file backing ``entry``.

 Mirrors:meth:`LifecycleService._load_env_fields` exactly: missing
 files collapse to the empty string, which produces an empty
 schema. This keeps the property's "two views agree" framing
 intact even for the (hypothetical) zero-key case.
 """

    path = _WORKSPACE_ROOT / entry.env_example_path
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


# ---------------------------------------------------------------------------
# Edge-case strategies — comment / blank line perturbations
# ---------------------------------------------------------------------------

# Comment-line text alphabet — ASCII letters, digits, spaces, underscores
# and a handful of punctuation characters that real ``.env.example``
# comments contain. We deliberately exclude ``\n`` (the splitlines
# boundary) and ``=`` (which would risk producing a fake assignment line
# if the comment body somehow lost its ``#`` prefix).
_COMMENT_ALPHABET: str = (
    string.ascii_letters + string.digits + " _-:.,/[]"
)

# A single perturbation is either:
# * a comment line (``#...``),
# * a hash-only line (``#``), or
# * a blank line (``""``).
# All three exercise distinct branches in:func:`parse_env_example`'s
# line classifier: comment with body, comment without body, and blank line.
_perturbation_strategy: st.SearchStrategy[str] = st.one_of(
    st.text(alphabet=_COMMENT_ALPHABET, min_size=0, max_size=40).map(
        lambda body: f"# {body}" if body else "#"
    ),
    st.just("#"),
    st.just(""),
)

# A "perturbation block" — a run of consecutive perturbation lines
# inserted at a single splice point. ``min_size=0`` lets Hypothesis
# shrink towards the no-perturbation case; ``max_size=4`` keeps the
# overall input bounded.
_perturbation_block: st.SearchStrategy[list[str]] = st.lists(
    _perturbation_strategy, min_size=0, max_size=4
)


def _splice_perturbations(text: str, blocks: list[list[str]]) -> str:
    """Insert one perturbation block between every pair of original lines.

 The original line order is preserved, so every assignment line in
 ``text`` survives at the same relative position. ``blocks`` may
 contain at most ``len(original_lines) + 1`` blocks (one per gap,
 including the leading and trailing ones); extras are ignored.
 """

    original_lines = text.splitlines()
    out: list[str] = []
    # Leading block — before the first original line.
    if blocks:
        out.extend(blocks[0])
    for index, line in enumerate(original_lines):
        out.append(line)
        # Trailing block for this line (i.e. block[index+1]).
        if index + 1 < len(blocks):
            out.extend(blocks[index + 1])
    return "\n".join(out)


# ---------------------------------------------------------------------------
# invariant — form schema LHS set ==.env.example LHS set
# ---------------------------------------------------------------------------


@given(entry=st.sampled_from(_MANAGED_SERVICES))
@settings(
    deadline=None,
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_form_schema_keys_equal_env_example_lhs_keys(
    entry: ManagedServiceEntry,
) -> None:
    """invariant (core invariant) — form schema LHS set ==.env.example LHS set.



 For every Managed_Service in the real manifest, the set of LHS
 keys produced by:func:`parse_env_example` on the file pointed to
 by ``entry.env_example_path`` MUST equal — as a set, with no
 missing or extra keys — the set of ``key`` values returned by:meth:`LifecycleService.get_form_schema(entry.name)`. Per, the ordering returned by ``get_form_schema``
 MUST match the parser's file-order output too.
 """

    # Side A: the parser, run directly against the file bytes.
    raw_text = _read_env_example(entry)
    parser_fields = parse_env_example(raw_text)
    parser_keys: list[str] = [f.key for f in parser_fields]
    parser_key_set: set[str] = set(parser_keys)

    # Side B: the orchestrator's form-schema view.
    schema_fields = _SERVICE.get_form_schema(entry.name)
    schema_keys: list[str] = [f.key for f in schema_fields]
    schema_key_set: set[str] = set(schema_keys)

    # Invariant 1 — exact set equality.
    missing = parser_key_set - schema_key_set
    extra = schema_key_set - parser_key_set
    assert parser_key_set == schema_key_set, (
        f"form schema LHS set mismatch for service {entry.name!r} "
        f"(env_example_path={entry.env_example_path!r}). "
        f"missing from schema: {sorted(missing)!r}; "
        f"extra in schema: {sorted(extra)!r}."
    )

    # Invariant 2 — file-order preservation.
    # The parser returns fields in file order; ``get_form_schema``
    # must surface them in the same order so the rendered form
    # matches the operator's mental model of the.env.example file.
    assert schema_keys == parser_keys, (
        f"form schema preserves file order for service {entry.name!r} "
        f"but ``get_form_schema`` returned {schema_keys!r} while the "
        f"parser returned {parser_keys!r} (env_example_path="
        f"{entry.env_example_path!r})."
    )

    # Invariant 3 — every schema field carries the same metadata as
    # its parser counterpart (default_value, comment, is_sensitive).
    # The operator help text depends on ``comment`` round-tripping
    # through the schema without modification, and sensitive-field
    # masking depends on ``is_sensitive`` round-tripping too. Both
    # are folded into the invariant here so a regression in either
    # direction fails this single invariant.
    for parser_field, schema_field in zip(parser_fields, schema_fields):
        assert schema_field.default_value == parser_field.default_value, (
            f"default_value drift for service {entry.name!r}, key "
            f"{parser_field.key!r}: parser={parser_field.default_value!r}, "
            f"schema={schema_field.default_value!r}"
        )
        assert schema_field.comment == parser_field.comment, (
            f"comment drift for service {entry.name!r}, key "
            f"{parser_field.key!r}: parser={parser_field.comment!r}, "
            f"schema={schema_field.comment!r}"
        )
        assert schema_field.is_sensitive == parser_field.is_sensitive, (
            f"is_sensitive drift for service {entry.name!r}, key "
            f"{parser_field.key!r}: parser={parser_field.is_sensitive!r}, "
            f"schema={schema_field.is_sensitive!r}"
        )


# ---------------------------------------------------------------------------
# invariant — parser determinism under comment + blank-line perturbation
# ---------------------------------------------------------------------------


@given(
    entry=st.sampled_from(_MANAGED_SERVICES),
    perturbations=st.lists(_perturbation_block, min_size=0, max_size=64),
)
@settings(
    deadline=None,
    max_examples=20,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_parser_is_deterministic_under_comment_and_blank_line_perturbations(
    entry: ManagedServiceEntry,
    perturbations: list[list[str]],
) -> None:
    """invariant (parser determinism) — comments / blanks don't change LHS set.



 Inserting any sequence of comment lines (``#``-prefixed bodies)
 and blank lines around the assignment lines of a real
 ``.env.example`` file MUST NOT change the set of LHS keys the
 parser produces. The parser is also deterministic — running it
 twice on the same input yields the same field list — and is
 insensitive to leading / trailing whitespace lines.

 Comment lines feed the comment buffer and blank lines reset it,
 but neither line shape can spawn or suppress an assignment-derived
 field.
 """

    raw_text = _read_env_example(entry)
    baseline_keys: list[str] = [f.key for f in parse_env_example(raw_text)]

    perturbed_text = _splice_perturbations(raw_text, perturbations)
    perturbed_keys: list[str] = [f.key for f in parse_env_example(perturbed_text)]

    # Invariant 1 — LHS set equality across perturbation.
    assert set(perturbed_keys) == set(baseline_keys), (
        f"perturbed parse changed the LHS key set for service "
        f"{entry.name!r}. baseline={baseline_keys!r}, "
        f"perturbed={perturbed_keys!r}, perturbations={perturbations!r}"
    )

    # Invariant 2 — file-order preservation. Inserting non-assignment
    # lines must not reorder the assignments themselves.
    assert perturbed_keys == baseline_keys, (
        f"perturbed parse reordered assignments for service "
        f"{entry.name!r}. baseline={baseline_keys!r}, "
        f"perturbed={perturbed_keys!r}, perturbations={perturbations!r}"
    )

    # Invariant 3 — idempotence: parsing the same input twice yields
    # the exact same field list (object equality on the frozen
    # dataclass list, which compares ``key``, ``default_value``,
    # ``comment``, and ``is_sensitive`` field-by-field).
    again = parse_env_example(perturbed_text)
    once = parse_env_example(perturbed_text)
    assert again == once, (
        f"parser is non-deterministic for service {entry.name!r}: "
        f"first call returned {once!r}, second call returned {again!r}"
    )


# ---------------------------------------------------------------------------
# Concrete regression anchor — every manifest service surfaces ≥1 field
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    _MANAGED_SERVICES,
    ids=[e.name for e in _MANAGED_SERVICES],
)
def test_each_manifest_service_yields_a_non_empty_form_schema(
    entry: ManagedServiceEntry,
) -> None:
    """Concrete anchor: every manifest service produces ≥1 form field.

 The Hypothesis-driven tests above prove the *equality* invariant,
 but neither asserts the schemas are non-empty. A bug that made
 both ``parse_env_example`` and ``get_form_schema`` silently return
 the empty list would still pass the equality check while leaving
 the operator with no form to fill in. This anchor pins the lower
 bound: every service shipped with the repo carries at least one
 declared LHS key in its ``.env.example`` file (true by
 construction of the project).
 """

    schema_fields = _SERVICE.get_form_schema(entry.name)
    assert len(schema_fields) >= 1, (
        f"service {entry.name!r} (env_example_path="
        f"{entry.env_example_path!r}) produced an empty form schema; "
        f"every Managed_Service must surface at least one LHS key."
    )
