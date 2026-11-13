"""Read-only balances and transactions from accounts linked through SimpleFIN Bridge.

Wire contract: https://www.simplefin.org/protocol.html, version 2.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import unquote, urlsplit, urlunsplit

import httpx

from mindroom.tool_system.authoring import Toolkit, tool

# The bridge fetches from each bank while the request waits, which can take a minute or more.
_TIMEOUT_SECONDS = 120.0
_SECONDS_PER_DAY = 86_400


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

    def __init__(self, access_url: str | None = None) -> None:
        """Store the SimpleFIN Access URL whose accounts the tools read."""
        self._access_url = access_url
        super().__init__()

    async def _get_accounts(self, params: list[tuple[str, str]], *, with_transactions: bool) -> str:
        """Fetch the Account Set and return it as JSON, or a message saying why it could not be read."""
        access = _parse_access_url(self._access_url) if self._access_url else None
        if access is None:
            return (
                "SimpleFIN is not configured: set the tool's Access URL in the dashboard. "
                "It is the https://user:password@host/... URL a Setup Token is exchanged for."
            )
        try:
            async with httpx.AsyncClient(auth=access.auth, timeout=_TIMEOUT_SECONDS) as client:
                response = await client.get(f"{access.base_url}/accounts", params=[("version", "2"), *params])
        except httpx.HTTPError as exc:
            return f"Could not reach SimpleFIN Bridge: {type(exc).__name__}"
        if response.status_code == httpx.codes.FORBIDDEN:
            return (
                "SimpleFIN rejected the Access URL; access was revoked or the URL is wrong. Create a new Setup Token."
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
