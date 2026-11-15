"""Hevy tool requests and responses against a fake Hevy API."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from mindroom.custom_tools import hevy
from mindroom.custom_tools.hevy import HevyTools
from mindroom.tools.hevy import hevy_tools

if TYPE_CHECKING:
    from collections.abc import Callable

API_KEY = "00000000-test-key"


def _iso(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _workout(workout_id: str, days_ago: float) -> dict[str, object]:
    return {
        "id": workout_id,
        "title": f"Workout {workout_id}",
        "description": None,
        "start_time": _iso(days_ago),
        "end_time": _iso(days_ago),
        "exercises": [
            {
                "index": 0,
                "title": "Bench Press (Barbell)",
                "exercise_template_id": "79D0BB3A",
                "sets": [{"index": 0, "type": "normal", "weight_kg": 80, "reps": 8, "distance_meters": None}],
            },
        ],
    }


def _api(
    monkeypatch: pytest.MonkeyPatch,
    respond: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Route the tool's HTTP clients to a fake Hevy API and return the requests it receives."""
    requests: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return respond(request)

    def client(**kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(hevy.httpx, "AsyncClient", client)
    return requests


def _pages(key: str, pages: list[list[dict[str, object]]]) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"page": page, "page_count": len(pages), key: pages[page - 1]})

    return respond


def test_registered_factory_returns_the_toolkit() -> None:
    """The registry builds the toolkit with every read function."""
    assert hevy_tools() is HevyTools
    assert set(HevyTools(api_key=API_KEY).functions) == {
        "find_exercises",
        "get_body_measurements",
        "get_exercise_history",
        "get_profile",
        "get_routines",
        "get_workout",
        "get_workouts",
    }


@pytest.mark.asyncio
async def test_missing_api_key_explains_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a key the tool says where to get one and sends nothing."""
    requests = _api(monkeypatch, lambda _: httpx.Response(200, json={}))
    result = await HevyTools(api_key="  ").get_profile()
    assert "hevy.com/settings?developer" in result
    assert requests == []


@pytest.mark.asyncio
async def test_profile_sends_the_key_and_adds_the_workout_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """The profile merges user info with the workout count and sends the key in the api-key header."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/user/info":
            return httpx.Response(200, json={"data": {"name": "Ann", "weight_unit": "lbs", "is_pro": True}})
        return httpx.Response(200, json={"workout_count": 42})

    requests = _api(monkeypatch, respond)
    result = json.loads(await HevyTools(api_key=API_KEY).get_profile())
    assert result == {"name": "Ann", "weight_unit": "lbs", "is_pro": True, "workout_count": 42}
    assert {request.headers["api-key"] for request in requests} == {API_KEY}
    assert str(requests[0].url).startswith("https://api.hevyapp.com/v1/")


@pytest.mark.asyncio
async def test_workouts_stop_paging_past_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Workouts older than the window are dropped, nulls are left out, and later pages are not fetched."""
    pages = [
        [_workout("a", 1), _workout("b", 20)],
        [_workout("c", 40), _workout("d", 50)],
        [_workout("e", 60)],
    ]
    requests = _api(monkeypatch, _pages("workouts", pages))
    result = json.loads(await HevyTools(api_key=API_KEY).get_workouts(days=30))
    assert [workout["id"] for workout in result["workouts"]] == ["a", "b"]
    assert "description" not in result["workouts"][0]
    assert "distance_meters" not in result["workouts"][0]["exercises"][0]["sets"][0]
    assert [request.url.params["page"] for request in requests] == ["1", "2"]
    assert requests[0].url.params["pageSize"] == "10"


@pytest.mark.asyncio
async def test_workouts_reject_a_non_positive_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """A window under one day is refused before any request."""
    requests = _api(monkeypatch, lambda _: httpx.Response(200, json={}))
    assert await HevyTools(api_key=API_KEY).get_workouts(days=0) == "days must be at least 1."
    assert requests == []


@pytest.mark.asyncio
async def test_find_exercises_matches_every_word_across_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every word of the query must appear in the title, in any case, on any page."""
    pages = [
        [{"id": "1", "title": "Bench Press (Barbell)", "thumbnail_url": None}, {"id": "2", "title": "Squat"}],
        [{"id": "3", "title": "Incline Bench Press (Dumbbell)", "is_custom": True}],
    ]
    requests = _api(monkeypatch, _pages("exercise_templates", pages))
    result = json.loads(await HevyTools(api_key=API_KEY).find_exercises("bench PRESS"))
    assert result == {
        "exercises": [
            {"id": "1", "title": "Bench Press (Barbell)"},
            {"id": "3", "title": "Incline Bench Press (Dumbbell)", "is_custom": True},
        ],
        "more": False,
    }
    assert requests[0].url.params["pageSize"] == "100"


@pytest.mark.asyncio
async def test_exercise_history_sends_whole_utc_days(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dates become the start and end of their UTC days, and bad dates are refused."""
    requests = _api(monkeypatch, lambda _: httpx.Response(200, json={"exercise_history": [{"reps": 5}]}))
    tools = HevyTools(api_key=API_KEY)
    result = await tools.get_exercise_history("79D0BB3A", start_date="2026-01-01", end_date="2026-01-31")
    assert json.loads(result) == {"exercise_history": [{"reps": 5}]}
    assert requests[0].url.path == "/v1/exercise_history/79D0BB3A"
    assert requests[0].url.params["start_date"] == "2026-01-01T00:00:00Z"
    assert requests[0].url.params["end_date"] == "2026-01-31T23:59:59Z"
    assert await tools.get_exercise_history("79D0BB3A", start_date="Jan 1") == "Dates must be YYYY-MM-DD."
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_routines_include_folders(monkeypatch: pytest.MonkeyPatch) -> None:
    """Routines and routine folders come back together."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/routines":
            return httpx.Response(200, json={"page": 1, "page_count": 1, "routines": [{"id": "r1"}]})
        return httpx.Response(200, json={"page": 1, "page_count": 1, "routine_folders": [{"id": 7}]})

    _api(monkeypatch, respond)
    result = json.loads(await HevyTools(api_key=API_KEY).get_routines())
    assert result == {"routines": [{"id": "r1"}], "routine_folders": [{"id": 7}]}


@pytest.mark.asyncio
async def test_body_measurements_keep_the_window_newest_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """Measurements outside the window are dropped and the rest are sorted newest first."""
    today = datetime.now(UTC).date()
    entries = [
        {"date": (today - timedelta(days=100)).isoformat(), "weight_kg": 81},
        {"date": (today - timedelta(days=2)).isoformat(), "weight_kg": 80, "fat_percent": None},
        {"date": (today - timedelta(days=10)).isoformat(), "weight_kg": 80.5},
    ]
    _api(monkeypatch, _pages("body_measurements", [entries]))
    result = json.loads(await HevyTools(api_key=API_KEY).get_body_measurements(days=30))
    assert [entry["weight_kg"] for entry in result["body_measurements"]] == [80, 80.5]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "rejected the API key"),
        (403, "Hevy Pro"),
        (404, "nothing with that id"),
        (429, "rate-limiting"),
        (500, "HTTP 500"),
    ],
)
async def test_http_errors_become_messages(monkeypatch: pytest.MonkeyPatch, status: int, expected: str) -> None:
    """Each failure status becomes a message telling the user what happened."""
    _api(monkeypatch, lambda _: httpx.Response(status))
    assert expected in await HevyTools(api_key=API_KEY).get_workout("w1")


@pytest.mark.asyncio
async def test_network_errors_become_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection failure is reported without a traceback."""

    def fail(request: httpx.Request) -> httpx.Response:
        msg = "down"
        raise httpx.ConnectError(msg, request=request)

    _api(monkeypatch, fail)
    assert await HevyTools(api_key=API_KEY).get_workout("w1") == "Could not reach Hevy: ConnectError"
