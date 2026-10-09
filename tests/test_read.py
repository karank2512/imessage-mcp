"""list_chats and read_thread against the in-process fake, plus normalisation.

No network: every request goes through ``FakeBlueBubbles`` as the client's
transport. Dates are compared as instants so the tests pass in any timezone.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from imessage_mcp.config import Settings
from imessage_mcp.normalize import (
    attributed_text,
    iso_from_ms,
    ms_from_iso,
    normalize_chat,
    normalize_message,
)
from imessage_mcp.server import (
    DEFAULT_CHAT_LIMIT,
    DEFAULT_THREAD_LIMIT,
    MAX_THREAD_LIMIT,
    ToolFailure,
    build_server,
    list_chats,
    read_thread,
)

from fake_bb import FakeBlueBubbles, load_fixture

PASSWORD = "fixture-pw"
DIRECT = "iMessage;-;fixture-one@example.com"
GROUP = "iMessage;+;chat000000000000000001"
EMPTY = "SMS;-;+15550100003"
UNKNOWN = "iMessage;-;nobody@example.com"


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


def fixture_messages(chat_guid: str) -> list[dict]:
    rows = [m for m in load_fixture("message_query.json")["data"] if chat_guid in {c["guid"] for c in m["chats"]}]
    return sorted(rows, key=lambda m: m["dateCreated"])


# ---- normalize ------------------------------------------------------------


def test_iso_from_ms_keeps_the_instant_and_carries_an_offset():
    iso = iso_from_ms(1700000000000)
    assert iso is not None
    assert instant(iso) == utc(1700000000000)
    assert datetime.fromisoformat(iso).utcoffset() is not None
    assert iso_from_ms(None) is None
    assert iso_from_ms("garbage") is None


def test_ms_from_iso_round_trips_and_accepts_z():
    assert ms_from_iso(iso_from_ms(1700000360000)) == 1700000360000
    assert ms_from_iso("2023-11-14T22:13:20Z") == 1700000000000
    assert ms_from_iso("2023-11-14T22:13:20.250+00:00") == 1700000000250
    assert ms_from_iso("2023-11-14T22:13:20+00:00") == ms_from_iso("2023-11-15T00:13:20+02:00")
    for bad in ("", "   ", "yesterday", "1700000000000"):
        with pytest.raises(ValueError):
            ms_from_iso(bad)


def test_attributed_text_shapes():
    assert attributed_text([{"string": "ab", "runs": []}, {"string": "cd"}]) == "abcd"
    assert attributed_text({"string": "ab"}) == "ab"
    assert attributed_text("plain") == "plain"
    assert attributed_text(None) == ""
    assert attributed_text([{"runs": []}]) == ""


def test_normalize_message_fields():
    msg = {
        "guid": "p:0/X",
        "text": "hi",
        "dateCreated": 1700000000000,
        "isFromMe": False,
        "handle": {"address": "fixture-one@example.com"},
        "attachments": [{"guid": "a"}, {"guid": "b"}],
        "threadOriginatorGuid": "p:0/W",
    }
    out = normalize_message(msg)
    assert set(out) == {"id", "date", "from_me", "sender", "text", "attachments", "reply_to"}
    assert out["id"] == "p:0/X"
    assert instant(out["date"]) == utc(1700000000000)
    assert out["from_me"] is False
    assert out["sender"] == "fixture-one@example.com"
    assert out["text"] == "hi"
    assert out["attachments"] == 2
    assert out["reply_to"] == "p:0/W"

    mine = normalize_message({"guid": "p:0/Y", "text": "x", "isFromMe": True, "handle": None})
    assert mine["from_me"] is True
    assert mine["sender"] == "me"
    assert mine["attachments"] == 0
    assert mine["reply_to"] is None
    assert mine["date"] is None


def test_normalize_message_text_fallbacks():
    body = [{"string": "from attributedBody", "runs": []}]
    assert normalize_message({"guid": "1", "text": None, "attributedBody": body})["text"] == "from attributedBody"
    assert normalize_message({"guid": "1", "text": "", "attributedBody": body})["text"] == "from attributedBody"
    assert normalize_message({"guid": "1", "text": "real", "attributedBody": body})["text"] == "real"
    only_attachment = normalize_message({"guid": "1", "text": None, "attributedBody": None, "attachments": [{}]})
    assert only_attachment["text"] == ""
    assert only_attachment["attachments"] == 1


def test_normalize_chat_unread_heuristic():
    base = {"guid": "g", "displayName": "", "participants": [{"address": "a"}, {"address": "a"}, {"address": "b"}]}
    assert normalize_chat({**base, "lastMessage": None})["unread"] is None
    assert normalize_chat({**base, "lastMessage": {"isFromMe": True, "dateCreated": 1}})["unread"] is False
    assert normalize_chat({**base, "lastMessage": {"isFromMe": False, "dateRead": None}})["unread"] is True
    assert normalize_chat({**base, "lastMessage": {"isFromMe": False, "dateRead": 5}})["unread"] is False
    assert normalize_chat({**base, "lastMessage": {"isFromMe": False}})["unread"] is None
    assert normalize_chat(base)["participants"] == ["a", "b"]
    assert normalize_chat(base)["last_message_at"] is None


# ---- list_chats -----------------------------------------------------------


def test_list_chats_newest_first(client, fake):
    chats = list_chats(client)

    assert [c["chat_guid"] for c in chats] == [GROUP, DIRECT, EMPTY]
    for c in chats:
        assert set(c) == {"chat_guid", "display_name", "participants", "last_message_at", "unread"}

    group, direct, empty = chats
    assert group["display_name"] == "Fixture Group"
    assert group["participants"] == ["+15550100001", "+15550100002"]
    assert instant(group["last_message_at"]) == utc(1700000420000)
    assert group["unread"] is False  # last message read

    assert direct["display_name"] == ""
    assert direct["participants"] == ["fixture-one@example.com"]
    assert instant(direct["last_message_at"]) == utc(1700000360000)
    assert direct["unread"] is True  # last message from them, never read

    assert empty["participants"] == ["+15550100003"]
    assert empty["last_message_at"] is None
    assert empty["unread"] is None

    (rec,) = fake.records
    assert rec.method == "POST"
    assert rec.path == "/api/v1/chat/query"
    assert rec.body["with"] == ["participants", "lastmessage"]
    assert rec.body["sort"] == "lastmessage"
    assert rec.body["limit"] == DEFAULT_CHAT_LIMIT


def test_list_chats_limit(client, fake):
    chats = list_chats(client, limit=2)
    assert [c["chat_guid"] for c in chats] == [GROUP, DIRECT]
    assert fake.records[0].body["limit"] == 2

    fake.clear()
    assert len(list_chats(client, limit=0)) == 3  # nonsense limit falls back to the default
    assert fake.records[0].body["limit"] == DEFAULT_CHAT_LIMIT


def test_list_chats_server_error_is_tool_failure(fake):
    with pytest.raises(ToolFailure) as excinfo:
        list_chats(fake.client(password="wrong"))
    assert "401" in str(excinfo.value)


# ---- read_thread ----------------------------------------------------------


def test_read_thread_oldest_to_newest(client, fake):
    result = read_thread(client, DIRECT)

    assert set(result) == {"chat_guid", "messages", "has_more"}
    assert result["chat_guid"] == DIRECT
    assert result["has_more"] is False

    expected = fixture_messages(DIRECT)
    assert [m["id"] for m in result["messages"]] == [m["guid"] for m in expected]
    for got, want in zip(result["messages"], expected):
        assert instant(got["date"]) == utc(want["dateCreated"])
        assert got["from_me"] is want["isFromMe"]
        assert got["sender"] == ("me" if want["isFromMe"] else want["handle"]["address"])
    dates = [instant(m["date"]) for m in result["messages"]]
    assert dates == sorted(dates)

    by_id = {m["id"]: m for m in result["messages"]}
    assert by_id["p:0/FIXTURE-MSG-0006"]["reply_to"] == "p:0/FIXTURE-MSG-0005"
    assert by_id["p:0/FIXTURE-MSG-0005"]["text"] == "Fixture message five, text only in attributedBody."
    assert sum(1 for m in result["messages"] if m["reply_to"]) == 1

    (rec,) = fake.records
    assert rec.method == "POST"
    assert rec.path == "/api/v1/message/query"
    assert rec.body["chatGuid"] == DIRECT
    assert rec.body["sort"] == "DESC"
    assert rec.body["limit"] == DEFAULT_THREAD_LIMIT + 1
    assert "before" not in rec.body
    assert {"attachment", "handle"} <= set(rec.body["with"])


def test_read_thread_null_text_with_attachment(client):
    result = read_thread(client, DIRECT)
    msg = next(m for m in result["messages"] if m["id"] == "p:0/FIXTURE-MSG-0004")
    assert msg["text"] == ""
    assert msg["attachments"] == 1
    assert msg["from_me"] is False
    assert msg["sender"] == "fixture-one@example.com"
    assert all(m["attachments"] == 0 for m in result["messages"] if m["id"] != "p:0/FIXTURE-MSG-0004")


def test_read_thread_pages_back_with_before(client, fake):
    all_ids = [m["guid"] for m in fixture_messages(DIRECT)]
    assert len(all_ids) == 6

    first = read_thread(client, DIRECT, limit=4)
    assert first["has_more"] is True
    assert [m["id"] for m in first["messages"]] == all_ids[2:]

    second = read_thread(client, DIRECT, limit=4, before=first["messages"][0]["date"])
    assert second["has_more"] is False
    assert [m["id"] for m in second["messages"]] == all_ids[:2]

    assert fake.records[1].body["before"] == 1700000180000
    assert fake.records[1].body["limit"] == 5

    # A page that is exactly full is not "more".
    exact = read_thread(client, DIRECT, limit=2, before=first["messages"][0]["date"])
    assert exact["has_more"] is False
    assert [m["id"] for m in exact["messages"]] == all_ids[:2]

    # Paging past the beginning yields an empty page, not an error.
    nothing = read_thread(client, DIRECT, limit=4, before=second["messages"][0]["date"])
    assert nothing == {"chat_guid": DIRECT, "messages": [], "has_more": False}


def test_read_thread_accepts_z_and_blank_before(client, fake):
    result = read_thread(client, DIRECT, before="2023-11-14T22:16:00Z")
    assert [m["id"] for m in result["messages"]] == ["p:0/FIXTURE-MSG-0001", "p:0/FIXTURE-MSG-0003"]
    assert fake.records[0].body["before"] == 1700000160000

    fake.clear()
    assert len(read_thread(client, DIRECT, before="  ")["messages"]) == 6
    assert "before" not in fake.records[0].body


def test_read_thread_empty_known_chat(client, fake):
    result = read_thread(client, EMPTY)
    assert result == {"chat_guid": EMPTY, "messages": [], "has_more": False}
    assert [(r.method, r.path) for r in fake.records] == [
        ("POST", "/api/v1/message/query"),
        ("GET", "/api/v1/chat/SMS%3B-%3B%2B15550100003"),
    ]


def test_read_thread_unknown_chat_is_tool_failure(client):
    with pytest.raises(ToolFailure) as excinfo:
        read_thread(client, UNKNOWN)
    assert "unknown chat_guid" in str(excinfo.value)
    assert UNKNOWN in str(excinfo.value)

    with pytest.raises(ToolFailure):
        read_thread(client, "   ")


def test_read_thread_bad_before_is_tool_failure(client, fake):
    with pytest.raises(ToolFailure) as excinfo:
        read_thread(client, DIRECT, before="last tuesday")
    assert "before" in str(excinfo.value)
    assert fake.records == []  # rejected before any request


def test_read_thread_limit_clamped(client, fake):
    read_thread(client, DIRECT, limit=10_000)
    assert fake.records[0].body["limit"] == MAX_THREAD_LIMIT + 1
    fake.clear()
    read_thread(client, DIRECT, limit=-3)
    assert fake.records[0].body["limit"] == DEFAULT_THREAD_LIMIT + 1


def test_read_thread_server_error_is_tool_failure(fake):
    fake.override(
        "POST",
        "/api/v1/message/query",
        500,
        {"status": 500, "message": "Server Error", "error": {"type": "boom", "message": "fixture failure"}},
    )
    with pytest.raises(ToolFailure) as excinfo:
        read_thread(fake.client(), DIRECT)
    assert "500" in str(excinfo.value)
    assert "fixture failure" in str(excinfo.value)


# ---- through the MCP server -----------------------------------------------


def _call(server, name, args):
    return asyncio.run(server.call_tool(name, args))


def _tool_error_class():
    try:
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError:  # pragma: no cover - mcp 1.x
        from mcp.server.fastmcp.exceptions import ToolError
    return ToolError


def test_mcp_tools_return_structured_results(fake):
    server = build_server(Settings(url=fake.url, password=PASSWORD), client=fake.client())

    chats = _call(server, "list_chats", {"limit": 2})
    assert chats.is_error is False
    assert [c["chat_guid"] for c in chats.structured_content["result"]] == [GROUP, DIRECT]

    thread = _call(server, "read_thread", {"chat_guid": DIRECT, "limit": 2})
    assert thread.is_error is False
    assert thread.structured_content["has_more"] is True
    assert [m["id"] for m in thread.structured_content["messages"]] == [
        "p:0/FIXTURE-MSG-0006",
        "p:0/FIXTURE-MSG-0007",
    ]


def test_mcp_unknown_chat_is_a_tool_error_not_a_crash(fake):
    server = build_server(Settings(url=fake.url, password=PASSWORD), client=fake.client())
    ToolError = _tool_error_class()

    with pytest.raises(ToolError) as excinfo:
        _call(server, "read_thread", {"chat_guid": UNKNOWN})
    assert "unknown chat_guid" in str(excinfo.value)
    assert not isinstance(excinfo.value, ToolFailure)

    with pytest.raises(ToolError) as excinfo:
        _call(server, "read_thread", {"chat_guid": DIRECT, "before": "nope"})
    assert "before" in str(excinfo.value)

    with pytest.raises(ToolError):
        _call(server, "read_thread", {})  # missing required argument
