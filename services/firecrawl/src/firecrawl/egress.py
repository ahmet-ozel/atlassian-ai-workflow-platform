"""Egress allowlist enforcement.

This module is **the** authoritative implementation of the host-allowlist
predicate that the firecrawl wrapper consults before performing any external
HTTP fetch. The function surface is intentionally small and side-effect-free
so it can be exercised by Hypothesis property tests and reused by the FastAPI
app and any future callers (e.g. an ``automation-service`` pre-flight check).

Design contract
---------------
* The allowlist is a comma-separated list of *host suffixes*. An empty or
  whitespace-only value DENIES all external hosts (closed by default).
* Suffix matching honours DNS label boundaries: ``example.com`` matches
  ``example.com`` and ``api.example.com`` but NOT ``barexample.com``. This
  prevents trivial spoofing via a confusable parent domain.
* Comparison is case-insensitive (DNS hostnames are case-insensitive per
  RFC 1035 §2.3.3) and trims surrounding whitespace.
* IPv4 / IPv6 / port-suffixed hosts pass through unchanged: matching is on
  the bare ``hostname`` portion of the URL.
* Inputs that fail to parse as valid URLs are denied with reason
  ``invalid_url`` rather than raising, so callers can return a clean HTTP 400
  / 403 without dropping the request on the floor.

The structured ``EgressDecision`` carries enough diagnostic data
(``host``, ``reason``, ``audit_action``) for the FastAPI layer to log and
emit a metric without re-parsing the URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

__all__ = [
    "EgressDecision",
    "EgressDenied",
    "EgressVerdict",
    "parse_allowlist",
    "is_host_allowed",
    "decide_egress",
]

#: Stable identifier emitted on the audit / log path when a request is
#: refused. Tests assert this exact string appears in the structured log
#: record.
EGRESS_DENIED_AUDIT_ACTION = "egress_denied"

#: Counterpart for permitted requests. Useful for symmetry in dashboards but
#: not required by the core allowlist behavior.
EGRESS_ALLOWED_AUDIT_ACTION = "egress_allowed"


EgressVerdict = Literal["allowed", "denied"]


class EgressDenied(Exception):
    """Raised by callers that prefer exception-driven control flow.

    The FastAPI handler in :mod:`firecrawl.app` does NOT raise this — it
    inspects the :class:`EgressDecision` directly so the structured 403
    response and the audit metric stay in lock-step. The exception exists
    for non-HTTP callers (e.g. background workers) that want to short-circuit
    on denial.
    """

    def __init__(self, decision: "EgressDecision") -> None:
        super().__init__(f"egress_denied: {decision.host!r} (reason={decision.reason})")
        self.decision = decision


@dataclass(frozen=True)
class EgressDecision:
    """Outcome of an allowlist check.

    Attributes
    ----------
    verdict
        ``"allowed"`` or ``"denied"``. The single source of truth for the
        FastAPI ``403 vs forward`` branch.
    host
        Lower-cased hostname extracted from the input URL. Empty string when
        the URL failed to parse (``reason == "invalid_url"``).
    url
        The original URL as supplied by the caller, untouched. Useful for
        the structured log record so operators can reproduce the request.
    reason
        Short machine-readable token explaining the decision. One of
        ``"allowlisted"``, ``"not_in_allowlist"``, ``"empty_allowlist"``,
        ``"invalid_url"``, ``"missing_host"``.
    audit_action
        ``"egress_allowed"`` or ``"egress_denied"``. The exact string that
        SHALL appear in audit / log records.
    """

    verdict: EgressVerdict
    host: str
    url: str
    reason: str
    audit_action: str


def parse_allowlist(raw: str | None) -> tuple[str, ...]:
    """Normalise the comma-separated allowlist env value.

    The function is intentionally lenient about the exact shape of the env
    string — operators often paste lists with stray spaces or trailing
    commas. We strip whitespace, drop empty entries, lower-case each host
    (DNS is case-insensitive), and de-duplicate while preserving the
    original order so log output is stable.

    An ``None`` or whitespace-only input yields an empty tuple, which the
    matcher interprets as "deny everything".
    """

    if not raw:
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for raw_entry in raw.split(","):
        host = raw_entry.strip().lower()
        if not host or host in seen:
            continue
        seen.add(host)
        out.append(host)
    return tuple(out)


def is_host_allowed(host: str, allowlist: tuple[str, ...]) -> bool:
    """Return ``True`` iff ``host`` matches any suffix in ``allowlist``.

    Matching honours DNS label boundaries: a list entry of ``example.com``
    matches the host ``example.com`` exactly and any subdomain such as
    ``api.example.com`` (where the entry is preceded by a dot in the host),
    but NOT ``barexample.com`` (no boundary).

    The empty allowlist always returns ``False`` — the closed-by-default
    posture is a hard requirement for this wrapper.
    """

    if not host or not allowlist:
        return False
    h = host.strip().lower()
    if not h:
        return False
    for entry in allowlist:
        if h == entry:
            return True
        # Subdomain match: `h` must end with `.<entry>` so that a list
        # entry of ``example.com`` does not accidentally allow the
        # confusable parent ``barexample.com``.
        if h.endswith("." + entry):
            return True
    return False


def decide_egress(url: str, allowlist: tuple[str, ...]) -> EgressDecision:
    """Compute the allow/deny verdict for ``url`` against ``allowlist``.

    The function never raises on a malformed URL — the caller can trust
    the returned :class:`EgressDecision` and translate it directly into
    an HTTP response or worker-side ``EgressDenied`` exception.
    """

    if url is None:  # type: ignore[unreachable]  # defensive
        return EgressDecision(
            verdict="denied",
            host="",
            url="",
            reason="invalid_url",
            audit_action=EGRESS_DENIED_AUDIT_ACTION,
        )

    parsed = urlparse(url.strip()) if isinstance(url, str) else None
    if parsed is None or not parsed.scheme or parsed.scheme.lower() not in ("http", "https"):
        return EgressDecision(
            verdict="denied",
            host="",
            url=url if isinstance(url, str) else "",
            reason="invalid_url",
            audit_action=EGRESS_DENIED_AUDIT_ACTION,
        )

    host = (parsed.hostname or "").lower()
    if not host:
        return EgressDecision(
            verdict="denied",
            host="",
            url=url,
            reason="missing_host",
            audit_action=EGRESS_DENIED_AUDIT_ACTION,
        )

    if not allowlist:
        return EgressDecision(
            verdict="denied",
            host=host,
            url=url,
            reason="empty_allowlist",
            audit_action=EGRESS_DENIED_AUDIT_ACTION,
        )

    if is_host_allowed(host, allowlist):
        return EgressDecision(
            verdict="allowed",
            host=host,
            url=url,
            reason="allowlisted",
            audit_action=EGRESS_ALLOWED_AUDIT_ACTION,
        )

    return EgressDecision(
        verdict="denied",
        host=host,
        url=url,
        reason="not_in_allowlist",
        audit_action=EGRESS_DENIED_AUDIT_ACTION,
    )
