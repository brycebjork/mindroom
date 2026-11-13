"""SimpleFIN tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import (
    ConfigField,
    SetupType,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolStatus,
)
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
            name="setup_token",
            env=("SIMPLEFIN_SETUP_TOKEN",),
            label="Setup Token",
            type="password",
            required=False,
            default=None,
            description="One-time SimpleFIN Setup Token, exchanged for lasting access on first use",
        ),
    ],
    dependencies=["httpx"],
    docs_url="https://docs.mindroom.chat/tools/data-and-databases/#simplefin",
    helper_text=(
        "Link accounts and create a Setup Token at [SimpleFIN Bridge](https://bridge.simplefin.org), then paste it "
        "here. MindRoom exchanges it the first time the tool runs; save a new token to reconnect."
    ),
    function_names=("get_transactions", "list_accounts"),
    managed_init_args=(ToolManagedInitArg.CREDENTIALS_MANAGER, ToolManagedInitArg.WORKER_TARGET),
)
def simplefin_tools() -> type[SimpleFinTools]:
    """Return SimpleFIN tools for account balances and transactions."""
    from mindroom.custom_tools.simplefin import SimpleFinTools

    return SimpleFinTools
