"""Books on the user's Goodreads shelves, read from the signed-in shelf pages with the user's browser session.

Goodreads stopped issuing API keys in December 2020, retired its shelf RSS feeds around 2026-10-28, and now shows
shelf pages only to signed-in readers.
Accounts that sign in with Amazon have no Goodreads password, so the tool never signs in: it sends the Cookie header
the user copies from a signed-in browser and reads https://www.goodreads.com/review/list/<user id>, the "My Books"
table.
The tool is read-only and only ever requests www.goodreads.com, never following redirects, so the cookie stays there.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import date, datetime
from typing import Any

import httpx

from mindroom.tool_system.authoring import Toolkit, tool

_BASE_URL = "https://www.goodreads.com"
_ALL_SHELVES = "#ALL#"
_TIMEOUT_SECONDS = 30.0
# The cookie came from a browser, and Goodreads' firewall challenges clients that do not look like one.
_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
_PER_PAGE = 100
# Reads up to 5,000 books of one shelf.
_MAX_PAGES = 50
_PAGE_DELAY_SECONDS = 0.5
_MAX_LIMIT = 500
_MAX_REVIEW_CHARS = 2000
_COOKIE_HINT = (
    "Sign in at goodreads.com in a desktop browser, open the developer tools' Network tab, reload the page, "
    "select the first www.goodreads.com request, copy the value of its Cookie request header, "
    "and save it as the Goodreads tool's session cookie."
)
_ROW = re.compile(r'<tr id="review_(\d+)"[\s\S]*?</tr>')
_PARTIAL_DATE_FORMATS = (("%b %d, %Y", 10), ("%B %d, %Y", 10), ("%b %Y", 7), ("%B %Y", 7), ("%Y", 4))


class _GoodreadsError(Exception):
    """Reading Goodreads failed; the message tells the user what to do."""


def _cookie_header(value: str) -> str:
    """Return a pasted Cookie header value without a ``Cookie:`` prefix or line breaks."""
    text = re.sub(r"[\r\n]+", " ", value).strip()
    return re.sub(r"^cookie:\s*", "", text, flags=re.IGNORECASE)


def _user_id_from(text: str) -> str | None:
    match = re.search(r"goodreads\.com/(?:review/list|user/show)/(\d+)", text)
    return match.group(1) if match else None


def _cell(row: str, field: str) -> str:
    """Return the inner HTML of a row's ``<td class="field NAME">``."""
    match = re.search(rf'<td class="field {field}"[^>]*>([\s\S]*?)</td>', row)
    return match.group(1) if match else ""


def _plain(markup: str) -> str:
    """Return a cell's visible text, without its label, edit links, or tags."""
    text = re.sub(r"<label>[\s\S]*?</label>", "", markup)
    text = re.sub(r"<a[^>]*>\s*\[edit\]\s*</a>", "", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return "\n".join(" ".join(line.split()) for line in text.splitlines() if line.strip()).strip()


def _partial_date(text: str) -> str | None:
    """Return a date such as ``Dec 28, 2021``, ``Nov 2010``, or ``2011`` as ``YYYY-MM-DD``, ``YYYY-MM``, or ``YYYY``."""
    text = " ".join(text.split())
    for pattern, length in _PARTIAL_DATE_FORMATS:
        try:
            return datetime.strptime(text, pattern).date().isoformat()[:length]  # noqa: DTZ007
        except ValueError:
            continue
    return None


def _letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def _display_author(listed: str, href: str) -> str:
    """Return "Last, First" as "First Last", unless the author's URL lists the name family-name first."""
    parts = [part.strip() for part in listed.split(",")]
    if len(parts) != 2 or not all(parts):
        return listed
    slug = _letters(re.sub(r"^.*/author/show/\d+\.?", "", href))
    as_listed, flipped = f"{parts[0]} {parts[1]}", f"{parts[1]} {parts[0]}"
    return as_listed if slug and slug == _letters(as_listed) != _letters(flipped) else flipped


def _number(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def _review(row: str) -> str:
    """Return the user's review as plain text, preferring the full text Goodreads hides behind "more"."""
    cell = _cell(row, "review")
    full = re.search(r'<span id="freeText\d[^"]*"[^>]*>([\s\S]*?)</span>', cell)
    short = re.search(r'<span id="freeTextContainer[^"]*"[^>]*>([\s\S]*?)</span>', cell)
    match = full or short
    text = _plain(match.group(1)) if match else ""
    return text if len(text) <= _MAX_REVIEW_CHARS else text[:_MAX_REVIEW_CHARS] + "…"


def _latest(row: str, field: str) -> str | None:
    """Return the latest of a row's reading-session dates, such as every ``date_read_value``."""
    days = [_partial_date(html.unescape(value)) for value in re.findall(rf'class="{field}">([^<]*)<', row)]
    return max(filter(None, days), default=None)


def _book(review_id: str, row: str) -> dict[str, Any] | None:
    """Return one shelf-table row as a book, leaving out what Goodreads left empty."""
    title = re.search(r'<a title="([^"]*)" href="/book/show/(\d+)', _cell(row, "title"))
    if not title:
        return None
    author = re.search(r'<a href="([^"]*)">([^<]*)</a>', _cell(row, "author"))
    rating = re.search(r'data-rating="([\d.]+)"', _cell(row, "rating"))
    pages = re.search(r"\d[\d,]*", _plain(_cell(row, "num_pages")))
    added = re.search(r'<span title="([^"]*)"', _cell(row, "date_added"))
    stars = round(float(rating.group(1))) if rating else 0
    book: dict[str, Any] = {
        "book_id": title.group(2),
        "title": html.unescape(title.group(1)),
        "author": _display_author(html.unescape(author.group(2)).strip(), author.group(1)) if author else None,
        "isbn": _plain(_cell(row, "isbn")) or _plain(_cell(row, "isbn13")),
        "pages": int(pages.group(0).replace(",", "")) if pages else None,
        "published": _partial_date(_plain(_cell(row, "date_pub"))),
        "average_rating": _number(_plain(_cell(row, "avg_rating"))),
        # Goodreads shows 0 stars for a book the user has not rated.
        "my_rating": stars or None,
        "shelves": [html.unescape(name).strip() for name in re.findall(r'class="shelfLink"[^>]*>([^<]*)<', row)],
        "format": _plain(_cell(row, "format")),
        "date_started": _latest(row, "date_started_value"),
        "date_read": _latest(row, "date_read_value"),
        "date_added": _partial_date(added.group(1)) if added else None,
        "review": _review(row),
        "link": f"{_BASE_URL}/review/show/{review_id}",
    }
    return {key: value for key, value in book.items() if value not in (None, "", [])}


def _in_range(day: str, first: str | None, last: str | None) -> bool:
    """Return whether a full or partial date falls between ``first`` and ``last``, compared at its own precision."""
    return (first is None or day >= first[: len(day)]) and (last is None or day <= last[: len(day)])


def _dump(value: Any) -> str:  # noqa: ANN401
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class GoodreadsTools(Toolkit):
    """Read the books on the user's Goodreads shelves, with their ratings, reviews, and reading dates."""

    name = "goodreads"
    instructions = (
        "Goodreads is read-only here: the tool cannot add, shelve, rate, or review books, "
        "so tell the user to do that on goodreads.com or in the app. "
        "The built-in shelves are read, currently-reading, and to-read; the user may have custom shelves too. "
        "`my_rating` is the user's 1-5 stars and is omitted when unrated. "
        "Dates are YYYY-MM-DD, or YYYY-MM or YYYY when Goodreads has only the month or year. "
        "Fields Goodreads left empty are omitted. "
        "Each call reads Goodreads' pages one by one, so prefer one call over many."
    )

    def __init__(self, session_token: str | None = None, profile_url: str | None = None) -> None:
        """Store the Cookie header of a signed-in Goodreads browser session and, optionally, the profile link."""
        self._cookie = _cookie_header(session_token or "")
        self._user_id = _user_id_from(profile_url or "")
        super().__init__()

    def _client(self) -> httpx.AsyncClient:
        if not self._cookie:
            raise _GoodreadsError("Goodreads is not configured. " + _COOKIE_HINT)
        return httpx.AsyncClient(
            base_url=_BASE_URL,
            headers={
                "cookie": self._cookie,
                "user-agent": _USER_AGENT,
                "accept": "text/html,application/xhtml+xml",
                "accept-language": "en-US,en;q=0.9",
            },
            timeout=_TIMEOUT_SECONDS,
            follow_redirects=False,
        )

    async def _get(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: dict[str, str | int] | None = None,
    ) -> httpx.Response:
        """Return one page, raising ``_GoodreadsError`` when the session is signed out, challenged, or failing."""
        try:
            response = await client.get(path, params=params)
        except httpx.HTTPError as exc:
            msg = f"Could not reach Goodreads: {type(exc).__name__}"
            raise _GoodreadsError(msg) from None
        if response.is_redirect and re.search(r"sign_?in", response.headers.get("location", "")):
            msg = "Goodreads says the saved session is signed out or expired. " + _COOKIE_HINT
            raise _GoodreadsError(msg)
        if response.headers.get("x-amzn-waf-action") or response.status_code == httpx.codes.ACCEPTED:
            msg = (
                "Goodreads' firewall challenged the request as automated. "
                "Open goodreads.com in your browser and try again later; if it keeps happening, save a fresh cookie."
            )
            raise _GoodreadsError(msg)
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            msg = "Goodreads is rate-limiting requests; try again in a few minutes."
            raise _GoodreadsError(msg)
        if response.status_code == httpx.codes.NOT_FOUND:
            msg = "Goodreads has no such page; check the profile link saved in the tool's settings."
            raise _GoodreadsError(msg)
        if response.status_code != httpx.codes.OK and not response.is_redirect:
            msg = f"Goodreads returned HTTP {response.status_code}."
            raise _GoodreadsError(msg)
        return response

    async def _account(self, client: httpx.AsyncClient) -> str:
        """Return the signed-in user's id, from the saved profile link or from where "My Books" leads."""
        if self._user_id:
            return self._user_id
        response = await self._get(client, "/review/list")
        location = response.headers.get("location", "")
        found = _user_id_from(f"goodreads.com{location}") or _user_id_from(location) or _user_id_from(response.text)
        if not found:
            msg = (
                "Could not tell which Goodreads account the session belongs to. Save your profile link, "
                "such as https://www.goodreads.com/user/show/12345678-name, in the tool's settings."
            )
            raise _GoodreadsError(msg)
        self._user_id = found
        return found

    async def _shelf(self, shelf: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Return the books on ``shelf``, most recently added first, reading at most ``limit``."""
        books: list[dict[str, Any]] = []
        seen: set[str] = set()
        async with self._client() as client:
            user_id = await self._account(client)
            for page in range(1, _MAX_PAGES + 1):
                if page > 1:
                    await asyncio.sleep(_PAGE_DELAY_SECONDS)
                params: dict[str, str | int] = {
                    "shelf": shelf,
                    "per_page": _PER_PAGE,
                    "page": page,
                    "sort": "date_added",
                    "order": "d",
                }
                response = await self._get(client, f"/review/list/{user_id}", params)
                body = response.text
                if response.is_redirect or 'id="books"' not in body:
                    # A signed-out visitor can also get a sign-in page with status 200.
                    msg = (
                        "Goodreads did not show the shelf, so the saved session is probably signed out. " + _COOKIE_HINT
                    )
                    raise _GoodreadsError(msg)
                rows = [(match.group(1), match.group(0)) for match in _ROW.finditer(body)]
                new = [(review_id, row) for review_id, row in rows if review_id not in seen]
                seen.update(review_id for review_id, _ in new)
                books.extend(book for review_id, row in new if (book := _book(review_id, row)))
                if limit is not None and len(books) >= limit:
                    return books[:limit]
                if len(rows) < _PER_PAGE or not new:
                    break
        return books

    @tool
    async def get_shelf(self, shelf: str = "currently-reading", limit: int = 50) -> str:
        """Get the books on one of the user's Goodreads shelves, most recently added first.

        Args:
            shelf: read, currently-reading, to-read, or the name of a custom shelf.
            limit: Most books to return, up to 500.

        """
        if not shelf.strip():
            return "shelf must name a shelf."
        if not 1 <= limit <= _MAX_LIMIT:
            return f"limit must be between 1 and {_MAX_LIMIT}."
        try:
            books = await self._shelf(shelf.strip(), limit)
        except _GoodreadsError as exc:
            return str(exc)
        return _dump({"shelf": shelf.strip(), "books": books})

    @tool
    async def find_my_books(self, query: str) -> str:
        """Find books on any of the user's shelves whose title or author contains every word of `query`.

        Args:
            query: Words of the title or author, such as "dune" or "le guin".

        """
        words = query.lower().split()
        if not words:
            return "query must name a title or author."
        try:
            books = await self._shelf(_ALL_SHELVES)
        except _GoodreadsError as exc:
            return str(exc)
        matches = [
            book
            for book in books
            if all(word in f"{book.get('title', '')} {book.get('author', '')}".lower() for word in words)
        ]
        return _dump({"books": matches})

    @tool
    async def get_books_read(self, start_date: str | None = None, end_date: str | None = None) -> str:
        """Get the books the user finished between two dates, newest first, with how many books and pages that is.

        Args:
            start_date: First day to include, as YYYY-MM-DD; omit to start at the first book read.
            end_date: Last day to include, as YYYY-MM-DD; omit to read through today.

        """
        try:
            first = date.fromisoformat(start_date).isoformat() if start_date else None
            last = date.fromisoformat(end_date).isoformat() if end_date else None
        except ValueError:
            return "Dates must be YYYY-MM-DD."
        try:
            books = await self._shelf("read")
        except _GoodreadsError as exc:
            return str(exc)
        dated = [book for book in books if "date_read" in book and _in_range(book["date_read"], first, last)]
        dated.sort(key=lambda book: book["date_read"], reverse=True)
        result: dict[str, Any] = {
            "books": dated,
            "count": len(dated),
            "pages": sum(book.get("pages", 0) for book in dated),
        }
        undated = [book["title"] for book in books if "date_read" not in book]
        if undated:
            # Books shelved as read without a read date fit no date range.
            result["read_without_date"] = undated
        return _dump(result)
