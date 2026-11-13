"""Read-only balances and transactions from accounts linked through SimpleFIN Bridge.

Wire contract: https://www.simplefin.org/protocol.html, version 2.
The dashboard stores a one-time Setup Token; the first call claims it and keeps the resulting Access URL in the
``simplefin_access`` credential service with the token's fingerprint, so a newly saved token is claimed again.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from mindroom.credentials.manager import CredentialsManager, load_scoped_credentials, save_scoped_credentials
from mindroom.tool_system.authoring import Toolkit, tool

if TYPE_CHECKING:
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

# The bridge fetches from each bank while the request waits, which can take a minute or more.
_TIMEOUT_SECONDS = 120.0
_CLAIM_TIMEOUT_SECONDS = 30.0
_SECONDS_PER_DAY = 86_400
_ACCESS_SERVICE = "simplefin_access"
# A Setup Token works once, so concurrent first calls must not both try to claim it.
_claim_lock = asyncio.Lock()


class _SetupError(Exception):
    """The Setup Token could not be turned into an Access URL; the message tells the user what to do."""


@dataclass(frozen=True)
class _AccessUrl:
    """An Access URL split into its endpoint and the Basic credentials embedded in it."""

    base_url: str
    auth: httpx.BasicAuth


@dataclass(frozen=True)
class _Transaction:
    id: str
    posted: str | None
    amount: str
    description: str
    pending: bool


@dataclass(frozen=True)
class _Account:
    id: str
    name: str
    institution: str | None
    currency: str
    balance: str
    available_balance: str | None
    balance_date: str
    transactions: list[_Transaction] | None


@dataclass(frozen=True)
class _AccountSet:
    accounts: list[_Account]
    errors: list[str]


def _parse_access_url(access_url: str) -> _AccessUrl | None:
    """Return the endpoint and credentials of an Access URL, or None when it has no HTTPS endpoint or credentials."""
    parts = urlsplit(access_url.strip())
    if parts.scheme != "https" or not parts.hostname or not parts.username or parts.password is None:
        return None
    host = parts.netloc.rpartition("@")[2]
    return _AccessUrl(
        base_url=urlunsplit(("https", host, parts.path.rstrip("/"), "", "")),
        auth=httpx.BasicAuth(unquote(parts.username), unquote(parts.password)),
    )


def _claim_url(setup_token: str) -> str | None:
    """Return the HTTPS claim URL a Setup Token encodes, or None when the text is not a Setup Token."""
    padded = setup_token + "=" * (-len(setup_token) % 4)
    try:
        decoded = base64.b64decode(padded, altchars=b"-_" if "-" in padded or "_" in padded else None, validate=True)
        claim_url = decoded.decode("ascii").strip()
    except (binascii.Error, UnicodeDecodeError):
        return None
    parts = urlsplit(claim_url)
    return claim_url if parts.scheme == "https" and parts.hostname else None


async def _claim(setup_token: str) -> tuple[str, _AccessUrl]:
    """Exchange a Setup Token for its Access URL and its parts, raising ``_SetupError`` with what the user should do."""
    claim_url = _claim_url(setup_token)
    if claim_url is None:
        msg = "The saved SimpleFIN Setup Token is not valid. Copy it again from SimpleFIN Bridge into the dashboard."
        raise _SetupError(msg)
    try:
        async with httpx.AsyncClient(timeout=_CLAIM_TIMEOUT_SECONDS) as client:
            response = await client.post(claim_url)
    except httpx.HTTPError as exc:
        msg = f"Could not reach SimpleFIN Bridge to claim the Setup Token: {type(exc).__name__}"
        raise _SetupError(msg) from None
    if response.status_code == httpx.codes.FORBIDDEN:
        msg = (
            "SimpleFIN says this Setup Token was already used or does not exist. Create a new Setup Token in "
            "SimpleFIN Bridge and save it in the dashboard. If you did not use this one, someone else may have, "
            "so disable that access in SimpleFIN Bridge."
        )
        raise _SetupError(msg)
    access_url = response.text.strip()
    access = _parse_access_url(access_url) if response.status_code == httpx.codes.OK else None
    if access is None:
        msg = f"SimpleFIN Bridge did not return an Access URL for the Setup Token (HTTP {response.status_code})."
        raise _SetupError(msg)
    return access_url, access


def _iso_time(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat()


def _transaction(raw: dict[str, Any]) -> _Transaction:
    posted = int(raw["posted"])
    return _Transaction(
        id=str(raw["id"]),
        posted=_iso_time(posted) if posted else None,
        amount=str(raw["amount"]),
        description=str(raw["description"]),
        pending=bool(raw.get("pending", False)),
    )


def _account_set(payload: dict[str, Any], *, with_transactions: bool) -> _AccountSet:
    """Return the accounts and errors of a version 2 Account Set."""
    institutions = {connection["conn_id"]: connection["name"] for connection in payload.get("connections", [])}
    accounts = [
        _Account(
            id=str(raw["id"]),
            name=str(raw["name"]),
            institution=institutions.get(raw.get("conn_id")),
            currency=str(raw["currency"]),
            balance=str(raw["balance"]),
            available_balance=raw.get("available-balance"),
            balance_date=_iso_time(int(raw["balance-date"])),
            transactions=[_transaction(item) for item in raw.get("transactions", [])] if with_transactions else None,
        )
        for raw in payload.get("accounts", [])
    ]
    errors = [str(error.get("msg") or error.get("code")) for error in payload.get("errlist", [])]
    return _AccountSet(accounts=accounts, errors=errors)


class SimpleFinTools(Toolkit):
    """Read balances and transactions of financial accounts linked in SimpleFIN Bridge."""

    name = "simplefin"
    instructions = (
        "SimpleFIN amounts are decimal strings in the account's currency; "
        "negative transaction amounts are money leaving the account. "
        "Times are UTC, and a pending transaction has no posted time yet."
    )

    def __init__(
        self,
        setup_token: str | None = None,
        *,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
    ) -> None:
        """Store the Setup Token and the credential store that keeps the Access URL it is exchanged for."""
        if credentials_manager is None:
            msg = "SimpleFinTools requires an explicit credentials_manager"
            raise RuntimeError(msg)
        self._setup_token = (setup_token or "").strip()
        self._credentials_manager = credentials_manager
        self._worker_target = worker_target
        super().__init__()

    async def _access(self) -> _AccessUrl:
        """Return the Access URL for the saved Setup Token, claiming the token the first time it is used."""
        if not self._setup_token:
            msg = "SimpleFIN is not configured: save a Setup Token from SimpleFIN Bridge in the tool's settings."
            raise _SetupError(msg)
        fingerprint = hashlib.sha256(self._setup_token.encode()).hexdigest()
        async with _claim_lock:
            stored = load_scoped_credentials(
                _ACCESS_SERVICE,
                credentials_manager=self._credentials_manager,
                worker_target=self._worker_target,
            )
            if stored and stored.get("setup_token_sha256") == fingerprint:
                access = _parse_access_url(str(stored.get("access_url", "")))
                if access is not None:
                    return access
            access_url, access = await _claim(self._setup_token)
            save_scoped_credentials(
                _ACCESS_SERVICE,
                {"access_url": access_url, "setup_token_sha256": fingerprint},
                credentials_manager=self._credentials_manager,
                worker_target=self._worker_target,
            )
        return access

    async def _get_accounts(self, params: list[tuple[str, str]], *, with_transactions: bool) -> str:
        """Fetch the Account Set and return it as JSON, or a message saying why it could not be read."""
        try:
            access = await self._access()
        except _SetupError as exc:
            return str(exc)
        try:
            async with httpx.AsyncClient(auth=access.auth, timeout=_TIMEOUT_SECONDS) as client:
                response = await client.get(f"{access.base_url}/accounts", params=[("version", "2"), *params])
        except httpx.HTTPError as exc:
            return f"Could not reach SimpleFIN Bridge: {type(exc).__name__}"
        if response.status_code == httpx.codes.FORBIDDEN:
            return (
                "SimpleFIN rejected this connection; its access was revoked. "
                "Create a new Setup Token in SimpleFIN Bridge and save it in the tool's settings."
            )
        if response.status_code == httpx.codes.PAYMENT_REQUIRED:
            return "SimpleFIN Bridge requires payment before it returns account data."
        if response.status_code != httpx.codes.OK:
            return f"SimpleFIN Bridge returned HTTP {response.status_code}."
        account_set = _account_set(response.json(), with_transactions=with_transactions)
        return json.dumps(asdict(account_set))

    @tool
    async def list_accounts(self) -> str:
        """List every linked account with its institution, currency, current balance, and available balance."""
        return await self._get_accounts([("balances-only", "1")], with_transactions=False)

    @tool
    async def get_transactions(
        self,
        days: int = 30,
        account_id: str | None = None,
        include_pending: bool = True,
    ) -> str:
        """Get the transactions of the last `days` days, with each account's balance.

        Args:
            days: How many days back to read, counting from now.
            account_id: Read only this account, an id from `list_accounts`; omit to read every account.
            include_pending: Whether to include transactions that have not posted yet.

        """
        if days < 1:
            return "days must be at least 1."
        params = [("start-date", str(int(time.time()) - days * _SECONDS_PER_DAY))]
        if include_pending:
            params.append(("pending", "1"))
        if account_id:
            params.append(("account", account_id))
        return await self._get_accounts(params, with_transactions=True)
