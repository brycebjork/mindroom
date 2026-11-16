"""Goodreads tool requests and responses against fake signed-in Goodreads shelf pages."""

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

_REAL_CLIENT = httpx.AsyncClient
COOKIE = "at-main=test-at; sess-at-main=test-sess; _session_id2=test-session"
USER_ID = "12345"


def _td(field: str, value: str) -> str:
    return f'<td class="field {field}"><label>{field}</label><div class="value">{value}</div></td>'


def _date_row(field: str, value: str | None) -> str:
    inner = f'<span class="{field}_value">{value}</span>' if value else '<span class="greyText">not set</span>'
    edit = '<a class="floatingBoxLink smallText" href="#">[edit]</a>'
    return f'<div class="date_row"><div class="editable_date {field}_x">{inner}{edit}</div></div>'


def _row(
    review_id: str,
    title: str,
    *,
    book_id: str | None = None,
    author: str = "Le Guin, Ursula K.",
    author_href: str = "/author/show/874602.Ursula_K_Le_Guin",
    rating: str = "0.0",
    shelves: tuple[str, ...] = ("read",),
    read: tuple[str | None, ...] = (None,),
    started: str | None = None,
    review: str = "",
    pages: str = "",
    published: str = "1969",
) -> str:
    book_id = book_id or review_id
    shelf_links = "".join(
        f'<a class="shelfLink" title="View all books in Ann&#39;s {name} shelf." '
        f'href="https://www.goodreads.com/review/list/{USER_ID}?shelf={name}">{name}</a>'
        for name in shelves
    )
    review_html = (
        f'<span id="freeTextContainer{review_id}">{review[:10]}</span>'
        f'<span id="freeText{review_id}" style="display:none">{review}</span>'
        if review
        else f'<a href="/review/edit/{book_id}?report_event=true">Write a review</a>'
    )
    cells = [
        _td(
            "cover",
            f'<a href="/book/show/{book_id}-x"><img src="https://i.gr-assets.com/{book_id}._SX50_.jpg" /></a>',
        ),
        _td("title", f'<a title="{title}" href="/book/show/{book_id}-slug">\n {title}\n</a>'),
        _td("author", f'<a href="{author_href}">{author}</a> <span title="Goodreads Author!">*</span>'),
        _td("isbn", "\n"),
        _td("isbn13", "  9780000000001\n"),
        _td("num_pages", f'<nobr>\n {pages}\n <span class="greyText">pp</span></nobr>' if pages else ""),
        _td("avg_rating", "  4.12\n"),
        _td("date_pub", f"  {published}\n"),
        _td("rating", f'<div class="stars" data-resource-id="{book_id}" data-rating="{rating}"></div>'),
        _td("shelves", f'<span id="shelfList">{shelf_links}</span><br /><a class="shelfChooserLink">[edit]</a>'),
        _td("review", review_html),
        _td("date_started", _date_row("date_started", started)),
        _td("date_read", "".join(_date_row("date_read", day) for day in read)),
        _td("date_added", '<span title="March 6, 2017">\n Mar 06, 2017\n</span>'),
        _td("format", '\n ebook\n <a class="smallText" href="/work/editions/1">[edit]</a>'),
    ]
    return f'<tr id="review_{review_id}" class="bookalike review">{"".join(cells)}</tr>'


def _page(rows: list[str]) -> str:
    return (
        "<html><body><div id='header'><a href='/user/show/12345-ann'>Ann</a></div>"
        f'<table id="books"><tbody id="booksBody">{"".join(rows)}</tbody></table></body></html>'
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
    monkeypatch.setattr(goodreads, "_PAGE_DELAY_SECONDS", 0)
    return requests


def _site(pages: list[list[str]]) -> Callable[[httpx.Request], httpx.Response]:
    """Redirect "My Books" to the account's list and serve ``pages`` of rows, then empty pages."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/review/list":
            return httpx.Response(302, headers={"location": f"https://www.goodreads.com/review/list/{USER_ID}-ann"})
        page = int(request.url.params["page"])
        return httpx.Response(200, text=_page(pages[page - 1] if page <= len(pages) else []))

    return respond


def _tools(**kwargs: str) -> GoodreadsTools:
    return GoodreadsTools(session_token=COOKIE, **kwargs)


def test_registered_factory_returns_the_toolkit() -> None:
    """The registry builds the toolkit with every read function."""
    assert goodreads_tools() is GoodreadsTools
    assert set(_tools().functions) == {"find_my_books", "get_books_read", "get_shelf"}


@pytest.mark.asyncio
async def test_missing_cookie_explains_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a cookie the tool says how to copy one and sends nothing."""
    requests = _api(monkeypatch, _site([]))
    blank = " "
    result = await GoodreadsTools(session_token=blank).get_shelf()
    assert "Cookie request header" in result
    assert requests == []


@pytest.mark.asyncio
async def test_requests_carry_the_cookie_only_to_goodreads(monkeypatch: pytest.MonkeyPatch) -> None:
    """The account comes from where My Books redirects, and every request goes to www.goodreads.com with the cookie."""
    requests = _api(monkeypatch, _site([[_row("1", "Dune")]]))
    pasted = f"Cookie: {COOKIE}\n"
    await GoodreadsTools(session_token=pasted).get_shelf("to-read")
    assert [request.url.path for request in requests] == ["/review/list", f"/review/list/{USER_ID}"]
    assert {request.url.host for request in requests} == {"www.goodreads.com"}
    assert {request.headers["cookie"] for request in requests} == {COOKIE}
    params = requests[1].url.params
    assert (params["shelf"], params["per_page"], params["sort"], params["order"]) == (
        "to-read",
        "100",
        "date_added",
        "d",
    )


@pytest.mark.asyncio
async def test_saved_profile_link_skips_the_account_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """A saved profile link names the account, so only the shelf is requested."""
    requests = _api(monkeypatch, _site([[_row("1", "Dune")]]))
    await _tools(profile_url="https://www.goodreads.com/user/show/777-ann").get_shelf()
    assert [request.url.path for request in requests] == ["/review/list/777"]


@pytest.mark.asyncio
async def test_account_is_found_in_the_page_when_my_books_does_not_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    """When My Books answers directly, the account id comes from its links."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/review/list":
            return httpx.Response(200, text='<a href="https://www.goodreads.com/review/list/4242?shelf=read">read</a>')
        return httpx.Response(200, text=_page([]))

    requests = _api(monkeypatch, respond)
    await _tools().get_shelf()
    assert requests[1].url.path == "/review/list/4242"


@pytest.mark.asyncio
async def test_unknown_account_asks_for_the_profile_link(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without any account id to find, the tool asks for the profile link."""
    _api(monkeypatch, lambda _: httpx.Response(200, text="<html>Welcome</html>"))
    assert "profile link" in await _tools().get_shelf()


@pytest.mark.asyncio
async def test_shelf_rows_become_books_without_empty_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """A row gives its rating, dates, shelves, full review, and links, and empty or unrated fields are left out."""
    rows = [
        _row(
            "901",
            "The Dispossessed",
            book_id="13651",
            rating="5.0",
            shelves=("read", "sci-fi"),
            read=("Mar 03, 2020", "Oct 12, 2024"),
            started="Oct 1, 2024",
            review="Loved it.<br />Anarres &amp; Urras.",
            pages="1,387",
            published="Nov 2010",
        ),
        _row("902", "Death&#39;s End", author="Liu, Cixin", author_href="/author/show/5780686.Liu_Cixin"),
    ]
    _api(monkeypatch, _site([rows]))
    result = json.loads(await _tools().get_shelf("read"))
    assert result["books"][0] == {
        "book_id": "13651",
        "title": "The Dispossessed",
        "author": "Ursula K. Le Guin",
        "isbn": "9780000000001",
        "pages": 1387,
        "published": "2010-11",
        "average_rating": 4.12,
        "my_rating": 5,
        "shelves": ["read", "sci-fi"],
        "format": "ebook",
        "date_started": "2024-10-01",
        "date_read": "2024-10-12",
        "date_added": "2017-03-06",
        "review": "Loved it.\nAnarres & Urras.",
        "link": "https://www.goodreads.com/review/show/901",
    }
    second = result["books"][1]
    assert (second["title"], second["author"], second["published"]) == ("Death's End", "Liu Cixin", "1969")
    assert {"my_rating", "date_read", "date_started", "review", "pages"}.isdisjoint(second)


@pytest.mark.asyncio
async def test_shelf_pages_until_a_short_or_repeated_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Paging stops after a page shorter than 100 rows, after a page of rows already seen, and at the limit."""
    full = [_row(str(n), f"Book {n}") for n in range(100)]
    requests = _api(monkeypatch, _site([full, [_row("100", "Book 100")]]))
    result = json.loads(await _tools().get_shelf("read", limit=500))
    assert len(result["books"]) == 101
    assert [request.url.params.get("page") for request in requests[1:]] == ["1", "2"]

    requests = _api(monkeypatch, _site([full, full, full]))
    result = json.loads(await _tools().get_shelf("read", limit=500))
    assert len(result["books"]) == 100
    assert len(requests) == 3

    requests = _api(monkeypatch, _site([full, full]))
    result = json.loads(await _tools().get_shelf("read", limit=2))
    assert [book["book_id"] for book in result["books"]] == ["0", "1"]
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_find_my_books_matches_title_and_author_on_every_shelf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every query word must appear in the title or author, and the search reads all shelves."""
    rows = [
        _row("1", "The Left Hand of Darkness"),
        _row("2", "Dune", author="Herbert, Frank", author_href="/author/show/58.Frank_Herbert"),
        _row("3", "Children of Dune", author="Herbert, Frank", author_href="/author/show/58.Frank_Herbert"),
    ]
    requests = _api(monkeypatch, _site([rows]))
    result = json.loads(await _tools().find_my_books("dune HERBERT"))
    assert [book["book_id"] for book in result["books"]] == ["2", "3"]
    assert requests[1].url.params["shelf"] == "#ALL#"
    assert await _tools().find_my_books("  ") == "query must name a title or author."


@pytest.mark.asyncio
async def test_books_read_filters_by_date_and_totals_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Books read in the range come newest first with their count and pages, partial dates match, and undated reads are listed apart."""
    rows = [
        _row("1", "Early", read=("Jan 05, 2025",), pages="100"),
        _row("2", "Late", read=("Dec 10, 2025",), pages="250"),
        _row("3", "Last year", read=("Dec 31, 2024",), pages="999"),
        _row("4", "Month only", read=("Jul 2025",)),
        _row("5", "Undated"),
    ]
    requests = _api(monkeypatch, _site([rows]))
    result = json.loads(await _tools().get_books_read("2025-01-01", "2025-12-31"))
    assert [book["title"] for book in result["books"]] == ["Late", "Month only", "Early"]
    assert (result["count"], result["pages"]) == (3, 350)
    assert result["read_without_date"] == ["Undated"]
    assert requests[1].url.params["shelf"] == "read"
    assert await _tools().get_books_read("2025/01/01") == "Dates must be YYYY-MM-DD."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(302, headers={"location": "https://www.goodreads.com/user/sign_in"}), "signed out or expired"),
        (httpx.Response(302, headers={"location": "https://www.goodreads.com/somewhere"}), "did not show the shelf"),
        (httpx.Response(200, text="<html><form action='/user/sign_in'></form></html>"), "did not show the shelf"),
        (httpx.Response(202, headers={"x-amzn-waf-action": "challenge"}), "firewall challenged"),
        (httpx.Response(429), "rate-limiting"),
        (httpx.Response(404), "no such page"),
        (httpx.Response(503), "HTTP 503"),
    ],
)
async def test_goodreads_failures_become_messages(
    monkeypatch: pytest.MonkeyPatch,
    response: httpx.Response,
    expected: str,
) -> None:
    """Sign-in redirects, firewall challenges, and HTTP errors come back as instructions, and redirects are not followed."""
    requests = _api(monkeypatch, lambda _: response)
    assert expected in await _tools(profile_url="https://www.goodreads.com/user/show/1").get_shelf()
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_network_errors_become_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection failure says Goodreads could not be reached without repeating the request."""

    def respond(request: httpx.Request) -> httpx.Response:
        msg = f"failed {request.headers['cookie']}"
        raise httpx.ConnectError(msg, request=request)

    _api(monkeypatch, respond)
    assert await _tools().get_shelf() == "Could not reach Goodreads: ConnectError"


@pytest.mark.asyncio
async def test_arguments_are_checked_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank shelf or an out-of-range limit is refused without reading Goodreads."""
    requests = _api(monkeypatch, _site([]))
    tools = _tools()
    assert await tools.get_shelf(" ") == "shelf must name a shelf."
    assert await tools.get_shelf(limit=0) == "limit must be between 1 and 500."
    assert requests == []
