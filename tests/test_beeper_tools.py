"""Beeper tool requests and responses against a fake Beeper Desktop API."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

from mindroom.custom_tools import beeper
from mindroom.custom_tools.beeper import BeeperTools
from mindroom.tools.beeper import beeper_tools

if TYPE_CHECKING:
    from collections.abc import Callable

BASE_URL = "http://beeper:23373"
TOKEN = "test-token"  # noqa: S105
CHAT_ID = "!signal_abc:local-signal.localhost"


def _api(
    monkeypatch: pytest.MonkeyPatch,
    respond: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Route the tool's HTTP clients to a fake Beeper API and return the requests it receives."""
    requests: list[httpx.Request] = []
    real_client = httpx.AsyncClient

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return respond(request)

    def client(**kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(beeper.httpx, "AsyncClient", client)
    return requests


def _json_api(monkeypatch: pytest.MonkeyPatch, status: int, payload: object) -> list[httpx.Request]:
    return _api(monkeypatch, lambda _: httpx.Response(status, json=payload))


def _tools() -> BeeperTools:
    return BeeperTools(base_url=f"{BASE_URL}/", access_token=TOKEN)


def _body(request: httpx.Request) -> object:
    return json.loads(request.content)


def test_registered_factory_returns_the_toolkit() -> None:
    """The registry builds the toolkit with every function the metadata lists."""
    assert beeper_tools() is BeeperTools
    assert set(_tools().functions) == {
        "edit_message",
        "get_chat",
        "list_accounts",
        "list_chats",
        "list_messages",
        "react",
        "search_contacts",
        "search_messages",
        "send_message",
        "set_draft",
        "set_reminder",
        "start_chat",
        "update_chat",
    }


@pytest.mark.asyncio
async def test_missing_settings_explain_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an address or token the tool says what to save and sends nothing."""
    requests = _json_api(monkeypatch, 200, [])
    blank_token = " "  # noqa: S105
    for tools in (BeeperTools(access_token=TOKEN), BeeperTools(base_url=BASE_URL, access_token=blank_token)):
        assert "Settings > Integrations" in await tools.list_accounts()
    assert requests == []


@pytest.mark.asyncio
async def test_accounts_send_the_token_and_keep_identity_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Accounts list the network account id and the user's name, without avatars."""
    requests = _json_api(
        monkeypatch,
        200,
        [{"accountID": "local-whatsapp_ba_1", "user": {"id": "ba_1", "fullName": "Bryce", "imgURL": "file:///x.jpg"}}],
    )
    result = json.loads(await _tools().list_accounts())
    assert result == [{"accountID": "local-whatsapp_ba_1", "fullName": "Bryce"}]
    assert str(requests[0].url) == f"{BASE_URL}/v1/accounts"
    assert requests[0].headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.asyncio
async def test_list_chats_sends_filters_and_compacts_chats(monkeypatch: pytest.MonkeyPatch) -> None:
    """Filters become the API's query parameters, and chats show participant names and the last message."""
    chat = {
        "id": CHAT_ID,
        "accountID": "local-signal_1",
        "title": "Alex",
        "type": "single",
        "unreadCount": 2,
        "isMuted": True,
        "isArchived": False,
        "participants": {
            "items": [{"id": "me", "fullName": "Bryce", "isSelf": True}, {"id": "a", "fullName": "Alex"}],
            "hasMore": False,
        },
        "preview": {"senderName": "Alex", "text": "x" * 400},
    }
    requests = _json_api(monkeypatch, 200, {"items": [chat], "hasMore": True, "oldestCursor": "c1"})
    result = json.loads(
        await _tools().list_chats(query=" alex ", unread_only=True, account_ids=["a1", "a2"], limit=500),
    )
    params = requests[0].url.params
    assert requests[0].url.path == "/v1/chats/search"
    assert params["query"] == "alex"
    assert params["unreadOnly"] == "true"
    assert params.get_list("accountIDs") == ["a1", "a2"]
    assert params["limit"] == "200"
    assert "cursor" not in params
    assert "inbox" not in params
    assert result["hasMore"] is True
    assert result["olderCursor"] == "c1"
    item = result["items"][0]
    assert item["participants"] == ["Alex"]
    assert item["isMuted"] is True
    assert "isArchived" not in item
    assert len(item["lastMessage"]["text"]) == 280


@pytest.mark.asyncio
async def test_list_messages_reads_oldest_first_and_pages_by_sort_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Messages come back in reading order, without local file paths, with a cursor for earlier ones."""
    messages = [
        {
            "id": "m2",
            "sortKey": "200",
            "timestamp": "2026-11-26T10:00:00Z",
            "senderName": "Alex",
            "text": "see photo",
            "attachments": [{"type": "img", "fileName": "a.jpg", "srcURL": "file:///secret/a.jpg"}],
            "reactions": [{"reactionKey": "👍", "participantID": "me"}],
        },
        {"id": "m1", "sortKey": "100", "timestamp": "2026-11-26T09:00:00Z", "isSender": True, "text": "hi"},
    ]
    requests = _json_api(monkeypatch, 200, {"items": messages, "hasMore": True})
    result = json.loads(await _tools().list_messages(f" {CHAT_ID} ", cursor="900"))
    assert requests[0].url.path == f"/v1/chats/{CHAT_ID}/messages"
    assert requests[0].url.params["direction"] == "before"
    assert [message["id"] for message in result["items"]] == ["m1", "m2"]
    assert result["items"][1]["attachments"] == [{"type": "img", "fileName": "a.jpg"}]
    assert result["items"][1]["reactions"] == ["👍"]
    assert result["olderCursor"] == "100"
    assert "srcURL" not in json.dumps(result)


@pytest.mark.asyncio
async def test_search_messages_caps_the_page_and_names_chats(monkeypatch: pytest.MonkeyPatch) -> None:
    """Search sends repeated array parameters, caps the page at 20, and maps chat ids to titles."""
    payload = {
        "items": [{"id": "m1", "chatID": CHAT_ID, "text": "dinner friday?"}],
        "chats": {CHAT_ID: {"id": CHAT_ID, "title": "Alex"}},
        "hasMore": False,
    }
    requests = _json_api(monkeypatch, 200, payload)
    result = json.loads(
        await _tools().search_messages("dinner", chat_ids=[CHAT_ID], sender="others", media="link", limit=50),
    )
    params = requests[0].url.params
    assert params.get_list("chatIDs") == [CHAT_ID]
    assert params.get_list("mediaTypes") == ["link"]
    assert params["limit"] == "20"
    assert params["sender"] == "others"
    assert result["chatTitles"] == {CHAT_ID: "Alex"}
    assert "olderCursor" not in result


@pytest.mark.asyncio
async def test_send_message_posts_text_and_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    """A send posts the text and reply target and returns the pending message id."""
    requests = _json_api(monkeypatch, 200, {"chatID": CHAT_ID, "pendingMessageID": "m169"})
    result = json.loads(await _tools().send_message(CHAT_ID, "On my way", reply_to_message_id="m1"))
    assert requests[0].method == "POST"
    assert requests[0].url.path == f"/v1/chats/{CHAT_ID}/messages"
    assert _body(requests[0]) == {"text": "On my way", "replyToMessageID": "m1"}
    assert result == {"sent": True, "chatID": CHAT_ID, "pendingMessageID": "m169"}


@pytest.mark.asyncio
async def test_blank_text_is_refused_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty messages, edits, reactions, drafts, and contact searches never reach Beeper."""
    requests = _json_api(monkeypatch, 200, {})
    tools = _tools()
    assert await tools.send_message(CHAT_ID, "  ") == "text must not be empty."
    assert await tools.edit_message(CHAT_ID, "m1", "") == "text must not be empty."
    assert await tools.react(CHAT_ID, "m1", " ") == "emoji must not be empty."
    assert "null to clear" in await tools.set_draft(CHAT_ID, " ")
    assert await tools.search_contacts("a1", " ") == "query must not be empty."
    assert await tools.update_chat(CHAT_ID) == "Nothing to change: set archived, muted, pinned, or read."
    assert await tools.start_chat("a1") == "Give user_id, phone_number, email, or username."
    assert requests == []


@pytest.mark.asyncio
async def test_set_draft_clears_before_writing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Beeper takes a new draft only into an empty box, so the tool clears it first; null only clears."""
    requests = _json_api(monkeypatch, 200, {"id": CHAT_ID})
    tools = _tools()
    assert json.loads(await tools.set_draft(CHAT_ID, "Sounds good!")) == {"draft": "Sounds good!"}
    assert json.loads(await tools.set_draft(CHAT_ID, None)) == {"draft": None}
    assert [(request.method, _body(request)) for request in requests] == [
        ("PATCH", {"draft": None}),
        ("PATCH", {"draft": {"text": "Sounds good!"}}),
        ("PATCH", {"draft": None}),
    ]


@pytest.mark.asyncio
async def test_update_chat_patches_flags_and_marks_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the given flags are sent, and read state uses its own endpoint."""
    requests = _json_api(monkeypatch, 200, {"id": CHAT_ID})
    result = json.loads(await _tools().update_chat(CHAT_ID, archived=True, pinned=False, read=True))
    assert result == {"updated": {"isArchived": True, "isPinned": False, "read": True}}
    assert (requests[0].method, requests[0].url.path, _body(requests[0])) == (
        "PATCH",
        f"/v1/chats/{CHAT_ID}",
        {"isArchived": True, "isPinned": False},
    )
    assert (requests[1].method, requests[1].url.path) == ("POST", f"/v1/chats/{CHAT_ID}/read")


@pytest.mark.asyncio
async def test_react_adds_and_removes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adding posts the reaction key; removing deletes it by key."""
    requests = _json_api(monkeypatch, 200, {"success": True})
    tools = _tools()
    await tools.react(CHAT_ID, "m1", "❤️")
    await tools.react(CHAT_ID, "m1", "❤️", remove=True)
    assert requests[0].method == "POST"
    assert _body(requests[0]) == {"reactionKey": "❤️"}
    assert requests[1].method == "DELETE"
    assert requests[1].url.path == f"/v1/chats/{CHAT_ID}/messages/m1/reactions/❤️"


@pytest.mark.asyncio
async def test_reminders_need_an_offset_and_null_removes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reminder time without a UTC offset is refused; null deletes the chat's reminder."""
    requests = _json_api(monkeypatch, 200, {})
    tools = _tools()
    assert "UTC offset" in await tools.set_reminder(CHAT_ID, "2026-11-27T09:00:00")
    assert await tools.set_reminder(CHAT_ID, "tomorrow") == "remind_at must be an ISO 8601 time."
    assert json.loads(await tools.set_reminder(CHAT_ID, "2026-11-27T09:00:00-07:00", dismiss_on_incoming_message=True))
    assert json.loads(await tools.set_reminder(CHAT_ID, None)) == {"reminder": None}
    assert _body(requests[0]) == {
        "reminder": {"remindAt": "2026-11-27T09:00:00-07:00", "dismissOnIncomingMessage": True},
    }
    assert requests[1].method == "DELETE"
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_start_chat_resolves_a_person(monkeypatch: pytest.MonkeyPatch) -> None:
    """Starting a chat sends only the identifiers given and returns the chat."""
    requests = _json_api(monkeypatch, 200, {"id": CHAT_ID, "chatID": CHAT_ID, "title": "Alex", "unreadCount": 0})
    result = json.loads(await _tools().start_chat("a1", phone_number=" +15551234567 ", full_name="Alex"))
    assert _body(requests[0]) == {"accountID": "a1", "user": {"phoneNumber": "+15551234567", "fullName": "Alex"}}
    assert result["id"] == CHAT_ID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (401, {}, "Beeper rejected the access token."),
        (403, {"message": "insufficient scope"}, "Beeper refused the request: insufficient scope;"),
        (404, {"message": "Chat not found"}, "Beeper has nothing with that id: Chat not found."),
        (400, {"message": "text is required", "code": "invalid"}, "Beeper rejected the request: text is required."),
        (500, {}, "Beeper returned HTTP 500."),
    ],
)
async def test_http_errors_become_messages(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    payload: object,
    expected: str,
) -> None:
    """Beeper's errors reach the model as text it can act on."""
    _json_api(monkeypatch, status, payload)
    assert (await _tools().get_chat(CHAT_ID)).startswith(expected)


@pytest.mark.asyncio
async def test_network_errors_name_the_address(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreachable Beeper names the address without leaking the token."""

    def refuse(request: httpx.Request) -> httpx.Response:
        msg = "refused"
        raise httpx.ConnectError(msg, request=request)

    _api(monkeypatch, refuse)
    result = await _tools().list_chats()
    assert result == f"Could not reach Beeper at {BASE_URL} (ConnectError); check that it is running."
    assert TOKEN not in result
