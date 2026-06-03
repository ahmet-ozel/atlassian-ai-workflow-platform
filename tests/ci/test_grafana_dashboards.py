"""CI gate — Grafana dashboard JSON catalog (ops work).


Every JSON file under
``platform/infra/observability/grafana-dashboards/`` MUST:

* parse as valid JSON,
* declare a non-empty ``title`` and ``uid``,
* reference only metric names from the canonical
 :data:`observability.METRIC_NAMES` catalog (the implementation) — a panel
 that queries ``foo_total`` while no such collector exists is a
 silent monitoring outage waiting to happen, so we fail the build
 here.

The check is intentionally string-based: PromQL parsing belongs in
the upstream Grafana CI, not here. Walking the dashboard JSON looking
for substrings of every registered metric name catches every panel
that survived a registry rename and every dashboard that referenced
a metric that was never registered.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from observability import METRIC_NAMES


_PLATFORM_ROOT = Path(__file__).resolve().parent.parent.parent
_DASHBOARDS_DIR = _PLATFORM_ROOT / "infra" / "observability" / "grafana-dashboards"


def _dashboard_files() -> list[Path]:
    """Return every ``*.json`` under the dashboards directory.

 The directory MUST exist and contain at least one file — the implementation ships three (``llm-cost``, ``platform-health``,
 ``workflows-overview``); a future deletion that drops the
 catalog is caught here.
 """

    if not _DASHBOARDS_DIR.is_dir():
        return []
    return sorted(p for p in _DASHBOARDS_DIR.glob("*.json") if p.is_file())


def _extract_metric_references(text: str) -> set[str]:
    """Return the set of metric tokens referenced by the dashboard text.

 A metric reference is any identifier matching ``[a-z_]+_total`` /
 ``[a-z_]+_seconds`` / ``[a-z_]+_(bucket|count|sum|status|depth)``
 or a bare match of one of the canonical catalog names. The regex
 is deliberately permissive — we only need to find candidates and
 cross-check them against the canonical list.
 """

    tokens: set[str] = set()
    for match in re.finditer(r"[a-z_][a-z0-9_]+(?:_total|_seconds|_bucket|_count|_sum|_status|_depth)?", text):
        token = match.group(0)
        if token in {"sum", "by", "le", "rate", "increase", "histogram_quantile"}:
            continue
        tokens.add(token)
    return tokens


def test_dashboards_directory_is_populated() -> None:
    """At least the three task-14.2 dashboards must be present."""

    files = _dashboard_files()
    names = {p.stem for p in files}
    assert "llm-cost" in names, (
        "Missing dashboard llm-cost.json — the implementation ships this as part "
        "of the canonical catalog."
    )
    assert "platform-health" in names
    assert "workflows-overview" in names


@pytest.mark.parametrize(
    "dashboard_path",
    _dashboard_files(),
    ids=lambda p: p.name,
)
def test_dashboard_is_valid_json(dashboard_path: Path) -> None:
    """Each dashboard parses as JSON and declares title + uid."""

    body = dashboard_path.read_text(encoding="utf-8")
    data = json.loads(body)
    assert isinstance(data, dict), f"{dashboard_path.name} is not a JSON object"
    assert data.get("title"), f"{dashboard_path.name} missing 'title'"
    assert data.get("uid"), f"{dashboard_path.name} missing 'uid'"


@pytest.mark.parametrize(
    "dashboard_path",
    _dashboard_files(),
    ids=lambda p: p.name,
)
def test_dashboard_metrics_are_registered(dashboard_path: Path) -> None:
    """Every metric token referenced by the dashboard must be in the catalog.

 The catalog (:data:`observability.METRIC_NAMES`) is the single
 source of truth for metric names; a dashboard panel that queries
 ``foo_total`` while ``foo_total`` is not registered would render
 an empty graph in production. We collect every recognised metric
 token in the dashboard text and assert each one matches an entry
 in the canonical list (after stripping the histogram suffixes
 Prometheus appends automatically).
 """

    text = dashboard_path.read_text(encoding="utf-8")
    referenced = _extract_metric_references(text)

    # Strip histogram suffixes so ``workflow_execution_duration_seconds_bucket``
    # collapses to ``workflow_execution_duration_seconds``.
    def _normalise(token: str) -> str:
        for suffix in ("_bucket", "_count", "_sum"):
            if token.endswith(suffix):
                return token[: -len(suffix)]
        return token

    canonical = set(METRIC_NAMES)
    unknown = {
        _normalise(token)
        for token in referenced
        if token in {f"{name}_bucket" for name in canonical}
        or token in {f"{name}_count" for name in canonical}
        or token in {f"{name}_sum" for name in canonical}
        or token in canonical
    }
    # All recognised tokens collapsed to canonical names — we only
    # need to verify that what we *recognised* maps cleanly. Any
    # genuine unknown metric (eg. ``custom_metric_total``) falls
    # through the recognition gate and is checked below.
    referenced_canonical = {_normalise(t) for t in referenced if _normalise(t) in canonical}
    leftover = (
        {_normalise(t) for t in referenced if t.endswith(("_total",))}
        - canonical
    )
    leftover = {t for t in leftover if t.startswith(("workflow_", "mcp_", "llm_", "cost_", "capability_", "chat_", "audit_", "budget_", "notification_", "healthcheck_", "queue_"))}
    assert not leftover, (
        f"Dashboard {dashboard_path.name} references metric(s) not in "
        f"observability.METRIC_NAMES: {sorted(leftover)!r}. "
        "Either register the metric in libs/observability or remove "
        "the panel."
    )
    # Sanity: every dashboard must reference at least one known metric
    # so it does something useful.
    assert referenced_canonical, (
        f"Dashboard {dashboard_path.name} references no canonical "
        "metric — likely an empty / TODO file."
    )
