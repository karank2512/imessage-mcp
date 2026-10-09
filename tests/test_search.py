"""search_messages against the in-process fake.

No network: every request goes through ``FakeBlueBubbles`` as the client's
transport. The fake evaluates the ``where`` clause with real LIKE semantics
(including ``ESCAPE``) and answers 400 to any statement it does not know, so
these tests prove both what the tool sends and what that query matches.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from imessage_mcp.bb import BBError, SEARCH_STATEMENT, like_pattern
from imessage_mcp.config import Settings
from imessage_mcp.server import (
    DEFAULT_SEARCH_LIMIT,
    MAX_SEARCH_LIMIT,
    ToolFailure,
    build_server,
    search_messages,
)

from fake_bb import TEXT_LIKE_STATEMENT, FakeBlueBubbles, load_fixture, sql_like

PASSWORD = "fixture-pw"
DIRECT = "iMessage;-;fixture-one@example.com"
GROUP = "iMessage;+;chat000000000000000001"
UNKNOWN = "iMessage;-;nobody@example.com"

HIT_KEYS = {"id", "chat_guid", "date", "from_me", "sender", "text", "attachments", "reply_to"}


@pytest.fixture
def fake():
    return FakeBlueBubbles(password=PASSWORD)


@pytest.fixture
def client(fake):
    return fake.client()


def instant(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(timezone.utc)


def utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def fixture_hits(needle: str, chat_guid: str | None = None, after_ms: int | None = None) -> list[dict]:
    """What the fixture should yield for a literal, case-insensitive ``needle``, newest first."""
    rows = []
    for m in load_fixture("message_query.json")["data"]:
        if not isinstance(m["text"], str) or needle.lower() not in m["text"].lower():
            continue
        if chat_guid and chat_guid not in {c["guid"] for c in m["chats"]}:
            continue
        if after_ms is not None and m["dateCreated"] <= after_ms:
            continue
        rows.append(m)
    return sorted(rows, key=lambda m: m["dateCreated"], reverse=True)


# ---- the fake's LIKE is honest ---------------------------------------------


def test_fake_sql_like_semantics():
    assert sql_like("%off%", "50% off today", "\\")
    assert sql_like("%OFF%", "50% off today", "\\")  # case-insensitive
    assert sql_like("a_c", "abc", "\\")
    assert not sql_like("a_c", "abbc", "\\")
    assert sql_like("%a\\_c%", "xa_cx", "\\")
    assert not sql_like("%a\\_c%", "xabcx", "\\")
    assert sql_like("%100\\%%", "a 100% b", "\\")
    assert not sql_like("%100\\%%", "a 100 b", "\\")
    assert sql_like("%back\\\\slash%", "a back\\slash b", "\\")
    assert not sql_like("abc", "xabcx", "\\")  # LIKE anchors both ends


def test_like_pattern_escapes_wildcards_and_the_escape_itself():
    assert like_pattern("plain") == "%plain%"
    assert like_pattern("50% off") == "%50\\% off%"
    assert like_pattern("snake_case") == "%snake\\_case%"
    assert like_pattern("a\\b") == "%a\\\\b%"
    assert like_pattern("%_\\") == "%\\%\\_\\\\%"
    assert SEARCH_STATEMENT == TEXT_LIKE_STATEMENT


# ---- search_messages --------------------------------------------------------


def test_search_match_newest_first_with_chat_guid(client, fake):
    hits = search_messages(client, "newest in the")

    expected = fixture_hits("newest in the")
    assert [m["guid"] for m in expected] == ["p:0/FIXTURE-MSG-0008", "p:0/FIXTURE-MSG-0007"]
    assert [h["id"] for h in hits] == [m["guid"] for m in expected]
    assert [h["chat_guid"] for h in hits] == [GROUP, DIRECT]
    for got, want in zip(hits, expected):
        assert set(got) == HIT_KEYS
        assert instant(got["date"]) == utc(want["dateCreated"])
        assert got["from_me"] is want["isFromMe"]
        assert got["sender"] == want["handle"]["address"]
        assert got["text"] == want["text"]
        assert got["attachments"] == 0
        assert got["reply_to"] is None

    (rec,) = fake.records
    assert rec.method == "POST"
    assert rec.path == "/api/v1/message/query"
    assert rec.body["where"] == [{"statement": SEARCH_STATEMENT, "args": {"text": "%newest in the%"}}]
    assert rec.body["sort"] == "DESC"
    assert rec.body["limit"] == DEFAULT_SEARCH_LIMIT
    assert rec.body["offset"] == 0
    assert {"chat", "attachment", "handle"} <= set(rec.body["with"])
    assert "chatGuid" not in rec.body
    assert "after" not in rec.body


def test_search_is_case_insensitive_and_only_sees_the_text_column(client):
    assert [h["id"] for h in search_messages(client, "NEWEST IN THE")] == [
        "p:0/FIXTURE-MSG-0008",
        "p:0/FIXTURE-MSG-0007",
    ]
    # Message five has its text only in attributedBody; the LIKE runs on
    # message.text, so it is not found. A documented limitation, not a bug.
    assert search_messages(client, "only in attributedBody") == []


def test_search_no_match_is_empty_not_an_error(client, fake):
    assert search_messages(client, "zzz nothing says this") == []
    assert len(fake.records) == 1  # no chat lookup when no chat filter was given


def test_search_chat_filter(client, fake):
    hits = search_messages(client, "newest in the", chat_guid=GROUP)
    assert [h["id"] for h in hits] == ["p:0/FIXTURE-MSG-0008"]
    assert hits[0]["chat_guid"] == GROUP
    assert fake.records[0].body["chatGuid"] == GROUP

    fake.clear()
    hits = search_messages(client, "Fixture", chat_guid=DIRECT)
    assert [h["id"] for h in hits] == [m["guid"] for m in fixture_hits("Fixture", chat_guid=DIRECT)]
    assert all(h["chat_guid"] == DIRECT for h in hits)

    fake.clear()
    assert search_messages(client, "newest in the group", chat_guid="  ") == [
        {**h} for h in search_messages(client, "newest in the group")
    ]
    assert "chatGuid" not in fake.records[0].body  # blank filter means no filter


def test_search_unknown_chat_is_tool_failure(client, fake):
    with pytest.raises(ToolFailure) as excinfo:
        search_messages(client, "Fixture", chat_guid=UNKNOWN)
    assert "unknown chat_guid" in str(excinfo.value)
    assert [(r.method, r.path) for r in fake.records] == [
        ("POST", "/api/v1/message/query"),
        ("GET", "/api/v1/chat/iMessage%3B-%3Bnobody%40example.com"),
    ]


def test_search_since_filter(client, fake):
    since_ms = 1700000160000
    hits = search_messages(client, "Fixture", since="2023-11-14T22:16:00Z")

    expected = fixture_hits("Fixture", after_ms=since_ms)
    assert len(expected) == 5
    assert [h["id"] for h in hits] == [m["guid"] for m in expected]
    assert all(instant(h["date"]) > utc(since_ms) for h in hits)
    assert fake.records[0].body["after"] == since_ms

    fake.clear()
    everything = search_messages(client, "Fixture", since="   ")
    assert len(everything) == len(fixture_hits("Fixture"))
    assert "after" not in fake.records[0].body


def test_search_bad_since_is_tool_failure(client, fake):
    with pytest.raises(ToolFailure) as excinfo:
        search_messages(client, "Fixture", since="last tuesday")
    assert "since" in str(excinfo.value)
    assert fake.records == []


def test_search_percent_in_query_is_literal(client, fake):
    hits = search_messages(client, "50% off")

    # Only the message that really contains "50% off"; the fake received the
    # escaped pattern and nothing else.
    assert [h["id"] for h in hits] == ["p:0/FIXTURE-MSG-0009"]
    assert hits[0]["text"] == "Fixture promo: 50% off today only."
    (rec,) = fake.records
    assert rec.body["where"] == [{"statement": SEARCH_STATEMENT, "args": {"text": "%50\\% off%"}}]

    # Proof that escaping is what keeps the match narrow: the same query with
    # a bare % reaches "50 items off" too, because % becomes a wildcard.
    fake.clear()
    widened = client.query_messages(
        {"where": [{"statement": SEARCH_STATEMENT, "args": {"text": "%50% off%"}}], "sort": "DESC"}
    )
    assert [m["guid"] for m in widened] == ["p:0/FIXTURE-MSG-0010", "p:0/FIXTURE-MSG-0009"]


def test_search_underscore_in_query_is_literal(client, fake):
    hits = search_messages(client, "snake_case")
    assert [h["id"] for h in hits] == ["p:0/FIXTURE-MSG-0011"]
    assert fake.records[0].body["where"][0]["args"]["text"] == "%snake\\_case%"

    fake.clear()
    widened = client.query_messages(
        {"where": [{"statement": SEARCH_STATEMENT, "args": {"text": "%snake_case%"}}], "sort": "DESC"}
    )
    assert [m["guid"] for m in widened] == ["p:0/FIXTURE-MSG-0012", "p:0/FIXTURE-MSG-0011"]


def test_fake_rejects_unknown_where_statements(client):
    with pytest.raises(BBError) as excinfo:
        client.query_messages({"where": [{"statement": "message.text LIKE :text", "args": {"text": "%x%"}}]})
    assert excinfo.value.status == 400


def test_search_empty_or_short_query_is_tool_failure(client, fake):
    for bad in ("", "   ", None):
        with pytest.raises(ToolFailure) as excinfo:
            search_messages(client, bad)
        assert "query is required" in str(excinfo.value)
    for short in ("a", " a ", "%"):
        with pytest.raises(ToolFailure) as excinfo:
            search_messages(client, short)
        assert "at least 2 characters" in str(excinfo.value)
    assert fake.records == []  # rejected before any request

    assert len(search_messages(client, "  ab  ")) == 0  # trimmed, long enough, no hit
    assert fake.records[0].body["where"][0]["args"]["text"] == "%ab%"


def test_search_limit_default_cap_and_truncation(client, fake):
    search_messages(client, "Fixture", limit=10_000)
    assert fake.records[0].body["limit"] == MAX_SEARCH_LIMIT

    fake.clear()
    search_messages(client, "Fixture", limit=0)
    assert fake.records[0].body["limit"] == DEFAULT_SEARCH_LIMIT

    fake.clear()
    two = search_messages(client, "Fixture", limit=2)
    assert fake.records[0].body["limit"] == 2
    assert [h["id"] for h in two] == [m["guid"] for m in fixture_hits("Fixture")[:2]]

    # Even if the server ignored the limit, the tool never returns more than it.
    fake.clear()
    fake.override("POST", "/api/v1/message/query", 200, load_fixture("message_query.json"))
    assert len(search_messages(client, "Fixture", limit=3)) == 3


def test_search_server_error_is_tool_failure(fake):
    with pytest.raises(ToolFailure) as excinfo:
        search_messages(fake.client(password="wrong"), "Fixture")
    assert "401" in str(excinfo.value)


# ---- through the MCP server -----------------------------------------------


def _call(server, name, args):
    return asyncio.run(server.call_tool(name, args))


def _tool_error_class():
    try:
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError:  # pragma: no cover - mcp 1.x
        from mcp.server.fastmcp.exceptions import ToolError
    return ToolError


def test_mcp_lists_four_tools(fake):
    server = build_server(Settings(url=fake.url, password=PASSWORD), client=fake.client())
    # The wire shape (camelCase), as tools/list would send it.
    tools = {t["name"]: t for t in (t.model_dump(by_alias=True) for t in asyncio.run(server.list_tools()))}
    assert list(tools) == ["server_health", "list_chats", "read_thread", "search_messages"]

    schema = tools["search_messages"]["inputSchema"]
    assert set(schema["properties"]) == {"query", "chat_guid", "since", "limit"}
    assert schema["required"] == ["query"]
    assert tools["search_messages"]["description"]


def test_mcp_search_returns_structured_results(fake):
    server = build_server(Settings(url=fake.url, password=PASSWORD), client=fake.client())

    result = _call(server, "search_messages", {"query": "newest in the", "chat_guid": GROUP})
    assert result.is_error is False
    hits = result.structured_content["result"]
    assert [h["id"] for h in hits] == ["p:0/FIXTURE-MSG-0008"]
    assert hits[0]["chat_guid"] == GROUP
    assert set(hits[0]) == HIT_KEYS


def test_mcp_search_errors_are_tool_errors_not_crashes(fake):
    server = build_server(Settings(url=fake.url, password=PASSWORD), client=fake.client())
    ToolError = _tool_error_class()

    with pytest.raises(ToolError) as excinfo:
        _call(server, "search_messages", {"query": ""})
    assert "query is required" in str(excinfo.value)
    assert not isinstance(excinfo.value, ToolFailure)

    with pytest.raises(ToolError) as excinfo:
        _call(server, "search_messages", {"query": "x"})
    assert "at least 2 characters" in str(excinfo.value)

    with pytest.raises(ToolError) as excinfo:
        _call(server, "search_messages", {"query": "Fixture", "since": "nope"})
    assert "since" in str(excinfo.value)

    with pytest.raises(ToolError):
        _call(server, "search_messages", {})  # missing required argument
