"""Unit tests for the Confluence LongTasksMixin.

Covers Requirement 38.2: the mixin translates a 404 from
``GET /rest/api/longtask/{long_task_id}`` into a ``LongTaskNotFoundError``
so the server-tool layer can surface the structured ``long_task_not_found``
error envelope. Non-404 HTTP failures must fall through unchanged.
"""

from unittest.mock import MagicMock

import pytest
from requests.exceptions import HTTPError

from mcp_atlassian.confluence.long_tasks import (
    LongTaskNotFoundError,
    LongTasksMixin,
)
from mcp_atlassian.utils.dc_guards import ERROR_CODES


class TestLongTasksMixin:
    """Tests for the LongTasksMixin class."""

    @pytest.fixture
    def long_tasks_mixin(self):
        """Create a LongTasksMixin instance with mocked dependencies."""
        mixin = MagicMock(spec=LongTasksMixin)
        mixin.config = MagicMock()
        mixin.confluence = MagicMock()

        # Bind the real method so we exercise the production code path.
        mixin.get_long_task = lambda *args, **kwargs: LongTasksMixin.get_long_task(
            mixin, *args, **kwargs
        )

        return mixin

    def test_get_long_task_returns_payload_on_success(self, long_tasks_mixin):
        """Happy path: a 200 payload from DC is returned verbatim."""
        # Arrange — a representative DC long-task status envelope.
        long_task_id = "12345"
        status_payload = {
            "id": long_task_id,
            "name": {"key": "com.atlassian.confluence.longrunning.name.copypagetree"},
            "percentageComplete": 42,
            "successful": False,
            "finished": False,
            "elapsedTime": 1500,
            "remainingTime": 2000,
            "messages": [
                {"translation": "Copying page tree", "args": []},
            ],
            "additionalDetails": {"destinationPageId": "67890"},
        }
        long_tasks_mixin.confluence.get.return_value = status_payload

        # Act
        result = long_tasks_mixin.get_long_task(long_task_id)

        # Assert — the dict is returned unchanged and the correct endpoint
        # path was requested.
        assert result == status_payload
        assert result["percentageComplete"] == 42
        assert result["finished"] is False
        assert result["successful"] is False
        long_tasks_mixin.confluence.get.assert_called_once_with(
            f"rest/api/longtask/{long_task_id}"
        )

    def test_get_long_task_finished_success_payload(self, long_tasks_mixin):
        """A completed-task payload (finished=True) is returned verbatim."""
        long_task_id = "99"
        status_payload = {
            "id": long_task_id,
            "percentageComplete": 100,
            "successful": True,
            "finished": True,
            "elapsedTime": 3200,
            "remainingTime": 0,
            "messages": [],
        }
        long_tasks_mixin.confluence.get.return_value = status_payload

        result = long_tasks_mixin.get_long_task(long_task_id)

        assert result["percentageComplete"] == 100
        assert result["finished"] is True
        assert result["successful"] is True

    def test_get_long_task_404_maps_to_long_task_not_found(self, long_tasks_mixin):
        """A 404 from DC is translated to LongTaskNotFoundError (Req 38.2)."""
        # Arrange — underlying client raises HTTPError with a 404 response.
        long_task_id = "does-not-exist"
        mock_response = MagicMock()
        mock_response.status_code = 404
        http_error = HTTPError(response=mock_response)
        long_tasks_mixin.confluence.get.side_effect = http_error

        # Act / Assert — the mixin raises LongTaskNotFoundError carrying the
        # offending id, which the server layer maps to the structured
        # ``long_task_not_found`` error code in the allowlist.
        with pytest.raises(LongTaskNotFoundError) as exc_info:
            long_tasks_mixin.get_long_task(long_task_id)

        assert exc_info.value.long_task_id == long_task_id
        assert long_task_id in str(exc_info.value)
        # The structured error code must be part of the documented allowlist.
        assert "long_task_not_found" in ERROR_CODES

    def test_get_long_task_non_404_http_error_propagates(self, long_tasks_mixin):
        """Non-404 HTTP failures fall through as HTTPError unchanged."""
        # Arrange — transport/auth errors must not be swallowed by the mixin.
        mock_response = MagicMock()
        mock_response.status_code = 500
        http_error = HTTPError("Internal Server Error", response=mock_response)
        long_tasks_mixin.confluence.get.side_effect = http_error

        # Act / Assert
        with pytest.raises(HTTPError, match="Internal Server Error"):
            long_tasks_mixin.get_long_task("42")

    def test_get_long_task_http_error_without_response_propagates(
        self, long_tasks_mixin
    ):
        """HTTPError without an attached response is not treated as a 404."""
        http_error = HTTPError("connection dropped")
        http_error.response = None
        long_tasks_mixin.confluence.get.side_effect = http_error

        with pytest.raises(HTTPError, match="connection dropped"):
            long_tasks_mixin.get_long_task("42")

    def test_get_long_task_unexpected_response_returns_empty_dict(
        self, long_tasks_mixin
    ):
        """Non-dict responses are normalized to an empty dict for stable JSON."""
        long_tasks_mixin.confluence.get.return_value = None

        result = long_tasks_mixin.get_long_task("42")

        assert result == {}


class TestLongTaskNotFoundError:
    """Tests for the LongTaskNotFoundError exception shape."""

    def test_carries_long_task_id_as_string(self):
        """The exception coerces the id to str for deterministic serialization."""
        err = LongTaskNotFoundError("abc-123")
        assert err.long_task_id == "abc-123"
        assert "abc-123" in str(err)

    def test_coerces_non_string_ids(self):
        """Integer ids from some DC flows are coerced to str."""
        err = LongTaskNotFoundError(42)  # type: ignore[arg-type]
        assert err.long_task_id == "42"
