"""Read-only Garmin Connect health and activity data through the community ``garminconnect`` client.

Garmin offers no public API for personal accounts, so this signs in the way Garmin's mobile app does.
The first call signs in with the saved email and password and keeps Garmin's tokens in the
``garmin_connect_access`` credential service; later calls reuse and refresh them.
An account with two-factor authentication pauses the sign-in until the user saves the code Garmin sent,
and an email and password Garmin rejected are not tried again until they change,
because repeated failed sign-ins can lock a Garmin account.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from mindroom.credentials.manager import CredentialsManager, load_scoped_credentials, save_scoped_credentials
from mindroom.tool_system.authoring import Toolkit, tool

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_ACCESS_SERVICE = "garmin_connect_access"
# Sign-ins and token saves must not interleave, or two calls could start two sign-ins or overwrite fresher tokens.
_session_lock = threading.Lock()

_NOT_CONFIGURED = "Garmin Connect is not configured: save your Garmin email and password in the tool's settings."
_MFA_NEEDED = (
    "Garmin sent a verification code for this sign-in. Save it as the Garmin Connect tool's MFA code in the "
    "dashboard, then ask again within a few minutes."
)
_REJECTED = (
    "Garmin rejected the saved email and password. Update them in the Garmin Connect tool's settings; "
    "MindRoom will not try them again until they change, so the account is not locked."
)
_RATE_LIMITED = "Garmin is limiting requests from this server. Wait a few minutes before asking again."

_ActivityType = Literal[
    "cycling",
    "running",
    "swimming",
    "multi_sport",
    "fitness_equipment",
    "hiking",
    "walking",
    "other",
]


class _SetupError(Exception):
    """Garmin data could not be read; the message tells the user what to do."""


def _fingerprint(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _today(garmin: Garmin) -> str:
    """Return today's date in the user's Garmin time zone, which is the calendar Garmin's days follow."""
    zone_name = garmin.get_user_timezone()
    try:
        zone = ZoneInfo(zone_name) if zone_name else None
    except (ZoneInfoNotFoundError, ValueError):
        zone = None
    return datetime.now(zone).date().isoformat()


class GarminConnectTools(Toolkit):
    """Read health and activity data from the user's Garmin Connect account."""

    name = "garmin_connect"
    instructions = (
        "Garmin Connect tools return Garmin's own JSON. Dates are YYYY-MM-DD in the user's Garmin time zone; "
        "omit a day for today. When a result says Garmin needs a verification code or rejected the sign-in, "
        "tell the user and do not call Garmin tools again until they update the tool's settings."
    )

    def __init__(
        self,
        email: str | None = None,
        password: str | None = None,
        mfa_code: str | None = None,
        *,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
    ) -> None:
        """Store the account settings and the credential store that keeps Garmin's tokens."""
        if credentials_manager is None:
            msg = "GarminConnectTools requires an explicit credentials_manager"
            raise RuntimeError(msg)
        self._email = (email or "").strip()
        self._password = password or ""
        self._mfa_code = (mfa_code or "").strip()
        self._credentials_manager = credentials_manager
        self._worker_target = worker_target
        self._garmin: Garmin | None = None
        super().__init__()

    def _load(self) -> dict[str, Any]:
        return (
            load_scoped_credentials(
                _ACCESS_SERVICE,
                credentials_manager=self._credentials_manager,
                worker_target=self._worker_target,
            )
            or {}
        )

    def _save(self, record: dict[str, Any]) -> None:
        save_scoped_credentials(
            _ACCESS_SERVICE,
            record,
            credentials_manager=self._credentials_manager,
            worker_target=self._worker_target,
        )

    def _keep(self, garmin: Garmin, record: dict[str, Any]) -> Garmin:
        """Save the signed-in session's tokens, ending any pending verification or rejection, and use it."""
        record = {key: value for key, value in record.items() if key not in {"mfa_state", "rejected"}}
        self._save({**record, "tokens": garmin.client.dumps()})
        self._garmin = garmin
        return garmin

    def _resume_with_code(self, record: dict[str, Any]) -> Garmin:
        """Finish a sign-in that waits for a verification code, trying each saved code only once."""
        code_fingerprint = _fingerprint(self._mfa_code)
        if not self._mfa_code or code_fingerprint == record.get("used_mfa_code_sha256"):
            raise _SetupError(_MFA_NEEDED)
        garmin = Garmin(self._email, self._password)
        try:
            garmin.resume_login(record["mfa_state"], self._mfa_code)
        except GarminConnectAuthenticationError:
            # The waiting sign-in is spent; the next call starts a new one, and Garmin sends a new code.
            remaining = {key: value for key, value in record.items() if key != "mfa_state"}
            self._save({**remaining, "used_mfa_code_sha256": code_fingerprint})
            msg = (
                "Garmin did not accept that verification code, or it expired. Ask again to have Garmin send a "
                "new code, then save the new code as the tool's MFA code."
            )
            raise _SetupError(msg) from None
        return self._keep(garmin, {**record, "used_mfa_code_sha256": code_fingerprint})

    def _resume_with_tokens(self, record: dict[str, Any]) -> Garmin | None:
        """Sign in with the saved tokens, or return None when Garmin no longer accepts them."""
        # Without an email and password, rejected tokens raise instead of starting a sign-in here.
        garmin = Garmin()
        try:
            garmin.login(record["tokens"])
        except GarminConnectAuthenticationError:
            return None
        return self._keep(garmin, record)

    def _sign_in(self) -> Garmin:
        """Return a signed-in client, reusing saved tokens; call with ``_session_lock`` held."""
        if self._garmin is not None:
            return self._garmin
        if not self._email or not self._password:
            raise _SetupError(_NOT_CONFIGURED)
        account = _fingerprint(self._email, self._password)
        record = self._load()
        if record.get("account_sha256") != account:
            record = {"account_sha256": account, "used_mfa_code_sha256": record.get("used_mfa_code_sha256")}
        if "tokens" in record:
            garmin = self._resume_with_tokens(record)
            if garmin is not None:
                return garmin
            record = {key: value for key, value in record.items() if key != "tokens"}
        if record.get("rejected"):
            raise _SetupError(_REJECTED)
        if "mfa_state" in record:
            return self._resume_with_code(record)
        garmin = Garmin(self._email, self._password, return_on_mfa=True)
        try:
            status, mfa_state = garmin.login()
        except GarminConnectAuthenticationError:
            self._save({**record, "rejected": True})
            raise _SetupError(_REJECTED) from None
        if status == "needs_mfa":
            self._save({**record, "mfa_state": mfa_state})
            raise _SetupError(_MFA_NEEDED)
        return self._keep(garmin, record)

    def _call(self, read: Callable[[Garmin], Any]) -> Any:  # noqa: ANN401  # Garmin's JSON has no fixed shape
        """Run one Garmin read, signing in first, and keep tokens Garmin refreshed during it."""
        try:
            with _session_lock:
                garmin = self._sign_in()
            result = read(garmin)
        except GarminConnectAuthenticationError:
            with _session_lock:
                self._garmin = None
                record = self._load()
                self._save({key: value for key, value in record.items() if key != "tokens"})
            msg = "Garmin ended the saved sign-in. Ask again to sign in with the saved email and password."
            raise _SetupError(msg) from None
        except GarminConnectTooManyRequestsError:
            raise _SetupError(_RATE_LIMITED) from None
        except GarminConnectConnectionError as exc:
            msg = f"Garmin Connect request failed: {exc}"
            raise _SetupError(msg) from None
        except ValueError as exc:
            raise _SetupError(str(exc)) from None
        with _session_lock:
            record = self._load()
            tokens = garmin.client.dumps()
            if record.get("tokens") != tokens:
                self._save({**record, "tokens": tokens})
        return result

    async def _read(self, read: Callable[[Garmin], Any]) -> str:
        """Run a blocking Garmin read off the event loop and return its JSON, or what the user should do."""
        try:
            result = await asyncio.to_thread(self._call, read)
        except _SetupError as exc:
            return str(exc)
        return json.dumps(result, default=str)

    @tool
    async def get_daily_summary(self, day: str | None = None) -> str:
        """Get one day's totals: steps, distance, calories, intensity minutes, resting heart rate, stress, and Body Battery.

        Args:
            day: The day as YYYY-MM-DD; omit for today.

        """
        return await self._read(lambda garmin: garmin.get_user_summary(day or _today(garmin)))

    @tool
    async def get_sleep(self, day: str | None = None) -> str:
        """Get the sleep that ended on one day: stages, duration, sleep score, and overnight readings.

        Args:
            day: The day the sleep ended, as YYYY-MM-DD; omit for last night.

        """
        return await self._read(lambda garmin: garmin.get_sleep_data(day or _today(garmin)))

    @tool
    async def get_heart_rate(self, day: str | None = None) -> str:
        """Get one day's heart rate: resting, minimum, maximum, and readings through the day.

        Args:
            day: The day as YYYY-MM-DD; omit for today.

        """
        return await self._read(lambda garmin: garmin.get_heart_rates(day or _today(garmin)))

    @tool
    async def get_hrv(self, day: str | None = None) -> str:
        """Get one night's heart rate variability (HRV) summary and status.

        Args:
            day: The day the night ended, as YYYY-MM-DD; omit for last night.

        """
        return await self._read(lambda garmin: garmin.get_hrv_data(day or _today(garmin)))

    @tool
    async def get_stress(self, day: str | None = None) -> str:
        """Get one day's stress levels: average, maximum, and readings through the day.

        Args:
            day: The day as YYYY-MM-DD; omit for today.

        """
        return await self._read(lambda garmin: garmin.get_stress_data(day or _today(garmin)))

    @tool
    async def get_body_battery(self, start_date: str, end_date: str | None = None) -> str:
        """Get Body Battery charged, drained, and levels for each day in a range.

        Args:
            start_date: First day as YYYY-MM-DD.
            end_date: Last day as YYYY-MM-DD; omit for just the first day.

        """
        return await self._read(lambda garmin: garmin.get_body_battery(start_date, end_date or start_date))

    @tool
    async def get_daily_steps(self, start_date: str, end_date: str | None = None) -> str:
        """Get the step count, distance, and step goal of each day in a range.

        Args:
            start_date: First day as YYYY-MM-DD.
            end_date: Last day as YYYY-MM-DD; omit for just the first day.

        """
        return await self._read(lambda garmin: garmin.get_daily_steps(start_date, end_date or start_date))

    @tool
    async def get_activities(
        self,
        start_date: str,
        end_date: str | None = None,
        activity_type: _ActivityType | None = None,
    ) -> str:
        """Get recorded activities in a date range with type, duration, distance, calories, and heart rate.

        Args:
            start_date: First day as YYYY-MM-DD.
            end_date: Last day as YYYY-MM-DD; omit to include every activity since the first day.
            activity_type: Only activities of this type; omit for all types.

        """
        return await self._read(lambda garmin: garmin.get_activities_by_date(start_date, end_date, activity_type))

    @tool
    async def get_weigh_ins(self, start_date: str, end_date: str | None = None) -> str:
        """Get weight and body composition measurements in a date range.

        Args:
            start_date: First day as YYYY-MM-DD.
            end_date: Last day as YYYY-MM-DD; omit for just the first day.

        """
        return await self._read(lambda garmin: garmin.get_weigh_ins(start_date, end_date or start_date))
