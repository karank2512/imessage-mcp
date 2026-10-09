"""An in-process fake of the BlueBubbles REST API for tests.

``FakeBlueBubbles`` is a ``BBClient`` transport: a callable
``(method, url, body, headers) -> (status, body)``. It never touches the
network or binds a port, so it runs anywhere pytest does. It serves canned JSON from
``tests/fixtures``, applies the chat and message query parameters the real
server honours (chat guid, before/after, sort, offset, limit, ``with``),
checks the ``password`` query parameter, and records every request so tests
can assert on paths and bodies. All data is invented.

Usage::

    fake = FakeBlueBubbles(password="fixture-pw")
    client = fake.client()                 # BBClient wired to the fake
    ...
    fake.records[0].path == "/api/v1/server/info"
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from imessage_mcp.bb import BBClient

FIXTURES = Path(__file__).parent / "fixtures"

PATH_SERVER_INFO = "/api/v1/server/info"
PATH_CHAT_QUERY = "/api/v1/chat/query"
PATH_CHAT_PREFIX = "/api/v1/chat/"
PATH_MESSAGE_QUERY = "/api/v1/message/query"
PATH_MESSAGE_TEXT = "/api/v1/message/text"

FAKE_URL = "http://fake-bluebubbles.invalid:1234"


@dataclass
class Recorded:
    method: str
    path: str
    query: dict[str, list[str]]
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def password(self) -> str | None:
        values = self.query.get("password")
        return values[0] if values else None


def load_fixture(name: str) -> dict[str, Any]:
    with (FIXTURES / name).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _envelope(status: int, message: str) -> dict[str, Any]:
    return {
        "status": status,
        "message": message,
        "error": {"type": message, "message": message},
    }


class FakeBlueBubbles:
    """BlueBubbles stand-in that answers ``BBClient`` requests in-process."""

    def __init__(self, password: str = "fixture-pw"):
        self.password = password
        self.url = FAKE_URL
        self.records: list[Recorded] = []
        self.overrides: dict[tuple[str, str], tuple[int, Any]] = {}

    # ---- test helpers ---------------------------------------------------

    def client(self, password: str | None = None, **kwargs: Any) -> BBClient:
        """A ``BBClient`` whose transport is this fake."""
        pw = self.password if password is None else password
        return BBClient(self.url, pw, transport=self, **kwargs)

    def override(self, method: str, path: str, status: int, payload: Any) -> None:
        """Make ``method path`` answer ``status`` with ``payload`` (JSON, or raw str/bytes)."""
        self.overrides[(method, path)] = (status, payload)

    def clear(self) -> None:
        self.records.clear()
        self.overrides.clear()

    # ---- transport --------------------------------------------------------

    def __call__(
        self, method: str, url: str, body: bytes | None, headers: dict[str, str]
    ) -> tuple[int, bytes]:
        parts = urlsplit(url)
        path = parts.path
        rec = Recorded(
            method=method,
            path=path,
            query=parse_qs(parts.query),
            body=_parse_body(body),
            headers=dict(headers),
        )
        self.records.append(rec)

        if rec.password != self.password:
            payload = _envelope(401, "Unauthorized")
            payload["error"] = {"type": "Authentication Error", "message": "Invalid password"}
            return _json(401, payload)

        override = self.overrides.get((method, path))
        if override is not None:
            status, payload = override
            if isinstance(payload, bytes):
                return status, payload
            if isinstance(payload, str):
                return status, payload.encode("utf-8")
            return _json(status, payload)

        return self._route(method, path, rec.body)

    def _route(self, method: str, path: str, body: Any) -> tuple[int, bytes]:
        if method == "GET" and path == PATH_SERVER_INFO:
            return _json(200, load_fixture("server_info.json"))
        if method == "POST" and path == PATH_CHAT_QUERY:
            return _json(200, self._chat_query(body if isinstance(body, dict) else {}))
        if method == "GET" and path.startswith(PATH_CHAT_PREFIX):
            return self._chat_find(unquote(path[len(PATH_CHAT_PREFIX):]))
        if method == "POST" and path == PATH_MESSAGE_QUERY:
            return _json(200, self._message_query(body if isinstance(body, dict) else {}))
        if method == "POST" and path == PATH_MESSAGE_TEXT:
            return _json(200, load_fixture("message_text.json"))
        payload = _envelope(404, "Not Found")
        payload["error"]["message"] = f"no route for {method} {path}"
        return _json(404, payload)

    # ---- route logic ------------------------------------------------------

    def _chat_query(self, body: dict[str, Any]) -> dict[str, Any]:
        fixture = load_fixture("chat_query.json")
        chats = list(fixture["data"])
        if str(body.get("sort", "")).lower() == "lastmessage":
            chats.sort(key=_chat_last_ms, reverse=True)
        with_ = {str(w).lower() for w in body.get("with") or []}
        if "participants" not in with_:
            chats = [{k: v for k, v in c.items() if k != "participants"} for c in chats]
        if "lastmessage" not in with_:
            chats = [{k: v for k, v in c.items() if k != "lastMessage"} for c in chats]
        offset = _int(body.get("offset"), 0)
        limit = _int(body.get("limit"), len(chats))
        fixture["data"] = chats[offset : offset + limit]
        return fixture

    def _chat_find(self, guid: str) -> tuple[int, bytes]:
        for chat in load_fixture("chat_query.json")["data"]:
            if chat["guid"] == guid:
                return _json(200, {"status": 200, "message": "Successfully fetched chat!", "data": chat})
        return _json(404, _envelope(404, "Chat does not exist!"))

    def _message_query(self, body: dict[str, Any]) -> dict[str, Any]:
        fixture = load_fixture("message_query.json")
        messages = list(fixture["data"])

        chat_guid = body.get("chatGuid")
        if chat_guid:
            messages = [m for m in messages if chat_guid in {c["guid"] for c in m.get("chats", [])}]
        before = body.get("before")
        if before is not None:
            messages = [m for m in messages if m["dateCreated"] < int(before)]
        after = body.get("after")
        if after is not None:
            messages = [m for m in messages if m["dateCreated"] > int(after)]

        descending = str(body.get("sort", "DESC")).upper() != "ASC"
        messages.sort(key=lambda m: m["dateCreated"], reverse=descending)

        offset = _int(body.get("offset"), 0)
        limit = _int(body.get("limit"), len(messages))
        messages = messages[offset : offset + limit]

        # Like the real server, embedded records only appear when asked for.
        with_ = {str(w).lower() for w in body.get("with") or []}
        drop = set()
        if "attachment" not in with_:
            drop.add("attachments")
        if "handle" not in with_:
            drop.add("handle")
        fixture["data"] = [{k: v for k, v in m.items() if k not in drop} for m in messages]
        return fixture


# ---- helpers ---------------------------------------------------------------


def _json(status: int, payload: Any) -> tuple[int, bytes]:
    return status, json.dumps(payload).encode("utf-8")


def _parse_body(raw: bytes | None) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return raw.decode("utf-8", "replace")


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _chat_last_ms(chat: dict[str, Any]) -> int:
    last = chat.get("lastMessage")
    return int(last["dateCreated"]) if isinstance(last, dict) else -1
