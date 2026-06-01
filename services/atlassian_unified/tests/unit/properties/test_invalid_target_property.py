"""Property test P11 — Pre-HTTP input-shape validation emits ``invalid_target``.

Validates Requirements 11.5 and 13.3 of the
``bitbucket-cloud-dc-parity`` spec / design Property 11.

Two properties are exercised:

1. :meth:`UsersMixin.get_user` in CloudMode — for any generated
   ``username`` that does NOT match the Cloud ``account_id`` shape
   (neither the legacy brace-wrapped UUID form
   ``^\\{[0-9a-f-]{36}\\}$`` nor the modern account-id form
   ``^[A-Za-z0-9_:\\-]+$``), the mixin SHALL raise a
   :class:`ValueError` whose message is prefixed with
   ``"invalid_target:"`` BEFORE issuing any HTTP call. The server-tool
   layer maps that prefix onto a structured ``invalid_target`` error
   envelope. The "pre-HTTP" half of the invariant is verified by
   asserting ``mixin.bitbucket.get.call_count == 0`` on the
   :class:`~unittest.mock.MagicMock` that stands in for the
   ``atlassian.Bitbucket`` client.

2. :meth:`CommitsMixin.compare_commits` in CloudMode — for any
   generated ``from_ref`` / ``to_ref`` pair where at least one side is
   empty or contains any of ``/``, ``?``, ``#`` or whitespace (space,
   tab, newline, carriage return, form feed, vertical tab), the mixin
   SHALL raise a :class:`ValueError` whose message is prefixed with
   ``"invalid_target:"`` BEFORE issuing any HTTP call. On Cloud the
   compare endpoint is reached via ``self.bitbucket._session.get``
   (unified-diff text, not JSON), so the pre-HTTP assertion covers
   both ``mixin.bitbucket.get.call_count == 0`` AND
   ``mixin.bitbucket._session.get.call_count == 0``.

The mixin DC branches are intentionally out of scope — this property
covers the Cloud pre-HTTP guards exclusively (Requirements 19.3, 23.2).
The tests stamp ``is_cloud=True`` onto a bypassed mixin instance and
inspect only what the Cloud branch does, mirroring the pattern used in
:mod:`tests.unit.bitbucket.test_users_cloud_mode` and
:mod:`tests.unit.bitbucket.test_commits_cloud_mode`.

**Validates: Requirements 11.5, 13.3**
"""

from __future__ import annotations

import re
import string
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from mcp_atlassian.bitbucket.commits import CommitsMixin
from mcp_atlassian.bitbucket.users import UsersMixin


# ---------------------------------------------------------------------------
# Cloud ``account_id`` shape — mirror the definitions in users.py so this
# property test can generate strings guaranteed to NOT match.
# ---------------------------------------------------------------------------
#
# ``users.py`` defines the two accepted Cloud identifier shapes as:
#
#   _CLOUD_UUID_RE       = re.compile(r"^\{[0-9a-f-]{36}\}$")
#   _CLOUD_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9_:\-]+$")
#
# A username that matches NEITHER is rejected pre-HTTP with
# ``invalid_target:``. We re-declare the regexes here (rather than
# importing the private helper) so a regression in the allowlist
# definition surfaces as a property-test failure with a clear
# counter-example instead of silently agreeing with the code.
_CLOUD_UUID_RE = re.compile(r"^\{[0-9a-f-]{36}\}$")
_CLOUD_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9_:\-]+$")

# The characters inside the modern Cloud ``account_id`` alphabet. Any
# string composed exclusively of these characters would pass the
# allowlist check, so our "invalid username" strategies MUST emit at
# least one character outside this set (or be empty).
_ACCOUNT_ID_ALPHABET = frozenset(string.ascii_letters + string.digits + "_:-")

# Characters guaranteed to lie OUTSIDE ``_ACCOUNT_ID_ALPHABET``. DC-style
# usernames commonly contain ``.`` (``first.last``) or ``@`` (email
# slugs); whitespace, ``/``, ``?`` and ``#`` are also outside the set
# and are additionally the illegal chars enforced by the
# ``compare_commits`` guard, which makes them useful for joint coverage.
_FORBIDDEN_USERNAME_CHARS: tuple[str, ...] = (
    ".",
    "@",
    "#",
    "?",
    "/",
    "\\",
    "!",
    "$",
    "%",
    "^",
    "&",
    "*",
    "(",
    ")",
    "+",
    "=",
    "[",
    "]",
    "<",
    ">",
    "'",
    '"',
    ";",
    ",",
    " ",
    "\t",
    "\n",
    "\r",
)


# ---------------------------------------------------------------------------
# Illegal characters for ``compare_commits`` ``from_ref`` / ``to_ref``.
# ---------------------------------------------------------------------------
#
# commits.py rejects any ref that is empty or contains any of ``/``,
# ``?``, ``#`` or whitespace (``str.isspace()`` true). We enumerate the
# concrete whitespace characters here so Hypothesis can sample from a
# concrete set rather than guess what counts as whitespace.
_COMPARE_ILLEGAL_CHARS: tuple[str, ...] = (
    "/",
    "?",
    "#",
    " ",
    "\t",
    "\n",
    "\r",
    "\x0b",  # vertical tab — ``str.isspace()`` is True
    "\x0c",  # form feed — ``str.isspace()`` is True
)


# ---------------------------------------------------------------------------
# Fixtures — bypassed mixin instances wired for Cloud mode.
# ---------------------------------------------------------------------------


@pytest.fixture
def cloud_users_mixin() -> UsersMixin:
    """Return a :class:`UsersMixin` instance wired for Cloud mode.

    Bypasses :meth:`BitbucketClient.__init__` via ``__new__`` so no real
    HTTP / auth setup runs. The stamped ``bitbucket`` attribute is a
    :class:`MagicMock` so :attr:`~MagicMock.call_count` is available for
    the pre-HTTP assertion. The stamped ``config`` namespace provides
    the minimal attributes the :attr:`BitbucketClient.is_cloud`
    property reads (``is_cloud=True``) plus the URL / SSL attributes
    the mixin itself consults. ``workspace`` is ``None`` — ``get_user``
    is workspace-agnostic on Cloud.
    """
    mixin = UsersMixin.__new__(UsersMixin)
    mixin.bitbucket = MagicMock()
    mixin.config = SimpleNamespace(
        is_cloud=True,
        workspace=None,
        url="https://api.bitbucket.org",
        ssl_verify=True,
    )
    return mixin


@pytest.fixture
def cloud_commits_mixin() -> CommitsMixin:
    """Return a :class:`CommitsMixin` instance wired for Cloud mode.

    Mirrors :func:`cloud_users_mixin` but stamps ``workspace="my-team"``
    so the pre-HTTP guard runs before the workspace-resolution step
    even if the guard implementation reorders (the invariant under
    test is *pre-HTTP*, not *pre-workspace-resolution*). The
    :class:`MagicMock` auto-creates ``bitbucket._session`` as a child
    mock so ``_session.get.call_count`` is available for the Cloud
    unified-diff call site.
    """
    mixin = CommitsMixin.__new__(CommitsMixin)
    mixin.bitbucket = MagicMock()
    mixin.config = SimpleNamespace(
        is_cloud=True,
        workspace="my-team",
        url="https://api.bitbucket.org",
        ssl_verify=True,
    )
    return mixin


# ---------------------------------------------------------------------------
# Strategy — invalid Cloud usernames (Property 11.A)
# ---------------------------------------------------------------------------


@st.composite
def _invalid_cloud_usernames(draw: st.DrawFn) -> str:
    """Generate ``username`` values that MUST fail the Cloud shape check.

    Three branches guarantee diversity while keeping every draw
    invalid by construction:

    1. **Empty string** — rejected because neither regex matches an
       empty input (``_CLOUD_UUID_RE`` requires the 36-char UUID body;
       ``_CLOUD_ACCOUNT_ID_RE`` has ``+`` quantifier).
    2. **DC-style literal** — a realistic DC username (``first.last``,
       ``user@example.com``, ``Display Name``) drawn from a curated
       pool. These mirror the parametrised examples in
       :mod:`tests.unit.bitbucket.test_users_cloud_mode` and are the
       concrete counter-examples users hit in practice.
    3. **Random text containing at least one forbidden char** — a
       free-text draw with an injected character from
       :data:`_FORBIDDEN_USERNAME_CHARS`, guaranteeing the modern
       account-id regex cannot match. A redundant ``assume()`` at the
       end verifies neither regex matches so a future tightening of
       the regex cannot silently render the draw valid.

    The :func:`assume` call filters away the vanishingly rare case
    where a free-text draw happens to coincide with the brace-UUID
    shape (a 42-character string that starts with ``{`` and ends with
    ``}`` with a specific hex layout inside).
    """
    shape = draw(st.sampled_from(("empty", "dc_literal", "free_text")))

    if shape == "empty":
        return ""

    if shape == "dc_literal":
        return draw(
            st.sampled_from(
                (
                    "john.doe",
                    "jane@example.com",
                    "Display Name",
                    "first.last",
                    "a b c",
                    " leading_space",
                    "trailing_space ",
                    "contains/slash",
                    "contains?question",
                    "contains#hash",
                    "dot.in.name",
                    "tab\there",
                    "newline\nhere",
                )
            )
        )

    # free_text: draw arbitrary text plus at least one forbidden char.
    # Position of the forbidden char is randomised so the counter-
    # example distribution covers "starts with", "ends with" and
    # "middle-of-string" placements.
    base = draw(st.text(max_size=24))
    forbidden = draw(st.sampled_from(_FORBIDDEN_USERNAME_CHARS))
    position = draw(st.integers(min_value=0, max_value=len(base)))
    candidate = base[:position] + forbidden + base[position:]

    # Defence in depth — the construction above should already make
    # matches impossible, but ``assume()`` lets Hypothesis discard the
    # draw cleanly if a future refactor loosens the regex.
    assume(not _CLOUD_UUID_RE.match(candidate))
    assume(not _CLOUD_ACCOUNT_ID_RE.match(candidate))
    return candidate


# ---------------------------------------------------------------------------
# Strategy — invalid compare_commits refs (Property 11.B)
# ---------------------------------------------------------------------------


@st.composite
def _invalid_compare_refs(draw: st.DrawFn) -> str:
    """Generate ``from_ref`` / ``to_ref`` values that MUST fail the guard.

    Two branches match the rejection rule in ``commits.py``:

    1. **Empty string** — rejected because the Cloud
       ``{to}..{from}`` spec cannot be formed without a non-empty ref.
    2. **Contains illegal char** — a free-text draw with an injected
       character from :data:`_COMPARE_ILLEGAL_CHARS` (``/``, ``?``,
       ``#`` or any ``str.isspace()`` code point). At least one such
       character guarantees the guard rejects the ref.
    """
    if draw(st.booleans()):
        return ""

    # Non-empty base; the character set here is deliberately broad
    # (includes all printable ASCII plus some control chars) so the
    # injected illegal character drives the rejection rather than an
    # unrelated aspect of the base.
    base = draw(
        st.text(
            alphabet=st.characters(
                min_codepoint=0x20,
                max_codepoint=0x7E,
                # Exclude the illegal chars from the base so we fully
                # control which and how many illegal chars the draw
                # contains. This keeps Hypothesis's shrinker focused on
                # the injected position rather than hunting through the
                # base for a slash it already contained.
                exclude_characters="/?# ",
            ),
            min_size=1,
            max_size=32,
        )
    )
    illegal = draw(st.sampled_from(_COMPARE_ILLEGAL_CHARS))
    position = draw(st.integers(min_value=0, max_value=len(base)))
    return base[:position] + illegal + base[position:]


# Valid refs — drawn when the *other* side of the compare is invalid,
# so the test can pin down which side triggered the rejection. A ref is
# valid when it is non-empty and contains NO ``/``, ``?``, ``#`` or
# whitespace; the alphabet below is the strict complement of the
# illegal-char set.
_valid_compare_refs: st.SearchStrategy[str] = st.text(
    alphabet=string.ascii_letters + string.digits + "._-+~",
    min_size=1,
    max_size=20,
)


# ===========================================================================
# Property 11.A — UsersMixin.get_user raises invalid_target pre-HTTP (Req 13.3)
# ===========================================================================


class TestGetUserInvalidTargetProperty:
    """Property 11.A — Cloud ``get_user`` pre-HTTP ``invalid_target``.

    For any generated ``username`` that does not match either Cloud
    identifier shape, the mixin SHALL raise ``ValueError`` prefixed
    with ``"invalid_target:"`` before issuing any HTTP call.

    Validates: Requirement 13.3.
    """

    @given(username=_invalid_cloud_usernames())
    def test_invalid_username_raises_invalid_target_pre_http(
        self,
        cloud_users_mixin: UsersMixin,
        username: str,
    ) -> None:
        """Property 11.A.

        Core invariant — for any username drawn from
        :func:`_invalid_cloud_usernames`, both halves hold:

        * :class:`ValueError` is raised whose message is prefixed with
          ``"invalid_target:"`` (the contract with the server-tool
          layer's error-envelope mapper).
        * ``mixin.bitbucket.get.call_count == 0`` — the guard fires
          *before* any outbound HTTP (Requirement 19.3).

        The MagicMock's ``call_count`` is reset inside the test body
        because Hypothesis reuses the same fixture instance across
        examples; without the reset a single leaky call would
        cascade-fail every subsequent example.
        """
        # Reset the per-example counters — the fixture is shared across
        # Hypothesis draws (conftest's ``function_scoped_fixture``
        # health check suppression applies here).
        cloud_users_mixin.bitbucket.reset_mock()

        # Sanity check on the generator itself: the draw must not
        # match either Cloud shape. If this ever fails, the strategy
        # (not the mixin) has the bug, and we want a clear signal.
        assert not _CLOUD_UUID_RE.match(username)
        assert not _CLOUD_ACCOUNT_ID_RE.match(username)

        with pytest.raises(ValueError) as excinfo:
            cloud_users_mixin.get_user(username)

        # Property 11.A.(1) — ``invalid_target:`` prefix.
        assert str(excinfo.value).startswith("invalid_target:"), (
            f"username={username!r} produced message "
            f"{str(excinfo.value)!r}; expected ``invalid_target:`` prefix"
        )
        # Property 11.A.(2) — zero outbound HTTP calls.
        assert cloud_users_mixin.bitbucket.get.call_count == 0, (
            f"username={username!r} produced "
            f"{cloud_users_mixin.bitbucket.get.call_count} outbound HTTP "
            "calls; the pre-HTTP guard must not reach the network."
        )


# ===========================================================================
# Property 11.B — CommitsMixin.compare_commits raises invalid_target pre-HTTP
# ===========================================================================


class TestCompareCommitsInvalidTargetProperty:
    """Property 11.B — Cloud ``compare_commits`` pre-HTTP ``invalid_target``.

    For any generated ``from_ref`` / ``to_ref`` pair where at least one
    side is empty or contains an illegal character, the mixin SHALL
    raise ``ValueError`` prefixed with ``"invalid_target:"`` before
    issuing any HTTP call on either
    ``self.bitbucket.get`` or ``self.bitbucket._session.get``.

    Validates: Requirement 11.5.
    """

    @given(
        from_ref=_invalid_compare_refs(),
        to_ref=_valid_compare_refs,
    )
    def test_invalid_from_ref_raises_invalid_target_pre_http(
        self,
        cloud_commits_mixin: CommitsMixin,
        from_ref: str,
        to_ref: str,
    ) -> None:
        """Property 11.B.(i) — invalid ``from_ref`` with valid ``to_ref``.

        Pins down that the guard rejects a bad ``from_ref`` even when
        ``to_ref`` is pristine, matching the per-argument loop in
        ``commits._validate_compare_ref``.
        """
        cloud_commits_mixin.bitbucket.reset_mock()

        with pytest.raises(ValueError) as excinfo:
            cloud_commits_mixin.compare_commits(
                project_key="my-team",
                repo_slug="myrepo",
                from_ref=from_ref,
                to_ref=to_ref,
            )

        assert str(excinfo.value).startswith("invalid_target:"), (
            f"from_ref={from_ref!r}, to_ref={to_ref!r} produced "
            f"{str(excinfo.value)!r}; expected ``invalid_target:`` prefix"
        )
        # Pre-HTTP: both the JSON client and the raw session must be
        # untouched. The Cloud compare path uses _session.get for the
        # unified-diff body, so asserting on the wrapper alone would
        # leave a blind spot.
        assert cloud_commits_mixin.bitbucket.get.call_count == 0
        assert cloud_commits_mixin.bitbucket._session.get.call_count == 0

    @given(
        from_ref=_valid_compare_refs,
        to_ref=_invalid_compare_refs(),
    )
    def test_invalid_to_ref_raises_invalid_target_pre_http(
        self,
        cloud_commits_mixin: CommitsMixin,
        from_ref: str,
        to_ref: str,
    ) -> None:
        """Property 11.B.(ii) — valid ``from_ref`` with invalid ``to_ref``.

        Symmetric to 11.B.(i); verifies the guard is applied to BOTH
        sides of the compare spec, not just ``from``.
        """
        cloud_commits_mixin.bitbucket.reset_mock()

        with pytest.raises(ValueError) as excinfo:
            cloud_commits_mixin.compare_commits(
                project_key="my-team",
                repo_slug="myrepo",
                from_ref=from_ref,
                to_ref=to_ref,
            )

        assert str(excinfo.value).startswith("invalid_target:")
        assert cloud_commits_mixin.bitbucket.get.call_count == 0
        assert cloud_commits_mixin.bitbucket._session.get.call_count == 0

    @given(
        from_ref=_invalid_compare_refs(),
        to_ref=_invalid_compare_refs(),
    )
    def test_both_refs_invalid_raises_invalid_target_pre_http(
        self,
        cloud_commits_mixin: CommitsMixin,
        from_ref: str,
        to_ref: str,
    ) -> None:
        """Property 11.B.(iii) — both sides invalid.

        Locks in that the guard fires (and still does so pre-HTTP) even
        in the joint-invalid case; this is the most likely real-world
        failure mode when a caller forwards user input unsanitised.
        """
        cloud_commits_mixin.bitbucket.reset_mock()

        with pytest.raises(ValueError) as excinfo:
            cloud_commits_mixin.compare_commits(
                project_key="my-team",
                repo_slug="myrepo",
                from_ref=from_ref,
                to_ref=to_ref,
            )

        assert str(excinfo.value).startswith("invalid_target:")
        assert cloud_commits_mixin.bitbucket.get.call_count == 0
        assert cloud_commits_mixin.bitbucket._session.get.call_count == 0
