"""SimpleFIN tool requests and responses against a fake SimpleFIN Bridge."""

from __future__ import annotations

import base64
import json
import time
from typing import TYPE_CHECKING

import httpx
import pytest

from mindroom.custom_tools import simplefin
from mindroom.custom_tools.simplefin import SimpleFinTools
from mindroom.tools.simplefin import simplefin_tools

if TYPE_CHECKING:
    from collections.abc import Callable

ACCESS_URL = "https://user%40example:s3cr%2Ft@bridge.example.org/simplefin"
ACCOUNT_SET = {
    "errlist": [{"code": "con.auth", "msg": "Reconnect your bank", "conn_id": "CON-1"}],
    "connections": [{"conn_id": "CON-1", "name": "First Bank", "org_id": "ORG-1", "sfin_url": "https://b"}],
    "accounts": [
        {
            "id": "ACT-1",
            "name": "Checking",
            "conn_id": "CON-1",
            "currency": "USD",
            "balance": "1234.56",
            "available-balance": "1200.00",
            "balance-date": 1_700_000_000,
            "transactions": [
                {"id": "TRN-1", "posted": 1_699_990_000, "amount": "-42.10", "description": "Grocer"},
                {"id": "TRN-2", "posted": 0, "amount": "-5.00", "description": "Coffee", "pending": True},
            ],
        },
    ],
}


def _bridge(
    monkeypatch: pytest.MonkeyPatch,
    respond: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Route the tool's HTTP client to ``respond`` and return the requests it receives."""
    requests: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return respond(request)

    def client(**kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(simplefin.httpx, "AsyncClient", client)
    return requests


def test_registered_factory_returns_the_toolkit() -> None:
    """The registry builds the toolkit that offers both functions."""
    assert simplefin_tools() is SimpleFinTools
    assert set(SimpleFinTools().functions) == {"list_accounts", "get_transactions"}


@pytest.mark.asyncio
async def test_list_accounts_reads_balances_with_the_embedded_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credentials leave the URL for a Basic header, and only balances are requested."""
    requests = _bridge(monkeypatch, lambda _: httpx.Response(200, json=ACCOUNT_SET))

    result = json.loads(await SimpleFinTools(access_url=ACCESS_URL).list_accounts())

    [request] = requests
    assert request.url.host == "bridge.example.org"
    assert request.url.path == "/simplefin/accounts"
    assert request.url.userinfo == b""
    assert dict(request.url.params) == {"version": "2", "balances-only": "1"}
    assert request.headers["authorization"] == "Basic " + base64.b64encode(b"user@example:s3cr/t").decode()
    assert result == {
        "accounts": [
            {
                "id": "ACT-1",
                "name": "Checking",
                "institution": "First Bank",
                "currency": "USD",
                "balance": "1234.56",
                "available_balance": "1200.00",
                "balance_date": "2023-11-14T22:13:20+00:00",
                "transactions": None,
            },
        ],
        "errors": ["Reconnect your bank"],
    }


@pytest.mark.asyncio
async def test_get_transactions_filters_by_days_account_and_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """The request asks for the chosen window and account, and pending transactions have no posted time."""
    requests = _bridge(monkeypatch, lambda _: httpx.Response(200, json=ACCOUNT_SET))
    before = int(time.time())

    result = json.loads(await SimpleFinTools(access_url=ACCESS_URL).get_transactions(days=7, account_id="ACT-1"))

    params = requests[0].url.params
    assert before - 7 * 86_400 <= int(params["start-date"]) <= int(time.time()) - 7 * 86_400
    assert params["pending"] == "1"
    assert params.get_list("account") == ["ACT-1"]
    assert "balances-only" not in params
    assert result["accounts"][0]["transactions"] == [
        {
            "id": "TRN-1",
            "posted": "2023-11-14T19:26:40+00:00",
            "amount": "-42.10",
            "description": "Grocer",
            "pending": False,
        },
        {"id": "TRN-2", "posted": None, "amount": "-5.00", "description": "Coffee", "pending": True},
    ]


@pytest.mark.asyncio
async def test_get_transactions_can_leave_out_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without pending, the request omits the flag and names no account."""
    requests = _bridge(
        monkeypatch,
        lambda _: httpx.Response(200, json={"errlist": [], "connections": [], "accounts": []}),
    )

    result = json.loads(await SimpleFinTools(access_url=ACCESS_URL).get_transactions(include_pending=False))

    assert set(requests[0].url.params) == {"version", "start-date"}
    assert result == {"accounts": [], "errors": []}


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (403, "SimpleFIN rejected the Access URL; access was revoked or the URL is wrong. Create a new Setup Token."),
        (402, "SimpleFIN Bridge requires payment before it returns account data."),
        (500, "SimpleFIN Bridge returned HTTP 500."),
    ],
)
@pytest.mark.asyncio
async def test_bridge_refusals_are_explained(monkeypatch: pytest.MonkeyPatch, status: int, message: str) -> None:
    """Each refusal the protocol defines tells the user what to do."""
    _bridge(monkeypatch, lambda _: httpx.Response(status))

    assert await SimpleFinTools(access_url=ACCESS_URL).list_accounts() == message


@pytest.mark.asyncio
async def test_connection_failure_does_not_reveal_the_access_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transport error names only its kind, never the URL or its credentials."""

    def refuse(request: httpx.Request) -> httpx.Response:
        msg = f"refused {request.url}"
        raise httpx.ConnectError(msg, request=request)

    _bridge(monkeypatch, refuse)

    assert (
        await SimpleFinTools(access_url=ACCESS_URL).list_accounts() == "Could not reach SimpleFIN Bridge: ConnectError"
    )


@pytest.mark.parametrize(
    "access_url",
    [None, "", "http://user:pass@bridge.example.org/simplefin", "https://bridge.example.org/simplefin"],
)
@pytest.mark.asyncio
async def test_unusable_access_url_asks_for_configuration(
    monkeypatch: pytest.MonkeyPatch,
    access_url: str | None,
) -> None:
    """A missing, plain-HTTP, or credential-less Access URL sends nothing."""
    requests = _bridge(monkeypatch, lambda _: httpx.Response(200, json=ACCOUNT_SET))

    result = await SimpleFinTools(access_url=access_url).get_transactions()

    assert result.startswith("SimpleFIN is not configured")
    assert requests == []


@pytest.mark.asyncio
async def test_get_transactions_rejects_an_empty_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """A window shorter than a day is refused before any request."""
    requests = _bridge(monkeypatch, lambda _: httpx.Response(200, json=ACCOUNT_SET))

    assert await SimpleFinTools(access_url=ACCESS_URL).get_transactions(days=0) == "days must be at least 1."
    assert requests == []
