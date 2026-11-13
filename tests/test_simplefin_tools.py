"""SimpleFIN tool setup, requests, and responses against a fake SimpleFIN Bridge."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import TYPE_CHECKING

import httpx
import pytest

from mindroom.credentials.manager import CredentialsManager
from mindroom.custom_tools import simplefin
from mindroom.custom_tools.simplefin import SimpleFinTools
from mindroom.tools.simplefin import simplefin_tools

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

CLAIM_URL = "https://bridge.example.org/simplefin/claim/DEMO-1"
SETUP_TOKEN = base64.b64encode(CLAIM_URL.encode()).decode()
ACCESS_URL = "https://user%40example:s3cr%2Ft@bridge.example.org/simplefin"
EMPTY_SET = {"errlist": [], "connections": [], "accounts": []}
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
    accounts: Callable[[httpx.Request], httpx.Response],
    claim: Callable[[httpx.Request], httpx.Response] = lambda _: httpx.Response(200, text=ACCESS_URL),
) -> list[httpx.Request]:
    """Route the tool's HTTP clients to a fake bridge and return the requests it receives."""
    requests: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return claim(request) if request.method == "POST" else accounts(request)

    def client(**kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(simplefin.httpx, "AsyncClient", client)
    return requests


@pytest.fixture
def credentials(tmp_path: Path) -> CredentialsManager:
    """Return an empty credential store for one test."""
    return CredentialsManager(tmp_path / "credentials")


def _tools(credentials: CredentialsManager, setup_token: str | None = SETUP_TOKEN) -> SimpleFinTools:
    return SimpleFinTools(setup_token=setup_token, credentials_manager=credentials)


def test_registered_factory_returns_the_toolkit(credentials: CredentialsManager) -> None:
    """The registry builds the toolkit that offers both functions, which needs a credential store."""
    assert simplefin_tools() is SimpleFinTools
    assert set(_tools(credentials).functions) == {"list_accounts", "get_transactions"}
    with pytest.raises(RuntimeError, match="credentials_manager"):
        SimpleFinTools(setup_token=SETUP_TOKEN)


@pytest.mark.asyncio
async def test_setup_token_is_claimed_once_and_its_access_url_reused(
    monkeypatch: pytest.MonkeyPatch,
    credentials: CredentialsManager,
) -> None:
    """The first call claims the token; later calls, even from a new toolkit, read with the saved Access URL."""
    requests = _bridge(monkeypatch, lambda _: httpx.Response(200, json=EMPTY_SET))

    await _tools(credentials).list_accounts()
    await _tools(credentials).list_accounts()

    assert [(request.method, str(request.url.copy_with(query=None))) for request in requests] == [
        ("POST", CLAIM_URL),
        ("GET", "https://bridge.example.org/simplefin/accounts"),
        ("GET", "https://bridge.example.org/simplefin/accounts"),
    ]
    assert credentials.load_credentials("simplefin_access") == {
        "access_url": ACCESS_URL,
        "setup_token_sha256": hashlib.sha256(SETUP_TOKEN.encode()).hexdigest(),
    }


@pytest.mark.asyncio
async def test_a_newly_saved_setup_token_is_claimed(
    monkeypatch: pytest.MonkeyPatch,
    credentials: CredentialsManager,
) -> None:
    """Saving a different token reconnects with the Access URL that token is exchanged for."""
    requests = _bridge(monkeypatch, lambda _: httpx.Response(200, json=EMPTY_SET))
    await _tools(credentials).list_accounts()
    new_claim = "https://bridge.example.org/simplefin/claim/DEMO-2"

    await _tools(credentials, base64.b64encode(new_claim.encode()).decode()).list_accounts()

    assert [str(request.url) for request in requests if request.method == "POST"] == [CLAIM_URL, new_claim]


@pytest.mark.asyncio
async def test_list_accounts_reads_balances_with_the_embedded_credentials(
    monkeypatch: pytest.MonkeyPatch,
    credentials: CredentialsManager,
) -> None:
    """Credentials leave the Access URL for a Basic header, and only balances are requested."""
    requests = _bridge(monkeypatch, lambda _: httpx.Response(200, json=ACCOUNT_SET))

    result = json.loads(await _tools(credentials).list_accounts())

    request = requests[-1]
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
async def test_get_transactions_filters_by_days_account_and_pending(
    monkeypatch: pytest.MonkeyPatch,
    credentials: CredentialsManager,
) -> None:
    """The request asks for the chosen window and account, and pending transactions have no posted time."""
    requests = _bridge(monkeypatch, lambda _: httpx.Response(200, json=ACCOUNT_SET))
    before = int(time.time())

    result = json.loads(await _tools(credentials).get_transactions(days=7, account_id="ACT-1"))

    params = requests[-1].url.params
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
async def test_get_transactions_can_leave_out_pending(
    monkeypatch: pytest.MonkeyPatch,
    credentials: CredentialsManager,
) -> None:
    """Without pending, the request omits the flag and names no account."""
    requests = _bridge(monkeypatch, lambda _: httpx.Response(200, json=EMPTY_SET))

    result = json.loads(await _tools(credentials).get_transactions(include_pending=False))

    assert set(requests[-1].url.params) == {"version", "start-date"}
    assert result == {"accounts": [], "errors": []}


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (
            403,
            "SimpleFIN rejected this connection; its access was revoked. "
            "Create a new Setup Token in SimpleFIN Bridge and save it in the tool's settings.",
        ),
        (402, "SimpleFIN Bridge requires payment before it returns account data."),
        (500, "SimpleFIN Bridge returned HTTP 500."),
    ],
)
@pytest.mark.asyncio
async def test_bridge_refusals_are_explained(
    monkeypatch: pytest.MonkeyPatch,
    credentials: CredentialsManager,
    status: int,
    message: str,
) -> None:
    """Each refusal the protocol defines tells the user what to do."""
    _bridge(monkeypatch, lambda _: httpx.Response(status))

    assert await _tools(credentials).list_accounts() == message


@pytest.mark.asyncio
async def test_a_used_setup_token_is_explained_and_nothing_is_saved(
    monkeypatch: pytest.MonkeyPatch,
    credentials: CredentialsManager,
) -> None:
    """A token the bridge refuses asks for a new one, warns it may have been taken, and reads no accounts."""
    requests = _bridge(
        monkeypatch,
        lambda _: httpx.Response(200, json=ACCOUNT_SET),
        claim=lambda _: httpx.Response(403),
    )

    result = await _tools(credentials).list_accounts()

    assert result.startswith("SimpleFIN says this Setup Token was already used or does not exist.")
    assert "someone else may have" in result
    assert [request.method for request in requests] == ["POST"]
    assert credentials.load_credentials("simplefin_access") is None


@pytest.mark.parametrize(
    "setup_token",
    [
        "not a token!",
        base64.b64encode(b"http://bridge.example.org/simplefin/claim/DEMO-1").decode(),
        base64.b64encode(b"hello").decode(),
    ],
)
@pytest.mark.asyncio
async def test_text_that_is_not_a_setup_token_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
    credentials: CredentialsManager,
    setup_token: str,
) -> None:
    """Text that does not decode to an HTTPS claim URL is refused before any request."""
    requests = _bridge(monkeypatch, lambda _: httpx.Response(200, json=ACCOUNT_SET))

    result = await _tools(credentials, setup_token).list_accounts()

    assert result.startswith("The saved SimpleFIN Setup Token is not valid.")
    assert requests == []


@pytest.mark.parametrize("setup_token", [None, "", "   "])
@pytest.mark.asyncio
async def test_missing_setup_token_asks_for_configuration(
    monkeypatch: pytest.MonkeyPatch,
    credentials: CredentialsManager,
    setup_token: str | None,
) -> None:
    """Without a token the tool says how to set it up and sends nothing."""
    requests = _bridge(monkeypatch, lambda _: httpx.Response(200, json=ACCOUNT_SET))

    result = await _tools(credentials, setup_token).get_transactions()

    assert result.startswith("SimpleFIN is not configured")
    assert requests == []


@pytest.mark.asyncio
async def test_a_claim_that_returns_no_access_url_is_explained(
    monkeypatch: pytest.MonkeyPatch,
    credentials: CredentialsManager,
) -> None:
    """A claim answer that is not an HTTPS Access URL with credentials is not saved or used."""
    requests = _bridge(
        monkeypatch,
        lambda _: httpx.Response(200, json=ACCOUNT_SET),
        claim=lambda _: httpx.Response(200, text="<html>maintenance</html>"),
    )

    result = await _tools(credentials).list_accounts()

    assert result == "SimpleFIN Bridge did not return an Access URL for the Setup Token (HTTP 200)."
    assert [request.method for request in requests] == ["POST"]
    assert credentials.load_credentials("simplefin_access") is None


@pytest.mark.asyncio
async def test_connection_failure_does_not_reveal_the_access_url(
    monkeypatch: pytest.MonkeyPatch,
    credentials: CredentialsManager,
) -> None:
    """A transport error names only its kind, never the URL or its credentials."""

    def refuse(request: httpx.Request) -> httpx.Response:
        msg = f"refused {request.url}"
        raise httpx.ConnectError(msg, request=request)

    _bridge(monkeypatch, refuse)

    assert await _tools(credentials).list_accounts() == "Could not reach SimpleFIN Bridge: ConnectError"


@pytest.mark.asyncio
async def test_get_transactions_rejects_an_empty_window(
    monkeypatch: pytest.MonkeyPatch,
    credentials: CredentialsManager,
) -> None:
    """A window shorter than a day is refused before any request."""
    requests = _bridge(monkeypatch, lambda _: httpx.Response(200, json=ACCOUNT_SET))

    assert await _tools(credentials).get_transactions(days=0) == "days must be at least 1."
    assert requests == []
