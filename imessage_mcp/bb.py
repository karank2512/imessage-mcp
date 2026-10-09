"""Thin client over the BlueBubbles REST API.

Only the stdlib is used. Every failure, from a refused connection to a 401 to
a body that is not JSON, surfaces as ``BBError``; callers never see urllib or
socket exceptions.

The HTTP exchange itself goes through a *transport*: a callable
``(method, url, body: bytes | None, headers) -> (status, body: bytes)``. The
default is urllib. Tests pass an in-process fake so no socket is ever opened.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

DEFAULT_TIMEOUT = 10.0

PATH_SERVER_INFO = "/api/v1/server/info"
PATH_CHAT_QUERY = "/api/v1/chat/query"
PATH_CHAT = "/api/v1/chat/"  # + url-quoted chat guid
PATH_MESSAGE_QUERY = "/api/v1/message/query"
PATH_MESSAGE_TEXT = "/api/v1/message/text"

# What a chat / message query asks BlueBubbles to embed in each record.
CHAT_WITH = ["participants", "lastmessage"]
MESSAGE_WITH = ["attachment", "handle"]
# A search spans chats, so each hit also needs its chat embedded.
SEARCH_WITH = ["chat", "attachment", "handle"]

# The ``where`` item a search sends. BlueBubbles hands ``statement`` to its
# query builder verbatim with ``args`` bound, so the LIKE pattern is a bound
# parameter, never spliced into SQL. ``ESCAPE '\'`` makes ``\%``, ``\_`` and
# ``\\`` literal inside the pattern; ``like_pattern`` produces exactly that.
LIKE_ESCAPE = "\\"
SEARCH_STATEMENT = "message.text LIKE :text ESCAPE '\\'"

Transport = Callable[[str, str, "bytes | None", dict[str, str]], "tuple[int, bytes]"]


class BBError(Exception):
    """A BlueBubbles call failed.

    ``status`` is the HTTP status (or the ``status`` field of the response
    envelope). It is 0 when no HTTP response was received at all: refused
    connection, DNS failure, timeout, malformed URL.
    """

    def __init__(self, status: int, message: str):
        self.status = int(status)
        self.message = message
        if self.status:
            text = f"BlueBubbles returned {self.status}: {message}"
        else:
            text = f"BlueBubbles unreachable: {message}"
        super().__init__(text)


class BBClient:
    def __init__(
        self,
        base_url: str,
        password: str,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Transport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.timeout = timeout
        self.transport: Transport = transport or self._urllib_transport

    # ---- public API -----------------------------------------------------

    def server_info(self) -> dict[str, Any]:
        """GET /api/v1/server/info; returns the ``data`` object."""
        data = self._request("GET", PATH_SERVER_INFO)
        return data if isinstance(data, dict) else {}

    def query_messages(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        """POST /api/v1/message/query with ``body`` as-is; returns the ``data`` list."""
        data = self._request("POST", PATH_MESSAGE_QUERY, body)
        return data if isinstance(data, list) else []

    def thread_messages(
        self,
        chat_guid: str,
        limit: int,
        before_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """Newest-first messages of one chat, optionally created before ``before_ms``."""
        body: dict[str, Any] = {
            "chatGuid": chat_guid,
            "limit": int(limit),
            "offset": 0,
            "with": list(MESSAGE_WITH),
            "sort": "DESC",
        }
        if before_ms is not None:
            body["before"] = int(before_ms)
        return self.query_messages(body)

    def search_messages(
        self,
        query: str,
        limit: int,
        chat_guid: str | None = None,
        since_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """Newest-first messages whose ``text`` contains ``query`` literally.

        ``query`` is escaped with ``like_pattern`` so ``%`` and ``_`` in it
        cannot widen the match. Optionally restricted to one chat and to
        messages created after ``since_ms``.
        """
        body: dict[str, Any] = {
            "limit": int(limit),
            "offset": 0,
            "with": list(SEARCH_WITH),
            "sort": "DESC",
            "where": [{"statement": SEARCH_STATEMENT, "args": {"text": like_pattern(query)}}],
        }
        if chat_guid:
            body["chatGuid"] = chat_guid
        if since_ms is not None:
            body["after"] = int(since_ms)
        return self.query_messages(body)

    def chats(self, limit: int = 25) -> list[dict[str, Any]]:
        """POST /api/v1/chat/query for the most recently active chats."""
        body = {
            "limit": int(limit),
            "offset": 0,
            "with": list(CHAT_WITH),
            "sort": "lastmessage",
        }
        data = self._request("POST", PATH_CHAT_QUERY, body)
        return data if isinstance(data, list) else []

    def chat(self, chat_guid: str) -> dict[str, Any]:
        """GET /api/v1/chat/{guid}; raises ``BBError(404)`` for an unknown chat."""
        path = PATH_CHAT + urllib.parse.quote(chat_guid, safe="")
        data = self._request("GET", path)
        return data if isinstance(data, dict) else {}

    def send_text(self, chat_guid: str, text: str) -> dict[str, Any]:
        """POST /api/v1/message/text via AppleScript (no Private API)."""
        body = {"chatGuid": chat_guid, "message": text, "method": "apple-script"}
        data = self._request("POST", PATH_MESSAGE_TEXT, body)
        return data if isinstance(data, dict) else {}

    # ---- transport ------------------------------------------------------

    def _url(self, path: str) -> str:
        query = urllib.parse.urlencode({"password": self.password})
        return f"{self.base_url}{path}?{query}"

    def _urllib_transport(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        """Default transport. An HTTP error status is returned, not raised;
        anything that prevents a response at all propagates as an exception."""
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return int(resp.status), resp.read()
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read()
            except Exception:  # pragma: no cover - defensive
                raw = b""
            return int(exc.code), raw

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        headers = {"Accept": "application/json"}
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        try:
            status, raw = self.transport(method, self._url(path), data, headers)
        except urllib.error.URLError as exc:
            raise BBError(0, _reason_text(exc.reason)) from None
        except TimeoutError:
            raise BBError(0, f"timed out after {self.timeout:g}s") from None
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise BBError(0, _reason_text(exc)) from None

        status = int(status)
        if status >= 400:
            fallback = http.client.responses.get(status, "error")
            raise BBError(status, _error_message(_loads_or_none(raw), fallback))

        payload = _decode(raw, status)
        if isinstance(payload, dict):
            env_status = payload.get("status")
            if isinstance(env_status, int) and env_status >= 400:
                raise BBError(env_status, _error_message(payload, "error"))
            return payload.get("data")
        return payload


# ---- helpers -------------------------------------------------------------


def like_pattern(query: str) -> str:
    """``%query%`` for ``SEARCH_STATEMENT``: a substring match of ``query`` itself.

    The escape character, ``%`` and ``_`` inside ``query`` are prefixed with
    ``LIKE_ESCAPE`` so they match literally instead of acting as wildcards.
    """
    escaped = (
        query.replace(LIKE_ESCAPE, LIKE_ESCAPE + LIKE_ESCAPE)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


def _loads_or_none(raw: bytes | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None


def _error_message(payload: Any, fallback: str) -> str:
    """Pull the most specific message out of a BlueBubbles error envelope."""
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if payload.get("message"):
            return str(payload["message"])
    return fallback or "error"


def _reason_text(reason: Any) -> str:
    if isinstance(reason, TimeoutError):
        return "timed out"
    text = str(reason).strip()
    return text or reason.__class__.__name__


def _decode(raw: bytes, status: int) -> Any:
    if not raw:
        raise BBError(status, "empty response body")
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise BBError(status, "response body is not JSON") from None
