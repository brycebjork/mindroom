"""Garmin Connect sign-in, token reuse, verification codes, and reads against a fake Garmin client."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
from garminconnect import GarminConnectAuthenticationError, GarminConnectTooManyRequestsError

from mindroom.credentials.manager import CredentialsManager
from mindroom.custom_tools import garmin_connect
from mindroom.custom_tools.garmin_connect import GarminConnectTools
from mindroom.tools.garmin_connect import garmin_connect_tools

if TYPE_CHECKING:
    from pathlib import Path

EMAIL = "runner@example.org"
PASSWORD = "correct horse"  # noqa: S105  # fake account
WRONG_PASSWORD = "wrong"  # noqa: S105  # fake account
CODE = "123456"
TIME_ZONE = "Pacific/Kiritimati"


@dataclass
class _Account:
    """What the fake Garmin knows: the password, whether it asks for a code, and the tokens it issued."""

    mfa: bool = False
    valid_tokens: set[str] = field(default_factory=set)
    issued: int = 0
    calls: list[tuple[Any, ...]] = field(default_factory=list)
    read_error: Exception | None = None

    def issue(self) -> str:
        self.issued += 1
        token = f"tokens-{self.issued}"
        self.valid_tokens.add(token)
        return token


class _FakeClient:
    def __init__(self) -> None:
        self.tokens = ""

    def dumps(self) -> str:
        return self.tokens


class _FakeGarmin:
    """Stands in for ``garminconnect.Garmin``, serving the ``account`` the fixture installs."""

    account: _Account

    def __init__(self, email: str | None = None, password: str | None = None, *, return_on_mfa: bool = False) -> None:
        self.email = email
        self.password = password
        self.return_on_mfa = return_on_mfa
        self.client = _FakeClient()

    def login(self, tokenstore: str | None = None) -> tuple[str | None, Any]:
        self.account.calls.append(("login", "tokens" if tokenstore else "password"))
        if tokenstore:
            if tokenstore not in self.account.valid_tokens:
                msg = "tokens expired"
                raise GarminConnectAuthenticationError(msg)
            self.client.tokens = tokenstore
            return None, None
        if self.password != PASSWORD:
            msg = "401 Unauthorized"
            raise GarminConnectAuthenticationError(msg)
        if self.account.mfa:
            return "needs_mfa", {"session": "waiting"}
        self.client.tokens = self.account.issue()
        return None, None

    def resume_login(self, client_state: dict[str, Any], mfa_code: str) -> tuple[None, None]:
        self.account.calls.append(("resume", client_state["session"], mfa_code))
        if mfa_code != CODE:
            msg = "bad code"
            raise GarminConnectAuthenticationError(msg)
        self.client.tokens = self.account.issue()
        return None, None

    def get_user_timezone(self) -> str:
        return TIME_ZONE

    def _read(self, name: str, *args: object) -> dict[str, Any]:
        self.account.calls.append((name, *args))
        if self.account.read_error is not None:
            raise self.account.read_error
        return {"read": name, "args": list(args)}

    def get_user_summary(self, cdate: str) -> dict[str, Any]:
        return self._read("get_user_summary", cdate)

    def get_activities_by_date(self, startdate: str, enddate: str | None, activitytype: str | None) -> dict[str, Any]:
        return self._read("get_activities_by_date", startdate, enddate, activitytype)

    def get_body_battery(self, startdate: str, enddate: str) -> dict[str, Any]:
        # Garmin rotates tokens while reading; the toolkit must keep the new ones.
        self.client.tokens = self.account.issue()
        return self._read("get_body_battery", startdate, enddate)


@pytest.fixture
def account(monkeypatch: pytest.MonkeyPatch) -> _Account:
    """Install a fake Garmin client and return the account it serves."""
    fake_account = _Account()
    monkeypatch.setattr(_FakeGarmin, "account", fake_account, raising=False)
    monkeypatch.setattr(garmin_connect, "Garmin", _FakeGarmin)
    return fake_account


@pytest.fixture
def credentials(tmp_path: Path) -> CredentialsManager:
    """Return an empty credential store for one test."""
    return CredentialsManager(tmp_path / "credentials")


def _tools(
    credentials: CredentialsManager,
    *,
    email: str | None = EMAIL,
    password: str | None = PASSWORD,
    mfa_code: str | None = None,
) -> GarminConnectTools:
    return GarminConnectTools(email=email, password=password, mfa_code=mfa_code, credentials_manager=credentials)


def _stored(credentials: CredentialsManager) -> dict[str, Any]:
    return credentials.load_credentials("garmin_connect_access") or {}


def test_registered_factory_returns_the_toolkit(credentials: CredentialsManager) -> None:
    """The registry builds the toolkit with every read function, which needs a credential store."""
    assert garmin_connect_tools() is GarminConnectTools
    assert set(_tools(credentials).functions) == {
        "get_activities",
        "get_body_battery",
        "get_daily_steps",
        "get_daily_summary",
        "get_heart_rate",
        "get_hrv",
        "get_sleep",
        "get_stress",
        "get_weigh_ins",
    }
    with pytest.raises(RuntimeError, match="credentials_manager"):
        GarminConnectTools(email=EMAIL, password=PASSWORD)


@pytest.mark.parametrize(("email", "password"), [(None, None), (EMAIL, None), ("  ", PASSWORD)])
@pytest.mark.asyncio
async def test_missing_account_settings_ask_for_configuration(
    account: _Account,
    credentials: CredentialsManager,
    email: str | None,
    password: str | None,
) -> None:
    """Without an email and password the tool says how to set it up and contacts nothing."""
    result = await _tools(credentials, email=email, password=password).get_daily_summary("2026-11-01")

    assert result.startswith("Garmin Connect is not configured")
    assert account.calls == []


@pytest.mark.asyncio
async def test_first_call_signs_in_and_later_toolkits_reuse_the_tokens(
    account: _Account,
    credentials: CredentialsManager,
) -> None:
    """The password is used once; a later toolkit signs in with the saved tokens."""
    first = json.loads(await _tools(credentials).get_daily_summary("2026-11-01"))
    await _tools(credentials).get_daily_summary("2026-11-02")

    assert first == {"read": "get_user_summary", "args": ["2026-11-01"]}
    assert [call for call in account.calls if call[0] == "login"] == [("login", "password"), ("login", "tokens")]
    assert _stored(credentials)["tokens"] == "tokens-1"
    assert PASSWORD not in json.dumps(_stored(credentials))


@pytest.mark.asyncio
async def test_rejected_password_is_not_tried_again_until_it_changes(
    account: _Account,
    credentials: CredentialsManager,
) -> None:
    """A rejected email and password are remembered so repeated calls cannot lock the account."""
    first = await _tools(credentials, password=WRONG_PASSWORD).get_daily_summary("2026-11-01")
    second = await _tools(credentials, password=WRONG_PASSWORD).get_daily_summary("2026-11-01")
    fixed = json.loads(await _tools(credentials).get_daily_summary("2026-11-01"))

    assert first.startswith("Garmin rejected the saved email and password.")
    assert second == first
    assert [call for call in account.calls if call[0] == "login"] == [("login", "password"), ("login", "password")]
    assert fixed["read"] == "get_user_summary"


@pytest.mark.asyncio
async def test_verification_code_completes_a_two_factor_sign_in(
    account: _Account,
    credentials: CredentialsManager,
) -> None:
    """A sign-in waits for the code without starting again, then the saved code finishes it."""
    account.mfa = True

    asked = await _tools(credentials).get_daily_summary("2026-11-01")
    still_waiting = await _tools(credentials).get_daily_summary("2026-11-01")
    done = json.loads(await _tools(credentials, mfa_code=CODE).get_daily_summary("2026-11-01"))

    assert asked.startswith("Garmin sent a verification code")
    assert still_waiting == asked
    assert account.calls[:2] == [("login", "password"), ("resume", "waiting", CODE)]
    assert done["read"] == "get_user_summary"
    assert "mfa_state" not in _stored(credentials)
    assert _stored(credentials)["tokens"] == "tokens-1"


@pytest.mark.asyncio
async def test_a_rejected_verification_code_is_not_reused(
    account: _Account,
    credentials: CredentialsManager,
) -> None:
    """After a wrong code the next call starts a new sign-in and waits for a new code instead of retrying it."""
    account.mfa = True
    await _tools(credentials).get_daily_summary("2026-11-01")

    rejected = await _tools(credentials, mfa_code="000000").get_daily_summary("2026-11-01")
    restarted = await _tools(credentials, mfa_code="000000").get_daily_summary("2026-11-01")

    assert rejected.startswith("Garmin did not accept that verification code")
    assert restarted.startswith("Garmin sent a verification code")
    assert account.calls == [("login", "password"), ("resume", "waiting", "000000"), ("login", "password")]


@pytest.mark.asyncio
async def test_expired_tokens_fall_back_to_signing_in(account: _Account, credentials: CredentialsManager) -> None:
    """Tokens Garmin no longer accepts are replaced by a fresh sign-in with the saved password."""
    await _tools(credentials).get_daily_summary("2026-11-01")
    account.valid_tokens.clear()

    result = json.loads(await _tools(credentials).get_daily_summary("2026-11-02"))

    assert result["read"] == "get_user_summary"
    assert [call for call in account.calls if call[0] == "login"] == [
        ("login", "password"),
        ("login", "tokens"),
        ("login", "password"),
    ]
    assert _stored(credentials)["tokens"] == "tokens-2"


@pytest.mark.asyncio
async def test_tokens_garmin_refreshes_during_a_read_are_kept(
    account: _Account,
    credentials: CredentialsManager,
) -> None:
    """Rotated tokens are saved, so the next toolkit does not sign in with the password again."""
    await _tools(credentials).get_body_battery("2026-11-01")

    assert _stored(credentials)["tokens"] == "tokens-2"
    assert ("get_body_battery", "2026-11-01", "2026-11-01") in account.calls


@pytest.mark.usefixtures("account")
@pytest.mark.asyncio
async def test_today_follows_the_garmin_profile_time_zone(credentials: CredentialsManager) -> None:
    """An omitted day is today on the calendar of the user's Garmin time zone."""
    result = json.loads(await _tools(credentials).get_daily_summary())

    assert result["args"] == [datetime.now(ZoneInfo(TIME_ZONE)).date().isoformat()]


@pytest.mark.usefixtures("account")
@pytest.mark.asyncio
async def test_activities_pass_the_range_and_type(credentials: CredentialsManager) -> None:
    """Activities are read for the chosen range and type."""
    result = json.loads(await _tools(credentials).get_activities("2026-10-01", "2026-10-31", "running"))

    assert result == {"read": "get_activities_by_date", "args": ["2026-10-01", "2026-10-31", "running"]}


@pytest.mark.asyncio
async def test_rate_limit_during_a_read_is_explained(account: _Account, credentials: CredentialsManager) -> None:
    """Garmin's request limit tells the user to wait instead of retrying."""
    account.read_error = GarminConnectTooManyRequestsError("429")

    result = await _tools(credentials).get_daily_summary("2026-11-01")

    assert result == "Garmin is limiting requests from this server. Wait a few minutes before asking again."


@pytest.mark.asyncio
async def test_a_session_garmin_ends_during_a_read_is_dropped(
    account: _Account,
    credentials: CredentialsManager,
) -> None:
    """When Garmin rejects the session mid-read, its tokens are discarded so the next call signs in again."""
    await _tools(credentials).get_daily_summary("2026-11-01")
    account.read_error = GarminConnectAuthenticationError("session ended")

    result = await _tools(credentials).get_daily_summary("2026-11-02")

    assert result.startswith("Garmin ended the saved sign-in.")
    assert "tokens" not in _stored(credentials)
