"""Beeper tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.beeper import BeeperTools


@register_tool_with_metadata(
    name="beeper",
    file_access=ToolFileAccess.NONE,
    display_name="Beeper",
    description="Read, search, and send messages as you in every chat network connected to Beeper",
    category=ToolCategory.COMMUNICATION,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="MessageCircleMore",
    icon_color="text-indigo-500",
    config_fields=[
        ConfigField(
            name="base_url",
            env=("BEEPER_BASE_URL",),
            label="API address",
            type="url",
            required=False,
            default=None,
            description="Address of Beeper Desktop or Beeper Server, such as http://beeper:23373",
        ),
        ConfigField(
            name="access_token",
            env=("BEEPER_ACCESS_TOKEN",),
            label="Access token",
            type="password",
            required=False,
            default=None,
            description="Token from Beeper's Settings > Integrations > Approved connections",
        ),
    ],
    dependencies=["httpx"],
    docs_url="https://docs.mindroom.chat/tools/messaging-and-social/#beeper",
    helper_text=(
        "Run [Beeper Desktop](https://www.beeper.com/download) or a headless Beeper Server where MindRoom can reach it, "
        "create a token under Settings > Integrations > Approved connections, and paste the address and token here."
    ),
    function_names=(
        "edit_message",
        "get_chat",
        "list_accounts",
        "list_chats",
        "list_messages",
        "react",
        "search_contacts",
        "search_messages",
        "send_message",
        "set_draft",
        "set_reminder",
        "start_chat",
        "update_chat",
    ),
)
def beeper_tools() -> type[BeeperTools]:
    """Return Beeper tools for the user's chats."""
    from mindroom.custom_tools.beeper import BeeperTools

    return BeeperTools
