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
    description="Read the books on your Goodreads shelves, with your ratings, reviews, and dates read",
    category=ToolCategory.ENTERTAINMENT,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaFileAlt",
    icon_color="text-amber-700",
    config_fields=[
        ConfigField(
            name="secret_rss_link",
            env=("GOODREADS_SECRET_RSS_LINK",),
            label="RSS Feed Link",
            type="password",
            required=False,
            default=None,
            description="The RSS link at the bottom of your Goodreads 'My Books' page",
        ),
    ],
    dependencies=["httpx", "defusedxml"],
    docs_url="https://docs.mindroom.chat/tools/media-and-content/#goodreads",
    helper_text=(
        "Sign in to [Goodreads](https://www.goodreads.com/review/list) however you usually do, including with Amazon, "
        "copy the RSS link at the bottom of your 'My Books' page, and paste it here. "
        "Its key lets MindRoom read your shelves even when your profile is private, so keep it secret."
    ),
    function_names=("find_my_books", "get_books_read", "get_shelf"),
)
def goodreads_tools() -> type[GoodreadsTools]:
    """Return Goodreads tools for reading the user's shelves."""
    from mindroom.custom_tools.goodreads import GoodreadsTools

    return GoodreadsTools
