"""Apify Actor selection through config, without calling Apify."""

from pathlib import Path

import pytest

from mindroom.constants import resolve_runtime_paths
from mindroom.tool_system.metadata import (
    ToolConfigOverrideError,
    get_tool_by_name,
    validate_authored_tool_entry_overrides,
)
from mindroom.tool_system.registry import builtin_tool_registry

ACTORS = ["tri_angle/airbnb-scraper", "apify/rag-web-browser"]
FUNCTIONS = {"apify_actor_tri_angle_airbnb-scraper", "apify_actor_apify_rag-web-browser"}


@pytest.fixture
def offline_apify(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve each Actor's build locally so registration needs no Apify account or network."""

    def latest_build(_client: object, actor_id: str) -> dict[str, object]:
        return {
            "actorDefinition": {
                "description": f"Runs {actor_id}.",
                "input": {"properties": {"maxResults": {"type": "integer", "description": "Result cap."}}},
            },
        }

    monkeypatch.setattr("mindroom.tools.apify._create_apify_client", lambda _token: object())
    monkeypatch.setattr("mindroom.tools.apify._get_actor_latest_build", latest_build)


@pytest.mark.usefixtures("offline_apify")
@pytest.mark.parametrize("actors", [ACTORS, "tri_angle/airbnb-scraper, apify/rag-web-browser"])
def test_each_configured_actor_becomes_a_function(tmp_path: Path, actors: object) -> None:
    """A list or a comma-separated string registers every Actor, not one Actor named after the whole string."""
    runtime_paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "storage")
    validated = validate_authored_tool_entry_overrides(
        "apify",
        {"actors": actors},
        tool_metadata=builtin_tool_registry().entries,
    )
    assert validated == {"actors": ACTORS}

    tool = get_tool_by_name(
        "apify",
        runtime_paths,
        credential_overrides={"apify_api_token": "test-token"},
        tool_config_overrides=validated,
        runs_in_worker=False,
        worker_target=None,
        tools=builtin_tool_registry(),
    )

    assert set(tool.functions) == FUNCTIONS


def test_malformed_actor_id_fails_config_validation() -> None:
    """A typo in an Actor ID is a config error rather than a silently missing function."""
    with pytest.raises(ToolConfigOverrideError, match=r"'airbnb scraper' is not a valid Actors entry"):
        validate_authored_tool_entry_overrides(
            "apify",
            {"actors": ["airbnb scraper"]},
            config_path_prefix="agents.mind.tools",
            tool_metadata=builtin_tool_registry().entries,
        )
