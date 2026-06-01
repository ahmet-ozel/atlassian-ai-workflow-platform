"""Cloud-branch unit tests for :class:`CodeInsightsMixin`.

These tests cover the Cloud side of the Bitbucket code-insights mixin
introduced by task 12.1 of the ``bitbucket-cloud-dc-parity`` spec
(Requirements 12.1, 12.2, 12.3, 19.1, 19.2).

For each method that carries an ``if self.is_cloud:`` branch
(``list_code_insight_reports``, ``get_code_insight_report``,
``create_or_update_code_insight_report``, ``delete_code_insight_report``,
``list_code_insight_annotations``, ``bulk_create_code_insight_annotations``,
``delete_code_insight_annotations``), one happy-path test verifies that the
outbound URL matches the Cloud 2.0 template
``/2.0/repositories/{workspace}/{repo_slug}/commit/{sha}/reports[/{report_id}][/annotations[/{external_id}]]``
(Req 12.1, 12.2, 12.3). Additional tests confirm Cloud-specific body
shape adjustments:

* ``create_or_update_code_insight_report`` translates the DC ``logoUrl``
  field to Cloud's ``logo_url`` while leaving every other shared field
  (``title``, ``details``, ``result``, ``reporter``, ``link``, ``data``)
  untouched (Req 12.2).
* ``bulk_create_code_insight_annotations`` unwraps DC's
  ``{"annotations": [...]}`` body into a bare Cloud array for the
  bulk-upload endpoint (Req 12.3).
* ``delete_code_insight_annotations`` with ``external_ids=None`` has no
  Cloud bulk-delete endpoint, so the Cloud branch lists the report's
  annotations first and issues one ``DELETE`` per annotation — the
  ``external_id`` / ``uuid`` taken from the Cloud list envelope (Req
  12.3).

The mixin's DC branches are intentionally **not** touched here — those
paths are locked byte-for-byte by Requirement 19.2 / 23.2. The tests
below stamp ``is_cloud=True`` onto a bypassed :class:`CodeInsightsMixin`
instance and inspect what the Cloud branch does.

Test pattern (mirrors :mod:`test_commit_comments_cloud_mode` and
:mod:`test_branches_cloud_mode`):

* Bypass :meth:`CodeInsightsMixin.__init__` via
  :meth:`CodeInsightsMixin.__new__` to avoid the live-auth / live-HTTP
  constructor (the mixin inherits from :class:`BitbucketClient`).
* Stamp ``mixin.bitbucket = MagicMock()`` so ``get`` / ``post`` / ``put``
  / ``delete`` are driven by :class:`MagicMock`.
* Stamp a :class:`SimpleNamespace` on ``mixin.config`` with
  ``is_cloud=True``, ``workspace="my-team"``, plus the minimal URL / SSL
  attributes the :attr:`BitbucketClient.is_cloud` property reads.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call

import pytest

from mcp_atlassian.bitbucket.code_insights import CodeInsightsMixin


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def cloud_code_insights_mixin() -> CodeInsightsMixin:
    """Return a :class:`CodeInsightsMixin` instance wired for Cloud mode.

    ``CodeInsightsMixin.__new__`` bypasses
    :meth:`BitbucketClient.__init__`, so no real HTTP / auth setup runs.
    The stamped ``bitbucket`` mock stands in for the
    ``atlassian.Bitbucket`` client; the stamped ``config`` namespace
    carries just enough attributes for the
    :attr:`BitbucketClient.is_cloud` property and the Cloud branches of
    the mixin methods (``config.workspace`` in particular) to work.
    """
    mixin = CodeInsightsMixin.__new__(CodeInsightsMixin)
    mixin.bitbucket = MagicMock()
    mixin.config = SimpleNamespace(
        is_cloud=True,
        workspace="my-team",
        url="https://api.bitbucket.org",
        ssl_verify=True,
        timeout=75,
    )
    return mixin


def _cloud_report_payload(
    report_key: str,
    *,
    title: str = "Security Scan",
    result: str = "PASSED",
) -> dict:
    """Fabricate a Cloud 2.0 Code Insights report dict.

    Cloud reports are close to DC in shape so the mixin's Cloud branch
    passes responses through without normalization. The synthesized
    payload covers the fields the tests assert on.
    """
    return {
        "uuid": f"{{{report_key}-uuid}}",
        "key": report_key,
        "title": title,
        "result": result,
        "reporter": "my-ci",
        "type": "report",
    }


def _cloud_annotation_payload(
    external_id: str,
    *,
    message: str = "Hardcoded secret",
    severity: str = "HIGH",
) -> dict:
    """Fabricate a Cloud 2.0 Code Insights annotation dict.

    Cloud list envelopes expose both ``external_id`` and ``uuid`` on
    each annotation; the mixin's delete-all Cloud branch prefers
    ``external_id`` and falls back to ``uuid``.
    """
    return {
        "external_id": external_id,
        "uuid": f"{{{external_id}-uuid}}",
        "message": message,
        "severity": severity,
        "annotation_type": "VULNERABILITY",
    }


# ===========================================================================
# list_code_insight_reports (Req 12.2)
# ===========================================================================


class TestListCodeInsightReportsCloud:
    """``list_code_insight_reports`` Cloud branch — Requirement 12.2."""

    def test_issues_cloud_reports_url(
        self, cloud_code_insights_mixin: CodeInsightsMixin
    ) -> None:
        """Happy path: single-page Cloud envelope, verify URL prefix.

        Cloud termination is ``next=None`` (Req 7.3); the paged-results
        helper strips the ``next`` key and returns a flat list of
        report dicts as the downstream server-tool layer expects.
        """
        cloud_code_insights_mixin.bitbucket.get.return_value = {
            "values": [
                _cloud_report_payload("my-ci.sec", result="PASSED"),
                _cloud_report_payload("my-ci.cov", result="FAILED"),
            ],
            "next": None,
            "page": 1,
            "pagelen": 25,
            "size": 2,
        }

        result = cloud_code_insights_mixin.list_code_insight_reports(
            project_key="my-team",
            repo_slug="myrepo",
            commit_id="abc123def",
        )

        cloud_code_insights_mixin.bitbucket.get.assert_called_once()
        (called_url,), _kwargs = cloud_code_insights_mixin.bitbucket.get.call_args
        assert (
            called_url
            == "/2.0/repositories/my-team/myrepo/commit/abc123def/reports"
        )
        # The paged-results helper returns a flat list without the ``next`` key.
        assert [r["key"] for r in result] == ["my-ci.sec", "my-ci.cov"]
        assert [r["result"] for r in result] == ["PASSED", "FAILED"]

    def test_uses_config_workspace_when_project_key_empty(
        self, cloud_code_insights_mixin: CodeInsightsMixin
    ) -> None:
        """Workspace fallback (Req 2.5) routes through ``config.workspace``.

        When the caller passes an empty ``project_key`` the Cloud branch
        resolves the workspace from ``config.workspace`` and still emits
        ``/2.0/repositories/my-team/...``.
        """
        cloud_code_insights_mixin.bitbucket.get.return_value = {
            "values": [],
            "next": None,
        }

        cloud_code_insights_mixin.list_code_insight_reports(
            project_key="",
            repo_slug="r",
            commit_id="sha1",
        )

        (called_url,), _ = cloud_code_insights_mixin.bitbucket.get.call_args
        assert (
            called_url
            == "/2.0/repositories/my-team/r/commit/sha1/reports"
        )


# ===========================================================================
# get_code_insight_report (Req 12.2)
# ===========================================================================


class TestGetCodeInsightReportCloud:
    """``get_code_insight_report`` Cloud branch — Requirement 12.2."""

    def test_gets_cloud_report_by_key_url(
        self, cloud_code_insights_mixin: CodeInsightsMixin
    ) -> None:
        """``GET /2.0/repositories/{ws}/{slug}/commit/{sha}/reports/{key}``.

        The Cloud report-key path segment is the DC ``report_key`` value
        passed through verbatim (Req 12.2). Cloud returns a report dict
        which the mixin forwards unchanged.
        """
        payload = _cloud_report_payload("my-ci.sec")
        cloud_code_insights_mixin.bitbucket.get.return_value = payload

        result = cloud_code_insights_mixin.get_code_insight_report(
            project_key="my-team",
            repo_slug="myrepo",
            commit_id="abc123def",
            report_key="my-ci.sec",
        )

        cloud_code_insights_mixin.bitbucket.get.assert_called_once()
        call_args = cloud_code_insights_mixin.bitbucket.get.call_args
        assert call_args.args == (
            "/2.0/repositories/my-team/myrepo/commit/abc123def/reports/my-ci.sec",
        )
        assert result is payload


# ===========================================================================
# create_or_update_code_insight_report (Req 12.2 + DC→Cloud body translation)
# ===========================================================================


class TestCreateOrUpdateCodeInsightReportCloud:
    """``create_or_update_code_insight_report`` Cloud branch — Req 12.2."""

    def _setup_session_put(
        self, mixin: CodeInsightsMixin, payload: dict
    ) -> None:
        """Wire ``bitbucket._session.put`` to return a fake response."""
        import json as _json
        from unittest.mock import MagicMock

        fake_resp = MagicMock()
        fake_resp.raise_for_status = MagicMock()
        fake_resp.json.return_value = payload
        mixin.bitbucket._session.put.return_value = fake_resp

    def test_puts_cloud_report_url_with_snake_case_logo_url(
        self, cloud_code_insights_mixin: CodeInsightsMixin
    ) -> None:
        """``PUT .../reports/{key}`` with Cloud-shaped body via session.

        The Cloud branch bypasses ``atlassian-python-api``'s ``put`` helper
        (which appends a trailing slash causing HTTP 400) and uses the
        underlying session directly. The agent-facing parameter stays
        ``logo_url`` (Req 5.4); the Cloud branch must emit it as
        ``logo_url`` in the body (Cloud snake_case) and NOT as the DC
        ``logoUrl`` spelling.
        """
        import json as _json

        returned = _cloud_report_payload("my-ci.sec", result="FAILED")
        self._setup_session_put(cloud_code_insights_mixin, returned)

        result = cloud_code_insights_mixin.create_or_update_code_insight_report(
            project_key="my-team",
            repo_slug="myrepo",
            commit_id="abc123",
            report_key="my-ci.sec",
            title="Security Scan",
            details="12 vulnerabilities",
            result="FAILED",
            reporter="my-ci",
            link="https://ci.example.com/runs/42",
            logo_url="https://ci.example.com/logo.png",
            data=[{"title": "Issues", "type": "NUMBER", "value": 12}],
        )

        cloud_code_insights_mixin.bitbucket._session.put.assert_called_once()
        call_args = cloud_code_insights_mixin.bitbucket._session.put.call_args
        called_url = call_args.args[0]
        assert called_url.endswith(
            "/2.0/repositories/my-team/myrepo/commit/abc123/reports/my-ci.sec"
        )
        # Body is passed as json= kwarg (requests serialises it with correct Content-Type).
        sent_body = call_args.kwargs.get("json")
        assert sent_body is not None, f"json kwarg not found in call_args: {call_args}"
        assert sent_body["title"] == "Security Scan"
        assert sent_body["details"] == "12 vulnerabilities"
        assert sent_body["result"] == "FAILED"
        assert sent_body["logo_url"] == "https://ci.example.com/logo.png"
        assert "logoUrl" not in sent_body
        # Response passes through unchanged.
        assert result is returned

    def test_omits_optional_fields_when_not_provided(
        self, cloud_code_insights_mixin: CodeInsightsMixin
    ) -> None:
        """Minimal call ships ``{"title": ..., "details": ""}`` on the Cloud branch.

        Cloud's report endpoint requires ``details`` to be present in the
        request body (returns HTTP 400 when absent). When the caller omits
        ``details`` (leaves it as ``None``), the Cloud branch substitutes an
        empty string so the PUT does not fail. Every other optional parameter
        (``result``, ``reporter``, ``link``, ``logo_url``, ``data``) is still
        omitted from the Cloud request body when the caller leaves it as
        ``None``, so Cloud does not receive ``null`` values for fields it
        does not expect.
        """
        import json as _json

        returned = _cloud_report_payload("my-ci.sec")
        self._setup_session_put(cloud_code_insights_mixin, returned)

        cloud_code_insights_mixin.create_or_update_code_insight_report(
            project_key="my-team",
            repo_slug="myrepo",
            commit_id="abc123",
            report_key="my-ci.sec",
            title="Security Scan",
        )

        call_args = cloud_code_insights_mixin.bitbucket._session.put.call_args
        sent_body = call_args.kwargs.get("json")
        assert sent_body is not None, f"json kwarg not found: {call_args}"
        # ``details`` and ``report_type`` are always present (Cloud requires them);
        # empty string / "TEST" when the caller omits them.
        # No other optional field is present.
        assert sent_body == {"title": "Security Scan", "details": "", "report_type": "TEST"}


# ===========================================================================
# delete_code_insight_report (Req 12.2)
# ===========================================================================


class TestDeleteCodeInsightReportCloud:
    """``delete_code_insight_report`` Cloud branch — Requirement 12.2."""

    def test_deletes_cloud_report_url(
        self, cloud_code_insights_mixin: CodeInsightsMixin
    ) -> None:
        """``DELETE .../reports/{key}`` with a bare URL call.

        The Cloud DELETE carries no request body and no query params.
        """
        cloud_code_insights_mixin.bitbucket.delete.return_value = None

        ok = cloud_code_insights_mixin.delete_code_insight_report(
            project_key="my-team",
            repo_slug="myrepo",
            commit_id="abc123",
            report_key="my-ci.sec",
        )

        assert ok is True
        cloud_code_insights_mixin.bitbucket.delete.assert_called_once()
        delete_call = cloud_code_insights_mixin.bitbucket.delete.call_args
        assert delete_call.args == (
            "/2.0/repositories/my-team/myrepo/commit/abc123/reports/my-ci.sec",
        )
        # No body, no query params on the Cloud delete.
        assert "data" not in delete_call.kwargs
        assert "params" not in delete_call.kwargs


# ===========================================================================
# list_code_insight_annotations (Req 12.3)
# ===========================================================================


class TestListCodeInsightAnnotationsCloud:
    """``list_code_insight_annotations`` Cloud branch — Requirement 12.3."""

    def test_issues_cloud_annotations_url(
        self, cloud_code_insights_mixin: CodeInsightsMixin
    ) -> None:
        """``GET .../reports/{key}/annotations`` — single-page Cloud envelope."""
        cloud_code_insights_mixin.bitbucket.get.return_value = {
            "values": [
                _cloud_annotation_payload("ann-1"),
                _cloud_annotation_payload("ann-2", severity="MEDIUM"),
            ],
            "next": None,
        }

        result = cloud_code_insights_mixin.list_code_insight_annotations(
            project_key="my-team",
            repo_slug="myrepo",
            commit_id="abc123",
            report_key="my-ci.sec",
        )

        cloud_code_insights_mixin.bitbucket.get.assert_called_once()
        (called_url,), _kwargs = cloud_code_insights_mixin.bitbucket.get.call_args
        assert called_url == (
            "/2.0/repositories/my-team/myrepo/commit/abc123"
            "/reports/my-ci.sec/annotations"
        )
        assert [a["external_id"] for a in result] == ["ann-1", "ann-2"]


# ===========================================================================
# bulk_create_code_insight_annotations (Req 12.3 + DC→Cloud body unwrap)
# ===========================================================================


class TestBulkCreateCodeInsightAnnotationsCloud:
    """``bulk_create_code_insight_annotations`` Cloud branch — Req 12.3."""

    def test_posts_bare_array_to_cloud_annotations_url(
        self, cloud_code_insights_mixin: CodeInsightsMixin
    ) -> None:
        """``POST .../annotations`` with a bare Cloud array body via session.

        DC wraps the bulk-create payload as ``{"annotations": [...]}``;
        Cloud's bulk-upload endpoint expects the annotations list to be
        the top-level JSON body. The Cloud branch bypasses the
        ``atlassian-python-api`` ``post`` helper (which would wrap a list
        in a dict) and uses the underlying session directly so the bare
        array is preserved. The agent-facing ``annotations`` parameter
        name and per-annotation shape stay identical (Req 5.4 + Req 12.3).
        """
        import json as _json
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        annotations = [
            {
                "externalId": "ann-1",
                "message": "Hardcoded secret",
                "severity": "HIGH",
                "path": "src/foo.py",
                "line": 12,
                "annotation_type": "VULNERABILITY",
            },
            {
                "externalId": "ann-2",
                "message": "SQL injection",
                "severity": "HIGH",
            },
        ]

        # The Cloud branch calls ``self.bitbucket._session.post(...)``
        # directly; wire up a fake response on the session mock.
        fake_resp = MagicMock()
        fake_resp.raise_for_status = MagicMock()
        cloud_code_insights_mixin.bitbucket._session.post.return_value = fake_resp

        ok = cloud_code_insights_mixin.bulk_create_code_insight_annotations(
            project_key="my-team",
            repo_slug="myrepo",
            commit_id="abc123",
            report_key="my-ci.sec",
            annotations=annotations,
        )

        assert ok is True
        cloud_code_insights_mixin.bitbucket._session.post.assert_called_once()
        call_args = cloud_code_insights_mixin.bitbucket._session.post.call_args
        # First positional arg is the full URL (base + path).
        called_url = call_args.args[0]
        assert called_url.endswith(
            "/2.0/repositories/my-team/myrepo/commit/abc123"
            "/reports/my-ci.sec/annotations"
        )
        # Body is a JSON-serialised bare array (not a wrapped dict).
        # The session.post call uses keyword argument ``data=``.
        sent_data = call_args.kwargs.get("data")
        if sent_data is None and len(call_args.args) > 1:
            sent_data = call_args.args[1]
        assert sent_data is not None, f"data not found in call_args: {call_args}"
        assert sent_data == _json.dumps(annotations)
        # Content-Type header must be application/json.
        sent_headers = call_args.kwargs.get("headers", {})
        assert sent_headers.get("Content-Type") == "application/json"


# ===========================================================================
# delete_code_insight_annotations (Req 12.3 + Cloud per-annotation iteration)
# ===========================================================================


class TestDeleteCodeInsightAnnotationsCloud:
    """``delete_code_insight_annotations`` Cloud branch — Req 12.3.

    Cloud lacks a bulk-delete endpoint; the Cloud branch iterates the
    supplied ``external_ids`` and issues one per-annotation DELETE per
    target. When ``external_ids`` is ``None`` the Cloud branch lists the
    report's annotations first and deletes each one so the DC "delete
    all" semantics are preserved.
    """

    def test_deletes_each_supplied_external_id_individually(
        self, cloud_code_insights_mixin: CodeInsightsMixin
    ) -> None:
        """With explicit ``external_ids``, Cloud issues one DELETE per id.

        No list step is needed — the caller already knows which
        annotations to remove, so the Cloud branch skips the GET and
        iterates straight into per-id DELETE calls.
        """
        cloud_code_insights_mixin.bitbucket.delete.return_value = None

        ok = cloud_code_insights_mixin.delete_code_insight_annotations(
            project_key="my-team",
            repo_slug="myrepo",
            commit_id="abc123",
            report_key="my-ci.sec",
            external_ids=["ann-1", "ann-2"],
        )

        assert ok is True
        # No bulk-list step for the explicit-targets path.
        cloud_code_insights_mixin.bitbucket.get.assert_not_called()
        # One DELETE per supplied external id, in order.
        base = (
            "/2.0/repositories/my-team/myrepo/commit/abc123"
            "/reports/my-ci.sec/annotations"
        )
        assert cloud_code_insights_mixin.bitbucket.delete.call_args_list == [
            call(f"{base}/ann-1"),
            call(f"{base}/ann-2"),
        ]

    def test_none_external_ids_lists_then_deletes_each(
        self, cloud_code_insights_mixin: CodeInsightsMixin
    ) -> None:
        """``external_ids=None`` preserves DC "delete all" semantics on Cloud.

        The Cloud branch lists the report's annotations first, extracts
        each annotation's ``external_id`` from the Cloud envelope, and
        issues one DELETE per target. The GET happens before any DELETE.
        """
        cloud_code_insights_mixin.bitbucket.get.return_value = {
            "values": [
                _cloud_annotation_payload("ann-1"),
                _cloud_annotation_payload("ann-2"),
                _cloud_annotation_payload("ann-3"),
            ],
            "next": None,
        }
        cloud_code_insights_mixin.bitbucket.delete.return_value = None

        ok = cloud_code_insights_mixin.delete_code_insight_annotations(
            project_key="my-team",
            repo_slug="myrepo",
            commit_id="abc123",
            report_key="my-ci.sec",
            external_ids=None,
        )

        assert ok is True
        base = (
            "/2.0/repositories/my-team/myrepo/commit/abc123"
            "/reports/my-ci.sec/annotations"
        )
        # List step targets the annotations URL.
        cloud_code_insights_mixin.bitbucket.get.assert_called_once()
        (get_url,), _ = cloud_code_insights_mixin.bitbucket.get.call_args
        assert get_url == base
        # One DELETE per listed annotation, in listing order.
        assert cloud_code_insights_mixin.bitbucket.delete.call_args_list == [
            call(f"{base}/ann-1"),
            call(f"{base}/ann-2"),
            call(f"{base}/ann-3"),
        ]

    def test_none_external_ids_falls_back_to_uuid_when_external_id_missing(
        self, cloud_code_insights_mixin: CodeInsightsMixin
    ) -> None:
        """Annotations without an ``external_id`` fall back to ``uuid``.

        Cloud list payloads always include ``uuid`` but ``external_id``
        may be absent on annotations that were created without one.
        The Cloud delete-all branch uses ``external_id`` when present
        and falls back to ``uuid`` otherwise so no annotation is left
        behind.
        """
        cloud_code_insights_mixin.bitbucket.get.return_value = {
            "values": [
                {"external_id": "ann-1", "uuid": "{uuid-1}"},
                {"uuid": "{uuid-2}"},  # no external_id; must fall back to uuid
            ],
            "next": None,
        }
        cloud_code_insights_mixin.bitbucket.delete.return_value = None

        cloud_code_insights_mixin.delete_code_insight_annotations(
            project_key="my-team",
            repo_slug="myrepo",
            commit_id="abc123",
            report_key="my-ci.sec",
        )

        base = (
            "/2.0/repositories/my-team/myrepo/commit/abc123"
            "/reports/my-ci.sec/annotations"
        )
        assert cloud_code_insights_mixin.bitbucket.delete.call_args_list == [
            call(f"{base}/ann-1"),
            call(f"{base}/{{uuid-2}}"),
        ]
