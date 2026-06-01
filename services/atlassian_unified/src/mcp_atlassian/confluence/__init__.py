"""Confluence API integration module.

This module provides access to Confluence content through the Model Context Protocol.
"""

from .analytics import AnalyticsMixin
from .archive import ArchiveMixin as ConfluenceArchiveMixin
from .attachments import AttachmentsMixin
from .client import ConfluenceClient
from .comments import CommentsMixin
from .config import ConfluenceConfig
from .cql_advanced import CQLAdvancedMixin
from .descendants import DescendantsMixin
from .groups import GroupsMixin as ConfluenceGroupsMixin
from .inline_tasks import InlineTasksMixin
from .labels import LabelsMixin
from .likes import LikesMixin
from .long_tasks import LongTasksMixin
from .page_move_copy import PageMoveCopyMixin
from .page_properties import PagePropertiesMixin
from .pages import PagesMixin
from .restrictions import RestrictionsMixin
from .search import SearchMixin
from .space_permissions import SpacePermissionsMixin
from .spaces import SpacesMixin
from .templates import TemplatesMixin
from .users import UsersMixin
from .watchers import WatchersMixin


class ConfluenceFetcher(
    SearchMixin,
    SpacesMixin,
    # --- atlassian-dc-tool-parity: PageMoveCopyMixin overrides PagesMixin.move_page ---
    # ``PageMoveCopyMixin`` is listed *before* ``PagesMixin`` so that
    # ``fetcher.move_page`` resolves to the DC-parity implementation
    # (direct PUT against ``/rest/api/content/{id}/move/{position}/{target}``
    # that may return a ``longTaskId`` for asynchronous moves, per
    # Requirement 31). The legacy ``PagesMixin.move_page`` remains
    # accessible via the ``PagesMixin`` class directly for any caller
    # that still needs the ``target_space_key`` / library-adapter path,
    # and is exercised by the existing ``TestMovePage`` suite which
    # instantiates ``PagesMixin`` directly.
    PageMoveCopyMixin,
    PagesMixin,
    CommentsMixin,
    LabelsMixin,
    UsersMixin,
    AnalyticsMixin,
    AttachmentsMixin,
    # --- atlassian-dc-tool-parity mixins (DC-only additions) ---
    RestrictionsMixin,
    WatchersMixin,
    SpacePermissionsMixin,
    TemplatesMixin,
    PagePropertiesMixin,
    ConfluenceArchiveMixin,
    CQLAdvancedMixin,
    InlineTasksMixin,
    LikesMixin,
    LongTasksMixin,
    ConfluenceGroupsMixin,
    DescendantsMixin,
):
    """Main entry point for Confluence operations, providing backward compatibility.

    This class combines functionality from various mixins to maintain the same
    API as the original ConfluenceFetcher class.

    Available mixins:
    - SearchMixin: CQL search operations
    - SpacesMixin: Space operations
    - PageMoveCopyMixin: DC-parity page move + page-hierarchy copy (Req 31)
    - PagesMixin: Page operations
    - CommentsMixin: Comment operations
    - LabelsMixin: Label operations
    - UsersMixin: User operations
    - AnalyticsMixin: Page view analytics (Cloud only)
    - AttachmentsMixin: Attachment operations
    - RestrictionsMixin: Content restrictions list/set/clear (with prior state)
    - WatchersMixin: Self-scoped page watcher list/watch/unwatch (DC)
    - SpacePermissionsMixin: Space permissions inspection (read-only, DC)
    - TemplatesMixin: Blueprint/page template listing and create-from-template (Req 32)
    - PagePropertiesMixin: Per-page key/value content properties CRUD (Req 33)
    - ConfluenceArchiveMixin: Page archive/restore + space archive; no permanent delete (Req 34)
    - CQLAdvancedMixin: CQL advanced search with explicit order-by and space-filter awareness (Req 35)
    - InlineTasksMixin: Read-only listing of Confluence inline tasks via the DC ``mywork`` plugin (Req 36)
    - LikesMixin: Page like / unlike via the plugin-bundled ``/rest/likes/1.0`` endpoint (Req 37, plugin-gated)
    - LongTasksMixin: Read-only polling of Confluence long-running tasks (Req 38)
    - ConfluenceGroupsMixin: Read-only Confluence group search and user-group membership lookup (Req 39)
    - DescendantsMixin: Read-only page descendants tree with depth capped at 10 (Req 40)
    """

    pass


__all__ = [
    "ConfluenceFetcher",
    "ConfluenceConfig",
    "ConfluenceClient",
    "AnalyticsMixin",
]
