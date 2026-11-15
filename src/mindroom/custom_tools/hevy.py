"""Read-only workouts, routines, exercise history, and body measurements from the Hevy workout tracker.

Wire contract: https://api.hevyapp.com/docs, which needs a Hevy Pro account and the API key from
https://hevy.com/settings?developer.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from mindroom.tool_system.authoring import Toolkit, tool

_BASE_URL = "https://api.hevyapp.com/v1"
_TIMEOUT_SECONDS = 30.0
# Largest page sizes the API accepts for each list.
_WORKOUT_PAGE_SIZE = 10
_TEMPLATE_PAGE_SIZE = 100
# Stops a list from paging forever if the API keeps reporting more pages.
_MAX_PAGES = 100
_MAX_EXERCISE_MATCHES = 50


class _HevyError(Exception):
    """A request to Hevy failed; the message tells the user what to do."""


def _compact(value: Any) -> Any:  # noqa: ANN401
    """Return ``value`` without null fields, which most Hevy sets have many of."""
    if isinstance(value, dict):
        return {key: _compact(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_compact(item) for item in value]
    return value


def _dump(value: Any) -> str:  # noqa: ANN401
    return json.dumps(_compact(value), ensure_ascii=False, separators=(",", ":"))


def _parse_time(text: str) -> datetime:
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _day_bound(text: str, *, end: bool) -> str:
    """Return the UTC timestamp that starts, or with ``end`` closes, the ``YYYY-MM-DD`` day."""
    day = date.fromisoformat(text)
    return f"{day.isoformat()}T23:59:59Z" if end else f"{day.isoformat()}T00:00:00Z"


class HevyTools(Toolkit):
    """Read workouts, routines, exercise history, and body measurements logged in Hevy."""

    name = "hevy"
    instructions = (
        "Hevy always reports weight in kilograms and distance in meters, whatever unit the user picked; "
        "`get_profile` says which units the user prefers, so convert before answering. "
        "Times are UTC ISO 8601. Fields Hevy left empty are omitted. "
        "Find an exercise's id with `find_exercises` before calling `get_exercise_history`."
    )

    def __init__(self, api_key: str | None = None) -> None:
        """Store the Hevy API key."""
        self._api_key = (api_key or "").strip()
        super().__init__()

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict[str, str | int] | None = None) -> Any:  # noqa: ANN401
        """Return the decoded JSON of one GET request, raising ``_HevyError`` with what went wrong."""
        try:
            response = await client.get(f"{_BASE_URL}{path}", params=params)
        except httpx.HTTPError as exc:
            msg = f"Could not reach Hevy: {type(exc).__name__}"
            raise _HevyError(msg) from None
        if response.status_code == httpx.codes.UNAUTHORIZED:
            msg = (
                "Hevy rejected the API key. Copy it again from https://hevy.com/settings?developer "
                "into the tool's settings."
            )
            raise _HevyError(msg)
        if response.status_code == httpx.codes.FORBIDDEN:
            msg = "Hevy refused the request; its API works only while the account has Hevy Pro."
            raise _HevyError(msg)
        if response.status_code == httpx.codes.NOT_FOUND:
            msg = "Hevy has nothing with that id."
            raise _HevyError(msg)
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            msg = "Hevy is rate-limiting requests; try again in a minute."
            raise _HevyError(msg)
        if response.status_code != httpx.codes.OK:
            msg = f"Hevy returned HTTP {response.status_code}."
            raise _HevyError(msg)
        return response.json()

    def _client(self) -> httpx.AsyncClient:
        if not self._api_key:
            msg = "Hevy is not configured: save the API key from https://hevy.com/settings?developer in the tool's settings."
            raise _HevyError(msg)
        return httpx.AsyncClient(
            headers={"api-key": self._api_key, "accept": "application/json"},
            timeout=_TIMEOUT_SECONDS,
        )

    async def _pages(
        self,
        client: httpx.AsyncClient,
        path: str,
        key: str,
        page_size: int,
        *,
        older_than: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return the items of every page of a list, stopping after a page whose items all started before ``older_than``."""
        items: list[dict[str, Any]] = []
        for page in range(1, _MAX_PAGES + 1):
            payload = await self._get(client, path, {"page": page, "pageSize": page_size})
            page_items = payload.get(key) or []
            items.extend(page_items)
            if older_than is not None and all(_parse_time(item["start_time"]) < older_than for item in page_items):
                break
            if not page_items or page >= int(payload.get("page_count") or 0):
                break
        return items

    @tool
    async def get_profile(self) -> str:
        """Get the Hevy user's name, preferred weight and distance units, Pro status, and total workout count."""
        try:
            async with self._client() as client:
                info = await self._get(client, "/user/info")
                count = await self._get(client, "/workouts/count")
        except _HevyError as exc:
            return str(exc)
        return _dump({**(info.get("data") or {}), "workout_count": count.get("workout_count")})

    @tool
    async def get_workouts(self, days: int = 30) -> str:
        """Get the workouts started in the last `days` days, newest first, with every exercise and set.

        Args:
            days: How many days back to read, counting from now.

        """
        if days < 1:
            return "days must be at least 1."
        cutoff = datetime.now(UTC) - timedelta(days=days)
        try:
            async with self._client() as client:
                workouts = await self._pages(client, "/workouts", "workouts", _WORKOUT_PAGE_SIZE, older_than=cutoff)
        except _HevyError as exc:
            return str(exc)
        recent = [workout for workout in workouts if _parse_time(workout["start_time"]) >= cutoff]
        recent.sort(key=lambda workout: workout["start_time"], reverse=True)
        return _dump({"workouts": recent})

    @tool
    async def get_workout(self, workout_id: str) -> str:
        """Get one workout with every exercise and set.

        Args:
            workout_id: A workout id from `get_workouts`.

        """
        try:
            async with self._client() as client:
                return _dump(await self._get(client, f"/workouts/{workout_id.strip()}"))
        except _HevyError as exc:
            return str(exc)

    @tool
    async def find_exercises(self, query: str) -> str:
        """Find exercises, built-in and custom, whose name contains every word of `query`.

        Args:
            query: Words of the exercise name, such as "bench press" or "squat barbell".

        """
        words = query.lower().split()
        if not words:
            return "query must name the exercise."
        try:
            async with self._client() as client:
                templates = await self._pages(client, "/exercise_templates", "exercise_templates", _TEMPLATE_PAGE_SIZE)
        except _HevyError as exc:
            return str(exc)
        matches = [
            {
                key: template.get(key)
                for key in (
                    "id",
                    "title",
                    "type",
                    "primary_muscle_group",
                    "secondary_muscle_groups",
                    "equipment",
                    "is_custom",
                )
            }
            for template in templates
            if all(word in str(template.get("title", "")).lower() for word in words)
        ]
        return _dump({"exercises": matches[:_MAX_EXERCISE_MATCHES], "more": len(matches) > _MAX_EXERCISE_MATCHES})

    @tool
    async def get_exercise_history(
        self,
        exercise_template_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> str:
        """Get every logged set of one exercise, with the workout it was in, to follow progress over time.

        Args:
            exercise_template_id: An exercise id from `find_exercises`.
            start_date: First day to include, as YYYY-MM-DD in UTC; omit to start at the first logged set.
            end_date: Last day to include, as YYYY-MM-DD in UTC; omit to read through today.

        """
        try:
            params = {}
            if start_date:
                params["start_date"] = _day_bound(start_date, end=False)
            if end_date:
                params["end_date"] = _day_bound(end_date, end=True)
        except ValueError:
            return "Dates must be YYYY-MM-DD."
        try:
            async with self._client() as client:
                return _dump(await self._get(client, f"/exercise_history/{exercise_template_id.strip()}", params))
        except _HevyError as exc:
            return str(exc)

    @tool
    async def get_routines(self) -> str:
        """Get every saved routine with its exercises and planned sets, and the folders they are in."""
        try:
            async with self._client() as client:
                routines = await self._pages(client, "/routines", "routines", _WORKOUT_PAGE_SIZE)
                folders = await self._pages(client, "/routine_folders", "routine_folders", _WORKOUT_PAGE_SIZE)
        except _HevyError as exc:
            return str(exc)
        return _dump({"routines": routines, "routine_folders": folders})

    @tool
    async def get_body_measurements(self, days: int = 365) -> str:
        """Get the body weight, body fat, and tape measurements logged in the last `days` days, newest first.

        Args:
            days: How many days back to read, counting from today.

        """
        if days < 1:
            return "days must be at least 1."
        first_day = datetime.now(UTC).date() - timedelta(days=days)
        try:
            async with self._client() as client:
                entries = await self._pages(client, "/body_measurements", "body_measurements", _WORKOUT_PAGE_SIZE)
        except _HevyError as exc:
            return str(exc)
        recent = [entry for entry in entries if date.fromisoformat(entry["date"]) >= first_day]
        recent.sort(key=lambda entry: entry["date"], reverse=True)
        return _dump({"body_measurements": recent})
