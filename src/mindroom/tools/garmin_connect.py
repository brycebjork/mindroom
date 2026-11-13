"""Garmin Connect tool configuration."""

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
    from mindroom.custom_tools.garmin_connect import GarminConnectTools


@register_tool_with_metadata(
    name="garmin_connect",
    file_access=ToolFileAccess.NONE,
    display_name="Garmin Connect",
    description="Read sleep, heart rate, HRV, stress, Body Battery, steps, activities, and weigh-ins from Garmin Connect",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaChartArea",
    icon_color="text-sky-600",
    config_fields=[
        ConfigField(
            name="email",
            env=("GARMIN_EMAIL",),
            label="Email",
            type="text",
            required=False,
            default=None,
            description="Email address of your Garmin account",
        ),
        ConfigField(
            name="password",
            env=("GARMIN_PASSWORD",),
            label="Password",
            type="password",
            required=False,
            default=None,
            description="Password of your Garmin account",
        ),
        ConfigField(
            name="mfa_code",
            label="MFA code",
            type="text",
            required=False,
            default=None,
            description="Verification code Garmin sends when the account uses two-factor authentication",
        ),
    ],
    dependencies=["garminconnect"],
    docs_url="https://docs.mindroom.chat/tools/data-and-databases/#garmin_connect",
    helper_text=(
        "Signs in like the Garmin Connect app, since Garmin has no public API for personal accounts. "
        "If Garmin asks for a verification code, enter it as the MFA code and ask again."
    ),
    function_names=(
        "get_activities",
        "get_body_battery",
        "get_daily_steps",
        "get_daily_summary",
        "get_heart_rate",
        "get_hrv",
        "get_sleep",
        "get_stress",
        "get_weigh_ins",
    ),
    managed_init_args=(ToolManagedInitArg.CREDENTIALS_MANAGER, ToolManagedInitArg.WORKER_TARGET),
)
def garmin_connect_tools() -> type[GarminConnectTools]:
    """Return Garmin Connect tools for health and activity data."""
    from mindroom.custom_tools.garmin_connect import GarminConnectTools

    return GarminConnectTools
