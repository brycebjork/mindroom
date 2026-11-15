"""Hevy tool configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.tool_system.declarations import ConfigField, SetupType, ToolCategory, ToolFileAccess, ToolStatus
from mindroom.tool_system.registration import register_tool_with_metadata

if TYPE_CHECKING:
    from mindroom.custom_tools.hevy import HevyTools


@register_tool_with_metadata(
    name="hevy",
    file_access=ToolFileAccess.NONE,
    display_name="Hevy",
    description="Read workouts, routines, exercise history, and body measurements logged in the Hevy workout tracker",
    category=ToolCategory.PRODUCTIVITY,
    status=ToolStatus.REQUIRES_CONFIG,
    setup_type=SetupType.API_KEY,
    icon="FaChartBar",
    icon_color="text-blue-600",
    config_fields=[
        ConfigField(
            name="api_key",
            env=("HEVY_API_KEY",),
            label="API Key",
            type="password",
            required=False,
            default=None,
            description="Hevy API key from hevy.com/settings?developer",
        ),
    ],
    dependencies=["httpx"],
    docs_url="https://docs.mindroom.chat/tools/data-and-databases/#hevy",
    helper_text=(
        "Sign in at [Hevy settings](https://hevy.com/settings?developer) the way you sign in to the app, "
        "create an API key, and paste it here. Hevy's API needs Hevy Pro."
    ),
    function_names=(
        "find_exercises",
        "get_body_measurements",
        "get_exercise_history",
        "get_profile",
        "get_routines",
        "get_workout",
        "get_workouts",
    ),
)
def hevy_tools() -> type[HevyTools]:
    """Return Hevy tools for workout data."""
    from mindroom.custom_tools.hevy import HevyTools

    return HevyTools
