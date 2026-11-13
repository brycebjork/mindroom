"""SimpleFIN tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.simplefin import SimpleFinTools


@register_tool_with_metadata(
    name="simplefin",
    file_access=ToolFileAccess.NONE,
    display_name="SimpleFIN",
    description="Read balances and transactions of bank and card accounts linked in SimpleFIN Bridge",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaChartLine",
    icon_color="text-green-600",
    config_fields=[
        ConfigField(
            name="access_url",
            env=("SIMPLEFIN_ACCESS_URL",),
            label="Access URL",
            type="password",
            required=False,
            default=None,
            description="The https://user:password@host/... URL a SimpleFIN Setup Token is exchanged for",
        ),
    ],
    dependencies=["httpx"],
    docs_url="https://docs.mindroom.chat/tools/data-and-databases/#simplefin",
    helper_text=(
        "Link accounts and create a Setup Token at [SimpleFIN Bridge](https://bridge.simplefin.org), "
        "then exchange it for an Access URL with `curl -X POST \"$(echo '<setup token>' | base64 --decode)\"`."
    ),
    function_names=("get_transactions", "list_accounts"),
)
def simplefin_tools() -> type[SimpleFinTools]:
    """Return SimpleFIN tools for account balances and transactions."""
    from mindroom.custom_tools.simplefin import SimpleFinTools

    return SimpleFinTools
