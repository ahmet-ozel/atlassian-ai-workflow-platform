"""Pure formatters for the ``code_change_*`` workflow family.

This module hosts two **pure** helpers used by ``AgentRunnerWorkflow``
when it produces commits and pushes branches on behalf of the bot:

* :func:`compute_branch_name` - picks the Git branch name for an
  iteration of an AI-driven code change.
* :func:`format_commit_message` - wraps an LLM-generated commit message
  with the ``[bot]`` provenance prefix and a ``Co-authored-by`` footer.

Both functions are deterministic, perform only string/regex manipulation,
and never call ``datetime``, ``random``, ``uuid`` or any I/O. This keeps
them safe to invoke directly from inside a Temporal workflow under the
replay-determinism rule.

Format references
-----------------

Branch names follow this schema::

    iter == 1 and "ai/{ISSUE_KEY}" not in existing_branches
         "ai/{ISSUE_KEY}"
    otherwise
         "ai/{ISSUE_KEY}-iter{ITER}"

This is intentionally distinct from the foundation
:func:`temporal_shared.identifiers.branch_name` formatter (which always
emits ``ai/{issue_key}/iter-{N}``); the workflows-spec branch layout is
flatter and more familiar to humans inspecting the Bitbucket UI.

Commit messages follow this schema::

    [bot] {message}
    <blank line>
    Co-authored-by: ai-bot <{bot_email}>

The provenance footer matches the bot output attribution standard.

Formatter Properties
-----------------------------------------------------------------------

* ``compute_branch_name`` output never collides with a value already in
  ``existing_branches`` for the iter=1 decision (the branch returned by
  iter=1 is guaranteed not in the input set when the bare form is
  selected). For ``iter >= 2`` the caller is expected to monotonically
  increment ``iter`` so the iter-form is fresh.
* ``format_commit_message`` output begins with the literal ``[bot] `` and
  contains ``Co-authored-by: ai-bot <{bot_email}>`` on a final line,
  separated from the body by a blank line.

"""

from __future__ import annotations

import re
from collections.abc import Iterable

from .identifiers import InvalidIssueKeyError

__all__ = [
    "BOT_COMMIT_PREFIX",
    "InvalidIterationError",
    "InvalidBotEmailError",
    "compute_branch_name",
    "format_commit_message",
]

# ---------------------------------------------------------------------------
# Constants - keep magic strings in one place
# ---------------------------------------------------------------------------

#: Prefix prepended to every bot-authored commit subject. Documented in
#: the commit subject format.
BOT_COMMIT_PREFIX: str = "[bot]"

# Issue keys must match the same shape used by
# :mod:`temporal_shared.identifiers` - uppercase project, dash, positive
# decimal issue number with no leading zero.
_ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-[1-9][0-9]*$")

# Minimal RFC-5322-ish email validation. We are not trying to accept
# every valid address in the wild; this guard exists to refuse obvious
# garbage (whitespace, missing @, control chars) so the resulting commit
# trailer is well-formed and parseable by Git tooling.
_BOT_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InvalidIterationError(ValueError):
    """Raised when an iteration number is not a positive integer."""

    def __init__(self, iteration: object) -> None:
        super().__init__(
            f"Invalid iteration {iteration!r}: must be a positive int (>= 1)"
        )
        self.iteration = iteration


class InvalidBotEmailError(ValueError):
    """Raised when ``bot_email`` is not a parseable email address.

    A well-formed trailer is required so downstream Git tooling
    (``git log --pretty``, GitHub/Bitbucket co-author attribution) can
    recognise the author and avoid mis-rendering the commit.
    """

    def __init__(self, bot_email: object) -> None:
        super().__init__(
            f"Invalid bot_email {bot_email!r}: "
            f"must match {_BOT_EMAIL_RE.pattern}"
        )
        self.bot_email = bot_email


# ---------------------------------------------------------------------------
# Internal validators
# ---------------------------------------------------------------------------


def _validate_issue_key(issue_key: object) -> str:
    if not isinstance(issue_key, str) or not _ISSUE_KEY_RE.match(issue_key):
        # Reuse the same exception type as ``identifiers.py`` so callers
        # can catch a single error class for any issue-key validation
        # failure inside ``temporal_shared``.
        raise InvalidIssueKeyError(
            issue_key if isinstance(issue_key, str) else "<non-str>"
        )
    return issue_key


def _validate_iteration(iteration: object) -> int:
    # ``bool`` is a subclass of ``int`` in Python - exclude it explicitly
    # so ``compute_branch_name(..., True, ...)`` does not silently behave
    # like ``iter == 1``.
    if (
        not isinstance(iteration, int)
        or isinstance(iteration, bool)
        or iteration < 1
    ):
        raise InvalidIterationError(iteration)
    return iteration


def _validate_bot_email(bot_email: object) -> str:
    if not isinstance(bot_email, str) or not _BOT_EMAIL_RE.match(bot_email):
        raise InvalidBotEmailError(bot_email)
    return bot_email


# ---------------------------------------------------------------------------
# compute_branch_name
# ---------------------------------------------------------------------------


def compute_branch_name(
    issue_key: str,
    iteration: int,
    existing_branches: Iterable[str],
) -> str:
    """Return the Git branch name for an iteration of a code change.

    Decision rule::

        if iteration == 1 and "ai/{issue_key}" not in existing_branches:
            return "ai/{issue_key}"
        return "ai/{issue_key}-iter{iteration}"

    For ``iteration == 1`` the function prefers the bare ``ai/{issue_key}``
    branch and only falls back to the iter-suffixed form if the bare
    name is already taken. For ``iteration >= 2`` the iter-suffixed form
    is always returned; the caller is responsible for monotonically
    incrementing ``iteration`` so the result is fresh (the workflow's
    iter counter does this naturally - see
    :func:`temporal_shared.iteration.should_advance_iter`).

    Parameters
    ----------
    issue_key:
        Jira issue key, e.g. ``"PAY-4211"``. Must match
        ``^[A-Z][A-Z0-9_]+-[1-9][0-9]*$``.
    iteration:
        Positive integer iteration number (1-based). The first
        iteration is 1.
    existing_branches:
        Any iterable of branch names that already exist on the remote.
        Only consulted when ``iteration == 1`` (the only step where the
        decision depends on remote state).

    Returns
    -------
    str
        Either ``"ai/{issue_key}"`` (iter 1, bare slot free) or
        ``"ai/{issue_key}-iter{iteration}"`` (every other case).

    Raises
    ------
    InvalidIssueKeyError
        If *issue_key* does not match the pinned format.
    InvalidIterationError
        If *iteration* is not a positive integer.

    Examples
    --------
    >>> compute_branch_name("PAY-4211", 1, [])
    'ai/PAY-4211'
    >>> compute_branch_name("PAY-4211", 1, ["ai/PAY-4211"])
    'ai/PAY-4211-iter1'
    >>> compute_branch_name("PAY-4211", 2, [])
    'ai/PAY-4211-iter2'
    >>> compute_branch_name("PAY-4211", 3, ["ai/PAY-4211", "ai/PAY-4211-iter2"])
    'ai/PAY-4211-iter3'
    """
    issue_key = _validate_issue_key(issue_key)
    iteration = _validate_iteration(iteration)

    bare = f"ai/{issue_key}"

    # ``existing_branches`` may be any iterable (list, tuple, set,
    # generator).  Materialise it to a frozenset *only* when iter == 1,
    # because that is the only case where membership is actually
    # consulted - for iter >= 2 we skip the work entirely.
    if iteration == 1:
        existing_set = frozenset(existing_branches)
        if bare not in existing_set:
            return bare

    return f"{bare}-iter{iteration}"


# ---------------------------------------------------------------------------
# format_commit_message
# ---------------------------------------------------------------------------


def format_commit_message(
    message: str,
    issue_key: str,
    iteration: int,
    bot_email: str,
) -> str:
    """Wrap a commit message with bot provenance.

    Output shape::

        [bot] {message}

        Co-authored-by: ai-bot <{bot_email}>

    The first line is the original ``message`` prefixed with the literal
    ``[bot] `` token (note the trailing space) so reviewers can identify
    bot-authored commits at a glance in the Bitbucket UI. The blank line
    separates the subject/body from the trailers per Git convention so
    ``git log --pretty=%(trailers)`` recognises the ``Co-authored-by``
    line and renders the bot as a co-author on Bitbucket / GitHub.

    The ``issue_key`` and ``iteration`` parameters are validated for
    consistency with the surrounding ``code_change_*`` workflow even
    though they do not appear verbatim in the trailer; the LLM prompt is
    expected to embed the issue key in *message* itself, and rejecting
    invalid identifiers here surfaces caller bugs early.

    Parameters
    ----------
    message:
        The commit message body produced by the LLM. Trailing whitespace
        is stripped so the inserted blank line is unambiguous, but the
        function does **not** otherwise rewrite the body.
    issue_key:
        Jira issue key the commit relates to. Validated for shape.
    iteration:
        Positive iteration number. Validated as ``>= 1``.
    bot_email:
        The bot account's email address used in the ``Co-authored-by``
        trailer. Must be a parseable email (``foo@bar.tld``); the bot
        identity itself is hard-coded as ``ai-bot`` to match the
        attribution standard.

    Returns
    -------
    str
        A commit message string ready to pass to
        ``git commit -m`` / Bitbucket ``commit`` API.

    Raises
    ------
    TypeError
        If *message* is not a string (we refuse implicit ``str()``
        coercion to avoid eating LLM output bugs).
    InvalidIssueKeyError
        If *issue_key* is malformed.
    InvalidIterationError
        If *iteration* is not a positive integer.
    InvalidBotEmailError
        If *bot_email* is not a parseable email address.

    Examples
    --------
    >>> format_commit_message(
    ...     "fix payment retry logic",
    ...     "PAY-4211",
    ...     1,
    ...     "ai-bot@company.com",
    ... )
    '[bot] fix payment retry logic\\n\\nCo-authored-by: ai-bot <ai-bot@company.com>'
    """
    if not isinstance(message, str):
        raise TypeError(
            f"message must be str, got {type(message).__name__}"
        )

    _validate_issue_key(issue_key)
    _validate_iteration(iteration)
    _validate_bot_email(bot_email)

    # Normalise trailing whitespace / newlines so the blank line that
    # introduces the trailer block is always exactly one empty line.
    # Internal whitespace and intentional structure inside the body are
    # preserved verbatim.
    body = message.rstrip()

    return f"{BOT_COMMIT_PREFIX} {body}\n\nCo-authored-by: ai-bot <{bot_email}>"
