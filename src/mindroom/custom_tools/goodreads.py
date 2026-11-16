"""Books on the user's Goodreads shelves, read from the shelf RSS feed Goodreads publishes for each account.

Goodreads stopped issuing API keys in December 2020, and signing in goes through Amazon, so the tool never signs in.
It reads https://www.goodreads.com/review/list_rss/<user id>?key=<feed key>&shelf=<shelf>, the link at the bottom of
the user's "My Books" page, whose key lets it read the shelves of a private profile too.
The feed is read-only: the tool cannot shelve, rate, or review books.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from datetime import date
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from mindroom.tool_system.authoring import Toolkit, tool

_FEED_URL = "https://www.goodreads.com/review/list_rss/{user_id}"
_GOODREADS_HOSTS = frozenset({"goodreads.com", "www.goodreads.com", "m.goodreads.com"})
_ALL_SHELVES = "#ALL#"
_TIMEOUT_SECONDS = 30.0
_USER_AGENT = "Mozilla/5.0 (compatible; MindRoom Goodreads tool)"
# Goodreads returns 100 books per feed page; this reads up to 5,000 books of one shelf.
_MAX_PAGES = 50
_MAX_LIMIT = 500
_MAX_REVIEW_CHARS = 2000
_SETUP_HINT = (
    "Open your Goodreads 'My Books' page, copy the RSS link at the bottom of the page, "
    "and save it as the tool's RSS feed link."
)


class _GoodreadsError(Exception):
    """Reading the feed failed; the message tells the user what to do."""


@dataclass(frozen=True)
class _Feed:
    user_id: str
    key: str | None


def _parse_feed_link(link: str) -> _Feed:
    """Return the user id and feed key from a Goodreads RSS, shelf, or profile link."""
    text = link.strip()
    if not text:
        raise _GoodreadsError("Goodreads is not configured. " + _SETUP_HINT)
    parts = urlsplit(text if "://" in text else f"https://{text}")
    if (parts.hostname or "").lower() not in _GOODREADS_HOSTS:
        raise _GoodreadsError("The saved link is not a goodreads.com link. " + _SETUP_HINT)
    match = re.search(r"/(?:review/list_rss|review/list|user/show)/(\d+)", parts.path)
    if not match:
        raise _GoodreadsError("The saved link does not name a Goodreads account. " + _SETUP_HINT)
    keys = parse_qs(parts.query).get("key")
    return _Feed(user_id=match.group(1), key=keys[0] if keys else None)


def _text(item: Any, tag: str) -> str:  # noqa: ANN401
    element = item.find(tag)
    return (element.text or "").strip() if element is not None else ""


def _day(text: str) -> str | None:
    """Return an RSS date such as ``Mon, 06 Mar 2017 12:02:40 -0800`` as ``YYYY-MM-DD`` in its own time zone."""
    if not text:
        return None
    try:
        return parsedate_to_datetime(text).date().isoformat()
    except (TypeError, ValueError):
        return None


def _plain(markup: str) -> str:
    """Return review HTML as plain text."""
    text = re.sub(r"<br\s*/?>", "\n", markup, flags=re.IGNORECASE)
    text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
    return text if len(text) <= _MAX_REVIEW_CHARS else text[:_MAX_REVIEW_CHARS] + "…"


def _number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _book(item: Any) -> dict[str, Any]:  # noqa: ANN401
    """Return one feed item as a book, leaving out fields Goodreads left empty."""
    pages = item.find("book/num_pages")
    rating = _number(_text(item, "user_rating"))
    published = _text(item, "book_published")
    book: dict[str, Any] = {
        "book_id": _text(item, "book_id"),
        "title": _text(item, "title"),
        "author": _text(item, "author_name"),
        "isbn": _text(item, "isbn"),
        "pages": int(pages.text) if pages is not None and (pages.text or "").strip().isdigit() else None,
        "published": int(published) if published.isdigit() else None,
        "average_rating": _number(_text(item, "average_rating")),
        # Goodreads writes 0 for a book the user has not rated.
        "my_rating": int(rating) if rating else None,
        "shelves": [shelf.strip() for shelf in _text(item, "user_shelves").split(",") if shelf.strip()],
        "date_read": _day(_text(item, "user_read_at")),
        "date_added": _day(_text(item, "user_date_added")),
        "review": _plain(_text(item, "user_review")),
        "link": _text(item, "link").split("?", 1)[0],
    }
    return {key: value for key, value in book.items() if value not in (None, "", [])}


def _dump(value: Any) -> str:  # noqa: ANN401
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class GoodreadsTools(Toolkit):
    """Read the books on the user's Goodreads shelves, with their ratings, reviews, and dates read."""

    name = "goodreads"
    instructions = (
        "Goodreads is read-only here: the tool cannot add, shelve, rate, or review books, "
        "so tell the user to do that on goodreads.com or in the app. "
        "The built-in shelves are read, currently-reading, and to-read; the user may have custom shelves too. "
        "`my_rating` is the user's 1-5 stars and is omitted when unrated; dates are YYYY-MM-DD. "
        "A book's `shelves` lists its shelves other than read, and Goodreads may leave it empty. "
        "Fields Goodreads left empty are omitted."
    )

    def __init__(self, secret_rss_link: str | None = None) -> None:
        """Store the RSS feed link of the user's Goodreads shelves."""
        self._rss_link = secret_rss_link or ""
        super().__init__()

    async def _shelf(self, shelf: str, limit: int | None = None) -> list[dict[str, Any]]:
        """Return the books on ``shelf`` in the feed's order, newest added first, reading at most ``limit``."""
        feed = _parse_feed_link(self._rss_link)
        books: list[dict[str, Any]] = []
        seen: set[str] = set()
        params: dict[str, str | int] = {"shelf": shelf}
        if feed.key:
            params["key"] = feed.key
        async with httpx.AsyncClient(
            headers={"user-agent": _USER_AGENT, "accept": "application/rss+xml, application/xml"},
            timeout=_TIMEOUT_SECONDS,
        ) as client:
            for page in range(1, _MAX_PAGES + 1):
                items = await self._page(client, feed, {**params, "page": page})
                new = [item for item in items if _text(item, "guid") not in seen]
                # A page past the last one comes back empty or, on some shelves, repeats the first page.
                if not new:
                    break
                for item in new:
                    seen.add(_text(item, "guid"))
                    books.append(_book(item))
                if limit is not None and len(books) >= limit:
                    return books[:limit]
        return books

    async def _page(self, client: httpx.AsyncClient, feed: _Feed, params: dict[str, str | int]) -> list[Any]:
        try:
            response = await client.get(_FEED_URL.format(user_id=feed.user_id), params=params)
        except httpx.HTTPError as exc:
            msg = f"Could not reach Goodreads: {type(exc).__name__}"
            raise _GoodreadsError(msg) from None
        if response.status_code == httpx.codes.NOT_FOUND:
            msg = "Goodreads has no account with the saved link's user id. " + _SETUP_HINT
            raise _GoodreadsError(msg)
        if response.status_code == httpx.codes.TOO_MANY_REQUESTS:
            msg = "Goodreads is rate-limiting requests; try again in a minute."
            raise _GoodreadsError(msg)
        if response.status_code != httpx.codes.OK:
            msg = f"Goodreads returned HTTP {response.status_code}."
            raise _GoodreadsError(msg)
        try:
            root = ElementTree.fromstring(response.content)
        except (ElementTree.ParseError, DefusedXmlException):
            root = None
        channel = root.find("channel") if root is not None and root.tag == "rss" else None
        if channel is None:
            # A private profile without the feed key gets an HTML sign-in page instead of the feed.
            raise _GoodreadsError(
                "Goodreads did not return the shelf feed, which happens when the profile is private "
                "and the saved link has no key. " + _SETUP_HINT,
            )
        return channel.findall("item")

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
        dated = [
            book
            for book in books
            if "date_read" in book
            and (first is None or book["date_read"] >= first)
            and (last is None or book["date_read"] <= last)
        ]
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
