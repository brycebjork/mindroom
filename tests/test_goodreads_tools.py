"""Goodreads tool requests and responses against a fake Goodreads shelf feed."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
import pytest

from mindroom.custom_tools import goodreads
from mindroom.custom_tools.goodreads import GoodreadsTools
from mindroom.tools.goodreads import goodreads_tools

if TYPE_CHECKING:
    from collections.abc import Callable

FEED_KEY = "test-feed-key"
_REAL_CLIENT = httpx.AsyncClient
RSS_LINK = f"https://www.goodreads.com/review/list_rss/12345?key={FEED_KEY}&shelf=read"


def _item(
    book_id: str,
    title: str,
    *,
    author: str = "Ursula K. Le Guin",
    read_at: str = "",
    rating: int = 0,
    shelves: str = "",
    review: str = "",
    pages: str = "",
) -> str:
    return f"""
    <item>
      <guid><![CDATA[https://www.goodreads.com/review/show/{book_id}?utm_medium=api]]></guid>
      <pubDate><![CDATA[Mon, 06 Mar 2017 12:02:40 -0800]]></pubDate>
      <title>{title}</title>
      <link><![CDATA[https://www.goodreads.com/review/show/{book_id}?utm_medium=api&utm_source=rss]]></link>
      <book_id>{book_id}</book_id>
      <book_description><![CDATA[A long description.]]></book_description>
      <book id="{book_id}"><num_pages>{pages}</num_pages></book>
      <author_name>{author}</author_name>
      <isbn></isbn>
      <user_name>Ann</user_name>
      <user_rating>{rating}</user_rating>
      <user_read_at><![CDATA[{read_at}]]></user_read_at>
      <user_date_added><![CDATA[Mon, 06 Mar 2017 12:02:40 -0800]]></user_date_added>
      <user_date_created><![CDATA[Mon, 06 Mar 2017 12:02:40 -0800]]></user_date_created>
      <user_shelves>{shelves}</user_shelves>
      <user_review><![CDATA[{review}]]></user_review>
      <average_rating>4.12</average_rating>
      <book_published>1969</book_published>
      <description><![CDATA[html summary]]></description>
    </item>"""


def _feed(items: list[str]) -> str:
    return (
        '<?xml version="1.0"?><rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
        "<title>Ann's bookshelf: read</title>" + "".join(items) + "</channel></rss>"
    )


def _api(
    monkeypatch: pytest.MonkeyPatch,
    respond: Callable[[httpx.Request], httpx.Response],
) -> list[httpx.Request]:
    """Route the tool's HTTP clients to a fake Goodreads and return the requests it receives."""
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return respond(request)

    def client(**kwargs: object) -> httpx.AsyncClient:
        return _REAL_CLIENT(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(goodreads.httpx, "AsyncClient", client)
    return requests


def _pages(pages: list[list[str]]) -> Callable[[httpx.Request], httpx.Response]:
    """Serve ``pages`` of feed items, then empty feeds past the last page."""

    def respond(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(200, text=_feed(pages[page - 1] if page <= len(pages) else []))

    return respond


def test_registered_factory_returns_the_toolkit() -> None:
    """The registry builds the toolkit with every read function."""
    assert goodreads_tools() is GoodreadsTools
    assert set(GoodreadsTools(secret_rss_link=RSS_LINK).functions) == {"find_my_books", "get_books_read", "get_shelf"}


@pytest.mark.asyncio
async def test_missing_link_explains_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a link the tool says where to find one and sends nothing."""
    requests = _api(monkeypatch, _pages([]))
    result = await GoodreadsTools(secret_rss_link=" ").get_shelf()
    assert "RSS link" in result
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "link",
    [
        "https://evil.example/review/list_rss/12345?key=x",
        "https://www.goodreads.com.evil.example/review/list_rss/12345",
        "https://www.goodreads.com/book/show/77566",
    ],
)
async def test_links_that_are_not_a_goodreads_account_are_refused(
    monkeypatch: pytest.MonkeyPatch,
    link: str,
) -> None:
    """Only goodreads.com account links are read, so a saved link can never send requests elsewhere."""
    requests = _api(monkeypatch, _pages([]))
    result = await GoodreadsTools(secret_rss_link=link).get_shelf()
    assert "RSS link" in result
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("link", "key"),
    [
        (RSS_LINK, FEED_KEY),
        ("www.goodreads.com/review/list/12345-ann?shelf=to-read", None),
        ("https://www.goodreads.com/user/show/12345-ann", None),
    ],
)
async def test_feed_requests_go_to_goodreads_with_the_key(
    monkeypatch: pytest.MonkeyPatch,
    link: str,
    key: str | None,
) -> None:
    """RSS, shelf, and profile links all read the account's feed, sending the key only when the link has one."""
    requests = _api(monkeypatch, _pages([[_item("1", "Dune")]]))
    await GoodreadsTools(secret_rss_link=link).get_shelf("to-read")
    url = requests[0].url
    assert (url.host, url.path) == ("www.goodreads.com", "/review/list_rss/12345")
    assert url.params["shelf"] == "to-read"
    assert url.params.get("key") == key


@pytest.mark.asyncio
async def test_shelf_books_are_parsed_without_empty_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """A book carries its rating, dates, shelves, plain-text review, and clean link, without empty or unrated fields."""
    items = [
        _item(
            "1",
            "The Dispossessed",
            read_at="Sat, 12 Oct 2024 00:00:00 -0700",
            rating=5,
            shelves="sci-fi, favorites",
            review="Loved it.<br />Anarres &amp; Urras.",
            pages="387",
        ),
        _item("2", "The Lathe of Heaven"),
    ]
    _api(monkeypatch, _pages([items]))
    result = json.loads(await GoodreadsTools(secret_rss_link=RSS_LINK).get_shelf("read"))
    assert result["books"][0] == {
        "book_id": "1",
        "title": "The Dispossessed",
        "author": "Ursula K. Le Guin",
        "pages": 387,
        "published": 1969,
        "average_rating": 4.12,
        "my_rating": 5,
        "shelves": ["sci-fi", "favorites"],
        "date_read": "2024-10-12",
        "date_added": "2017-03-06",
        "review": "Loved it.\nAnarres & Urras.",
        "link": "https://www.goodreads.com/review/show/1",
    }
    assert {"my_rating", "date_read", "review", "pages", "shelves", "isbn"}.isdisjoint(result["books"][1])


@pytest.mark.asyncio
async def test_shelf_pages_until_the_feed_runs_out_or_repeats(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paging stops on an empty page, on a page that repeats earlier books, and once the limit is reached."""
    first = [_item(str(n), f"Book {n}") for n in range(3)]
    requests = _api(monkeypatch, _pages([first, [_item("3", "Book 3")]]))
    result = json.loads(await GoodreadsTools(secret_rss_link=RSS_LINK).get_shelf("read"))
    assert len(result["books"]) == 4
    assert [request.url.params["page"] for request in requests] == ["1", "2", "3"]

    requests = _api(monkeypatch, lambda _: httpx.Response(200, text=_feed(first)))
    result = json.loads(await GoodreadsTools(secret_rss_link=RSS_LINK).get_shelf("read"))
    assert len(result["books"]) == 3
    assert len(requests) == 2

    requests = _api(monkeypatch, _pages([first, first]))
    result = json.loads(await GoodreadsTools(secret_rss_link=RSS_LINK).get_shelf("read", limit=2))
    assert [book["book_id"] for book in result["books"]] == ["0", "1"]
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_find_my_books_matches_title_and_author_on_every_shelf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every query word must appear in the title or author, and the search reads the all-shelves feed."""
    items = [
        _item("1", "The Left Hand of Darkness"),
        _item("2", "Dune", author="Frank Herbert"),
        _item("3", "Children of Dune", author="Frank Herbert"),
    ]
    requests = _api(monkeypatch, _pages([items]))
    result = json.loads(await GoodreadsTools(secret_rss_link=RSS_LINK).find_my_books("dune HERBERT"))
    assert [book["book_id"] for book in result["books"]] == ["2", "3"]
    assert requests[0].url.params["shelf"] == "#ALL#"
    assert await GoodreadsTools(secret_rss_link=RSS_LINK).find_my_books("  ") == "query must name a title or author."


@pytest.mark.asyncio
async def test_books_read_filters_by_date_and_totals_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Books read in the range come newest first with their count and pages, and undated reads are listed apart."""
    items = [
        _item("1", "Early", read_at="Sun, 05 Jan 2025 00:00:00 -0800", pages="100"),
        _item("2", "Late", read_at="Wed, 10 Dec 2025 00:00:00 -0800", pages="250"),
        _item("3", "Last year", read_at="Tue, 31 Dec 2024 00:00:00 -0800", pages="999"),
        _item("4", "No pages", read_at="Tue, 01 Jul 2025 00:00:00 -0700"),
        _item("5", "Undated"),
    ]
    requests = _api(monkeypatch, _pages([items]))
    result = json.loads(await GoodreadsTools(secret_rss_link=RSS_LINK).get_books_read("2025-01-01", "2025-12-31"))
    assert [book["title"] for book in result["books"]] == ["Late", "No pages", "Early"]
    assert (result["count"], result["pages"]) == (3, 350)
    assert result["read_without_date"] == ["Undated"]
    assert requests[0].url.params["shelf"] == "read"
    assert await GoodreadsTools(secret_rss_link=RSS_LINK).get_books_read("2025/01/01") == "Dates must be YYYY-MM-DD."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(404, text="Page not found"), "no account with the saved link's user id"),
        (httpx.Response(429), "rate-limiting"),
        (httpx.Response(503), "HTTP 503"),
        (httpx.Response(200, text="<!DOCTYPE html><html><body>Sign in</body></html>"), "profile is private"),
        (httpx.Response(200, text="<html><body>Sign in</body></html>"), "profile is private"),
    ],
)
async def test_goodreads_failures_become_messages(
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
    expected: str,
) -> None:
    """HTTP errors and pages that are not a feed come back as instructions instead of exceptions."""
    _api(monkeypatch, lambda _: response)
    assert expected in await GoodreadsTools(secret_rss_link=RSS_LINK).get_shelf()


@pytest.mark.asyncio
async def test_network_errors_become_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection failure says Goodreads could not be reached without leaking the URL and its key."""

    def respond(request: httpx.Request) -> httpx.Response:
        msg = f"failed {request.url}"
        raise httpx.ConnectError(msg, request=request)

    _api(monkeypatch, respond)
    result = await GoodreadsTools(secret_rss_link=RSS_LINK).get_shelf()
    assert result == "Could not reach Goodreads: ConnectError"


@pytest.mark.asyncio
async def test_entity_expansion_in_the_feed_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A feed that declares entities is rejected instead of being expanded."""
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY a "aaaaaaaaaa">]><rss><channel><title>&a;</title></channel></rss>'
    )
    _api(monkeypatch, lambda _: httpx.Response(200, text=bomb))
    assert "did not return the shelf feed" in await GoodreadsTools(secret_rss_link=RSS_LINK).get_shelf()


@pytest.mark.asyncio
async def test_arguments_are_checked_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank shelf or an out-of-range limit is refused without reading the feed."""
    requests = _api(monkeypatch, _pages([]))
    tools = GoodreadsTools(secret_rss_link=RSS_LINK)
    assert await tools.get_shelf(" ") == "shelf must name a shelf."
    assert await tools.get_shelf(limit=0) == "limit must be between 1 and 500."
    assert requests == []
