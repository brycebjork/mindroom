"""Goodreads tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.goodreads import GoodreadsTools


@register_tool_with_metadata(
    name="goodreads",
    file_access=ToolFileAccess.NONE,
    display_name="Goodreads",
    description="Read the books on your Goodreads shelves, with your ratings, reviews, and reading dates",
    category=ToolCategory.ENTERTAINMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaFileAlt",
    icon_color="text-amber-700",
    config_fields=[
        ConfigField(
            name="session_token",
            env=("GOODREADS_SESSION_COOKIE",),
            label="Session Cookie",
            type="password",
            required=False,
            default=None,
            description="The Cookie request header of a signed-in goodreads.com browser tab",
        ),
        ConfigField(
            name="profile_url",
            env=("GOODREADS_PROFILE_URL",),
            label="Profile Link",
            type="url",
            required=False,
            default=None,
            description="Your Goodreads profile link; only needed if the tool cannot find your account by itself",
        ),
    ],
    dependencies=["httpx"],
    docs_url="https://docs.mindroom.chat/tools/media-and-content/#goodreads",
    helper_text=(
        "Goodreads has no API, so MindRoom reads your shelves with your browser's session. "
        "Sign in at goodreads.com on a computer (with Amazon is fine), open the developer tools' Network tab, "
        "reload, select the first www.goodreads.com request, and paste the value of its Cookie request header here. "
        "It works like a password for your Goodreads account, and signing out of Goodreads in that browser can end it."
    ),
    function_names=("find_my_books", "get_books_read", "get_shelf"),
)
def goodreads_tools() -> type[GoodreadsTools]:
    """Return Goodreads tools for reading the user's shelves."""
    from mindroom.custom_tools.goodreads import GoodreadsTools

    return GoodreadsTools
