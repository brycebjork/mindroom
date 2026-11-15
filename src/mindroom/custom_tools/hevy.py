"""Workouts, routines, exercise history, and body measurements from the Hevy workout tracker, and routines written to it.

Wire contract: https://api.hevyapp.com/docs, which needs a Hevy Pro account and the API key from
https://hevy.com/settings?developer.
The API has no delete, so the tool writes only routines, routine folders, and custom exercises, which the user can
remove in the app; it never changes logged workouts or measurements.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

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


_ExerciseType = Literal[
    "weight_reps",
    "reps_only",
    "bodyweight_reps",
    "bodyweight_assisted_reps",
    "duration",
    "weight_duration",
    "distance_duration",
    "short_distance_weight",
]
_EquipmentCategory = Literal[
    "none",
    "barbell",
    "dumbbell",
    "kettlebell",
    "machine",
    "plate",
    "resistance_band",
    "suspension",
    "other",
]
_MuscleGroup = Literal[
    "abdominals",
    "shoulders",
    "biceps",
    "triceps",
    "forearms",
    "quadriceps",
    "hamstrings",
    "calves",
    "glutes",
    "abductors",
    "adductors",
    "lats",
    "upper_back",
    "traps",
    "lower_back",
    "chest",
    "cardio",
    "neck",
    "full_body",
    "other",
]


class RepRange(BaseModel):
    """A target range of reps, such as 8 to 12."""

    model_config = ConfigDict(extra="forbid")

    start: int = Field(ge=1, description="Fewest reps of the range.")
    end: int = Field(ge=1, description="Most reps of the range.")


class RoutineSet(BaseModel):
    """One planned set; fill the fields the exercise's type uses, such as weight_kg and reps for weight_reps."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["warmup", "normal", "failure", "dropset"] = "normal"
    weight_kg: float | None = Field(default=None, ge=0, description="Planned weight in kilograms.")
    reps: int | None = Field(default=None, ge=0, description="Planned reps; use rep_range for a range instead.")
    rep_range: RepRange | None = None
    distance_meters: int | None = Field(default=None, ge=0)
    duration_seconds: int | None = Field(default=None, ge=0)
    custom_metric: float | None = Field(default=None, description="Steps or floors, for stair machines.")


class RoutineExercise(BaseModel):
    """One exercise of a routine, in the order it is done."""

    model_config = ConfigDict(extra="forbid")

    exercise_template_id: str = Field(min_length=1, description="An exercise id from `find_exercises`.")
    sets: list[RoutineSet] = Field(min_length=1)
    rest_seconds: int | None = Field(default=None, ge=0, description="Rest after each set.")
    notes: str | None = Field(default=None, description="Cues shown with the exercise, such as tempo or form.")
    superset_id: int | None = Field(
        default=None,
        ge=0,
        description="Give consecutive exercises the same number to do them as a superset.",
    )


def _routine_body(
    title: str,
    exercises: list[RoutineExercise],
    notes: str | None,
    folder_id: int | None,
) -> dict[str, Any]:
    planned = [exercise.model_dump() for exercise in exercises]
    for exercise in planned:
        for planned_set in exercise["sets"]:
            # Updating a routine rejects a null rep_range that creating one accepts.
            if planned_set["rep_range"] is None:
                del planned_set["rep_range"]
    routine: dict[str, Any] = {"title": title.strip(), "notes": notes, "exercises": planned}
    if folder_id is not None:
        routine["folder_id"] = folder_id
    return {"routine": routine}


def _compact(value: Any) -> Any:  # noqa: ANN401
    """Return ``value`` without null fields, which most Hevy sets have many of."""
    if isinstance(value, dict):
        return {key: _compact(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_compact(item) for item in value]
    return value


def _dump(value: Any) -> str:  # noqa: ANN401
    return json.dumps(_compact(value), ensure_ascii=False, separators=(",", ":"))


def _error_detail(response: httpx.Response) -> str:
    """Return ``": <Hevy's error message>"``, or nothing when the response carries none."""
    try:
        payload = response.json()
    except ValueError:
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    return f": {str(error)[:300]}" if error else ""


def _parse_time(text: str) -> datetime:
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _day_bound(text: str, *, end: bool) -> str:
    """Return the UTC timestamp that starts, or with ``end`` closes, the ``YYYY-MM-DD`` day."""
    day = date.fromisoformat(text)
    return f"{day.isoformat()}T23:59:59Z" if end else f"{day.isoformat()}T00:00:00Z"


class HevyTools(Toolkit):
    """Read workouts, routines, exercise history, and body measurements logged in Hevy, and write routines."""

    name = "hevy"
    instructions = (
        "Hevy always uses kilograms and meters, whatever unit the user picked; "
        "`get_profile` says which units the user prefers, so convert before answering, "
        "and convert planned weights to kilograms at full precision (pounds x 0.45359237) so the app shows round pounds. "
        "Times are UTC ISO 8601. Fields Hevy left empty are omitted. "
        "Find exercise ids with `find_exercises`; use a custom exercise only when no built-in one fits. "
        "A routine is a planned workout the user starts in the Hevy app, which logs the workout itself. "
        "Hevy's API cannot delete anything, so reuse routines: update one you made earlier instead of piling up new ones. "
        "`update_routine` replaces the whole routine, so change routines the user built only when asked."
    )

    def __init__(self, api_key: str | None = None) -> None:
        """Store the Hevy API key."""
        self._api_key = (api_key or "").strip()
        super().__init__()

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict[str, str | int] | None = None) -> Any:  # noqa: ANN401
        """Return the decoded JSON of one GET request, raising ``_HevyError`` with what went wrong."""
        return await self._request(client, "GET", path, params=params)

    async def _request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        params: dict[str, str | int] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:  # noqa: ANN401
        """Return the decoded JSON of one request, raising ``_HevyError`` with what went wrong."""
        try:
            response = await client.request(method, f"{_BASE_URL}{path}", params=params, json=body)
        except httpx.HTTPError as exc:
            msg = f"Could not reach Hevy: {type(exc).__name__}"
            raise _HevyError(msg) from None
        status = response.status_code
        if status in (httpx.codes.OK, httpx.codes.CREATED):
            return response.json() if response.content else {}
        detail = _error_detail(response)
        if status == httpx.codes.UNAUTHORIZED:
            msg = (
                "Hevy rejected the API key. Copy it again from https://hevy.com/settings?developer "
                "into the tool's settings."
            )
        elif status == httpx.codes.FORBIDDEN:
            msg = f"Hevy refused the request{detail}; its API works only while the account has Hevy Pro."
        elif status == httpx.codes.NOT_FOUND:
            msg = f"Hevy has nothing with that id{detail}."
        elif status == httpx.codes.TOO_MANY_REQUESTS:
            msg = "Hevy is rate-limiting requests; try again in a minute."
        elif status == httpx.codes.BAD_REQUEST:
            msg = f"Hevy rejected the request{detail}."
        else:
            msg = f"Hevy returned HTTP {status}{detail}."
        raise _HevyError(msg)

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
    async def get_routine(self, routine_id: str) -> str:
        """Get one routine with its exercises and planned sets.

        Args:
            routine_id: A routine id from `get_routines` or `create_routine`.

        """
        try:
            async with self._client() as client:
                return _dump(await self._get(client, f"/routines/{routine_id.strip()}"))
        except _HevyError as exc:
            return str(exc)

    @tool
    async def create_routine(
        self,
        title: str,
        exercises: list[RoutineExercise],
        notes: str | None = None,
        folder_id: int | None = None,
    ) -> str:
        """Create a routine, a planned workout the user can start in the Hevy app, and return it with its id.

        Args:
            title: Name shown in the app, such as "Mon Nov 16 - Upper Body".
            exercises: The exercises in order, each with its planned sets.
            notes: Notes shown on the routine, such as the day's goal or warm-up.
            folder_id: A folder id from `get_routines` or `create_routine_folder`; omit for "My Routines".

        """
        if not title.strip():
            return "title must not be empty."
        try:
            async with self._client() as client:
                body = _routine_body(title, exercises, notes, folder_id)
                return _dump(await self._request(client, "POST", "/routines", body=body))
        except _HevyError as exc:
            return str(exc)

    @tool
    async def update_routine(
        self,
        routine_id: str,
        title: str,
        exercises: list[RoutineExercise],
        notes: str | None = None,
        folder_id: int | None = None,
    ) -> str:
        """Replace a routine's title, notes, and every exercise and set, and return the updated routine.

        Read it with `get_routine` first and pass everything it should keep, since what you leave out is removed.

        Args:
            routine_id: A routine id from `get_routines` or `create_routine`.
            title: Name shown in the app.
            exercises: The complete new list of exercises in order, each with its planned sets.
            notes: Notes shown on the routine; Hevy never returns them, so pass them again or they are cleared.
            folder_id: Move the routine to this folder id; omit to leave it where it is.

        """
        if not title.strip():
            return "title must not be empty."
        try:
            async with self._client() as client:
                body = _routine_body(title, exercises, notes, folder_id)
                return _dump(await self._request(client, "PUT", f"/routines/{routine_id.strip()}", body=body))
        except _HevyError as exc:
            return str(exc)

    @tool
    async def create_routine_folder(self, title: str) -> str:
        """Create a routine folder, shown first in the app's list of routines, and return it with its id.

        Args:
            title: Name of the folder, such as "MindRoom daily".

        """
        if not title.strip():
            return "title must not be empty."
        try:
            async with self._client() as client:
                body = {"routine_folder": {"title": title.strip()}}
                return _dump(await self._request(client, "POST", "/routine_folders", body=body))
        except _HevyError as exc:
            return str(exc)

    @tool
    async def create_custom_exercise(
        self,
        title: str,
        exercise_type: _ExerciseType,
        equipment_category: _EquipmentCategory,
        muscle_group: _MuscleGroup,
        other_muscles: list[_MuscleGroup] | None = None,
    ) -> str:
        """Create a custom exercise for one that Hevy does not have built in, and return its id.

        Args:
            title: Name of the exercise.
            exercise_type: What each set records, such as weight_reps for weight and reps.
            equipment_category: Equipment the exercise uses.
            muscle_group: Main muscle group worked.
            other_muscles: Other muscle groups worked.

        """
        if not title.strip():
            return "title must not be empty."
        exercise = {
            "title": title.strip(),
            "exercise_type": exercise_type,
            "equipment_category": equipment_category,
            "muscle_group": muscle_group,
            "other_muscles": other_muscles or [],
        }
        try:
            async with self._client() as client:
                return _dump(await self._request(client, "POST", "/exercise_templates", body={"exercise": exercise}))
        except _HevyError as exc:
            return str(exc)

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
