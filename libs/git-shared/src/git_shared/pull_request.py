"""Pull-request opener protocol + Bitbucket adapter.

The :class:`PromptsGitRouter` decouples "open a PR" from the concrete
upstream by talking to a :class:`PullRequestOpener` interface. Two
implementations ship in-tree:

* :class:`BitbucketPullRequestOpener` — wraps an
  ``mcp_client``-style ``bitbucket_create_pull_request_cloud`` callable
  (the foundation library's MCP client) so the actual HTTP call goes
  through the existing capability-gated tool dispatch surface.
* :class:`InMemoryPullRequestOpener` — used by unit tests to capture
  the call arguments without touching Bitbucket; lives in the test
  module rather than here to keep production imports lean.

The interface is async because the Bitbucket call goes over HTTP. The
router awaits the opener inside the FastAPI handler — no
``run_in_executor`` indirection needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping, Protocol, runtime_checkable

from .errors import MergeConflictError, PullRequestError


@dataclass(frozen=True)
class PullRequestRef:
    """Result of :meth:`PullRequestOpener.open`.

    Carries enough metadata for the router to emit a
    ``prompt_pr_opened`` audit event and to surface a clickable link
    in the response payload.

    Attributes:
        provider: ``"bitbucket-cloud"`` or ``"bitbucket-dc"``.
        id: PR identifier as returned by the upstream provider
            (string because Bitbucket Cloud uses incrementing ints
            but DC uses larger types and we keep the wire shape
            uniform).
        url: Absolute https:// link to the PR.
        source_branch: Source branch name.
        target_branch: Target branch name.
    """

    provider: str
    id: str
    url: str
    source_branch: str
    target_branch: str


@runtime_checkable
class PullRequestOpener(Protocol):
    """Open a PR from ``source`` to ``target``.

    Implementations MUST be idempotent on title / description: the
    router may retry the call after a transient network failure and
    the upstream is expected to return the existing PR rather than
    creating a duplicate (Bitbucket Cloud already behaves this way).
    """

    async def open(
        self,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> PullRequestRef:
        """Open the PR and return a reference."""

        ...


# Type alias for the callable shape that ``BitbucketPullRequestOpener``
# wraps. Defined at module scope so callers can spell it out in tests.
BitbucketCreatePrCallable = Callable[
    [Mapping[str, object]],
    Awaitable[Mapping[str, object]],
]


class BitbucketPullRequestOpener:
    """Adapter that routes :class:`PullRequestOpener.open` to Bitbucket.

    The actual HTTP call is delegated to a callable matching the
    ``mcp_client`` foundation lib's ``bitbucket_create_pull_request_cloud``
    tool signature: an async function that accepts a single mapping of
    arguments and returns the upstream JSON payload.

    Args:
        invoker: Async callable that performs the upstream call. The
            mapping it receives uses Bitbucket's own field names
            (``title``, ``description``, ``source.branch.name``,
            ``destination.branch.name``, …). The wrapper packs the
            payload internally so callers of ``open()`` use the
            uniform :class:`PullRequestOpener` shape.
        workspace: Bitbucket Cloud workspace slug (eg.
            ``"acme-payments"``). Required for the tool URL.
        repo_slug: Repository slug within the workspace.
        provider_label: Human-readable provider name used in the
            returned :class:`PullRequestRef`. Defaults to
            ``"bitbucket-cloud"``; deployments using Bitbucket DC
            override this and inject an appropriate ``invoker``.
    """

    def __init__(
        self,
        *,
        invoker: BitbucketCreatePrCallable,
        workspace: str,
        repo_slug: str,
        provider_label: str = "bitbucket-cloud",
    ) -> None:
        if not workspace:
            raise ValueError("workspace must not be empty")
        if not repo_slug:
            raise ValueError("repo_slug must not be empty")
        self._invoker = invoker
        self._workspace = workspace
        self._repo_slug = repo_slug
        self._provider_label = provider_label

    async def open(
        self,
        *,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> PullRequestRef:
        """Open a PR via the injected ``invoker``.

        Returns:
            :class:`PullRequestRef` populated from the upstream
            payload (``id``, ``links.html.href``).

        Raises:
            MergeConflictError: When the upstream replies with a
                conflict signal (Bitbucket Cloud returns HTTP 409 with
                ``{"error": {"message": "There are conflicts..."}}``).
            PullRequestError: For any other upstream failure.
        """

        payload = {
            "workspace": self._workspace,
            "repo_slug": self._repo_slug,
            "title": title,
            "description": description,
            "source": {"branch": {"name": source_branch}},
            "destination": {"branch": {"name": target_branch}},
        }

        try:
            response = await self._invoker(payload)
        except _MergeConflictSignalError as exc:
            raise MergeConflictError(
                f"merge conflict between {source_branch!r} and "
                f"{target_branch!r}: {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - upstream failure surface
            raise PullRequestError(
                f"failed to open PR via {self._provider_label}: {exc}"
            ) from exc

        try:
            pr_id = str(response["id"])
        except (KeyError, TypeError) as exc:
            raise PullRequestError(
                f"upstream PR response missing 'id': {response!r}"
            ) from exc

        url = _extract_pr_url(response)

        return PullRequestRef(
            provider=self._provider_label,
            id=pr_id,
            url=url,
            source_branch=source_branch,
            target_branch=target_branch,
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _MergeConflictSignalError(Exception):
    """Sentinel raised by ``invoker`` adapters that detect a 409 conflict.

    Concrete invokers that talk to Bitbucket directly should raise
    this when the upstream payload says conflict; the wrapper then
    re-raises it as :class:`MergeConflictError`. The split keeps the
    public exception type stable while still letting invokers signal
    a conflict without shipping the full error hierarchy.
    """


def _extract_pr_url(response: Mapping[str, object]) -> str:
    """Pull the HTTP link out of a Bitbucket-shaped JSON payload.

    The Cloud and DC payloads differ slightly:

    * Cloud: ``response["links"]["html"]["href"]``
    * DC:    ``response["links"]["self"][0]["href"]``

    We try both shapes and fall back to an empty string so a missing
    URL never raises — the router treats an empty URL as
    "PR opened, link unavailable" and emits an audit warning rather
    than failing the request.
    """

    links = response.get("links")
    if not isinstance(links, Mapping):
        return ""

    html = links.get("html")
    if isinstance(html, Mapping):
        href = html.get("href")
        if isinstance(href, str):
            return href

    self_links = links.get("self")
    if isinstance(self_links, list) and self_links:
        first = self_links[0]
        if isinstance(first, Mapping):
            href = first.get("href")
            if isinstance(href, str):
                return href

    return ""


__all__ = [
    "BitbucketCreatePrCallable",
    "BitbucketPullRequestOpener",
    "PullRequestOpener",
    "PullRequestRef",
]
