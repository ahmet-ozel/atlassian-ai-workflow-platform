"""Jira API module for mcp_atlassian.

This module provides various Jira API client implementations.
"""

# flake8: noqa

# Re-export the Jira class for backward compatibility
from atlassian.jira import Jira

from .archive import ArchiveMixin
from .attachments import AttachmentsMixin
from .boards import BoardsMixin
from .client import JiraClient
from .comments import CommentsMixin
from .config import JiraConfig
from .dashboards import DashboardsMixin
from .development import DevelopmentMixin
from .epics import EpicsMixin
from .field_options import FieldOptionsMixin
from .fields import FieldsMixin
from .filters import FiltersMixin
from .forms_api import FormsApiMixin  # Forms REST API
from .formatting import FormattingMixin
from .groups import GroupsMixin
from .issues import IssuesMixin
from .links import LinksMixin
from .lookups import LookupsMixin
from .mentions import MentionsMixin
from .metrics import MetricsMixin
from .myself import MyselfMixin
from .notifications import NotificationsMixin
from .permissions import PermissionsMixin
from .project_roles import ProjectRolesMixin
from .projects import ProjectsMixin
from .queues import QueuesMixin
from .screens import ScreensMixin
from .search import SearchMixin
from .sla import SLAMixin
from .sprints import SprintsMixin
from .transitions import TransitionsMixin
from .users import UsersMixin
from .votes import VotesMixin
from .watchers import WatchersMixin
from .worklog import WorklogMixin


class JiraFetcher(
    ProjectsMixin,
    FieldsMixin,
    FieldOptionsMixin,
    FormsApiMixin,  # Use new Forms REST API instead of FormsMixin
    FormattingMixin,
    TransitionsMixin,
    WorklogMixin,
    EpicsMixin,
    CommentsMixin,
    SearchMixin,
    IssuesMixin,
    UsersMixin,
    WatchersMixin,
    BoardsMixin,
    SprintsMixin,
    QueuesMixin,
    AttachmentsMixin,
    LinksMixin,
    MetricsMixin,
    SLAMixin,
    DevelopmentMixin,
    # --- atlassian-dc-tool-parity mixins (DC-only additions) ---
    FiltersMixin,
    DashboardsMixin,
    NotificationsMixin,
    VotesMixin,
    LookupsMixin,
    PermissionsMixin,
    MyselfMixin,
    GroupsMixin,
    MentionsMixin,
    ProjectRolesMixin,
    ScreensMixin,
    ArchiveMixin,
):
    """
    The main Jira client class providing access to all Jira operations.

    This class inherits from multiple mixins that provide specific functionality:
    - ProjectsMixin: Project-related operations
    - FieldsMixin: Field-related operations
    - FormattingMixin: Content formatting utilities
    - TransitionsMixin: Issue transition operations
    - WorklogMixin: Worklog operations
    - EpicsMixin: Epic operations
    - CommentsMixin: Comment operations
    - SearchMixin: Search operations
    - IssuesMixin: Issue operations
    - UsersMixin: User operations
    - WatchersMixin: Watcher operations
    - BoardsMixin: Board operations
    - SprintsMixin: Sprint operations
    - AttachmentsMixin: Attachment download operations
    - LinksMixin: Issue link operations
    - MetricsMixin: Issue metrics and date operations
    - QueuesMixin: Service Desk queue read operations (Server/DC)
    - SLAMixin: SLA calculations
    - FiltersMixin: Saved JQL filter CRUD (owner-scoped delete)
    - DashboardsMixin: Dashboard discovery (read-only)
    - NotificationsMixin: Issue email notifications (broadcast-capable)
    - VotesMixin: Issue votes (get/add/remove, idempotent)
    - LookupsMixin: Instance-wide lookups (priorities/resolutions/statuses/issue types)
    - PermissionsMixin: Per-issue my-permissions check
    - MyselfMixin: Authenticated user profile
    - GroupsMixin: Group discovery read-only
    - MentionsMixin: @mention user suggestions (empty-query short-circuit)
    - ProjectRolesMixin: Project roles and actors (read-only)
    - ScreensMixin: Issue create/edit screen metadata (read-only)
    - ArchiveMixin: Issue archive/restore (DC 9.4+)

    The class structure is designed to maintain backward compatibility while
    improving code organization and maintainability.
    """

    pass


__all__ = [
    "JiraFetcher",
    "JiraConfig",
    "JiraClient",
    "Jira",
    "MetricsMixin",
    "SLAMixin",
]
