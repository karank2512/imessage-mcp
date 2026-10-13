"""send_message against the in-process fake: the allowlist gate, every
refusal path, handle normalisation, the attempt log, and read-only mode.

No network: the fake is the client's transport and validates the send body
before answering, so an allowed send proves the exact request that would
reach BlueBubbles. No real handle appears here; every number is a 555
fixture and every address is under example.com.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest

from imessage_mcp import config
from imessage_mcp.config import Settings
from imessage_mcp.send import (
    MAX_TEXT_LENGTH,
    SendError,
    allowed_handles,
    normalize_handle,
    send_message,
)
from imessage_mcp.server import build_server

from fake_bb import FakeBlueBubbles
from test_health import ROOT, drive_stdio

PASSWORD = "fixture-pw"
ALLOWED = "+16085550100"
EMAIL = "fixture-one@example.com"
TEXT = "Fixture outgoing text."
SEND_PATH = "/api/v1/message/text"


@pytest.fixture
def fake():
    return FakeBlueBubbles(password=PASSWORD)


@pytest.fixture
def client(fake):
    return fake.client()


@pytest.fixture
def log_file(tmp_path):
    return tmp_path / "send.log"


def settings(*allowlist: str, log: Path | None = None, read_only: bool = False) -> Settings:
    return Settings(
        url=FakeBlueBubbles().url,
        password=PASSWORD,
        allowlist=tuple(allowlist),
        read_only=read_only,
        log_path=str(log) if log else "",
    )


def log_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---- normalisation --------------------------------------------------------


def test_normalize_handle_rule():
    # Phone numbers: spaces, dashes and parentheses go; a leading + stays.
    assert normalize_handle("+1 (608) 555-0100") == "+16085550100"
    assert normalize_handle("  +16085550100 ") == "+16085550100"
    assert normalize_handle("(608) 555-0100") == "6085550100"
    assert normalize_handle("608-555-0100") == "6085550100"
    # No country code is ever inferred: a local-format number stays local.
    assert normalize_handle("(608) 555-0100") != normalize_handle("+16085550100")
    # Emails: trimmed and lower-cased, nothing stripped (the dash is part of the address).
    assert normalize_handle(" Fixture-One@Example.COM ") == "fixture-one@example.com"
    # Junk in, nothing out.
    assert normalize_handle("") == ""
    assert normalize_handle("  ( ) - ") == ""
    assert normalize_handle(None) == ""
    assert normalize_handle(16085550100) == ""


def test_allowed_handles_normalises_entries_and_drops_blanks():
    assert allowed_handles(("+1 608 555 0100", "Fixture-One@Example.com", " - ")) == {
        "+16085550100",
        "fixture-one@example.com",
    }


# ---- allowed send ---------------------------------------------------------


def test_allowed_send_posts_exact_body(client, fake):
    result = send_message(client, settings(ALLOWED), "+1 (608) 555-0100", TEXT)

    assert result == {"sent": True, "chat_guid": "iMessage;-;+16085550100", "message_id": "p:0/FIXTURE-MSG-0003"}

    (rec,) = fake.records
    assert rec.method == "POST"
    assert rec.path == SEND_PATH
    assert rec.password == PASSWORD
    assert rec.headers.get("Content-Type") == "application/json"
    assert set(rec.body) == {"chatGuid", "tempGuid", "message", "method"}
    assert rec.body["chatGuid"] == "any;-;+16085550100"
    assert rec.body["message"] == TEXT
    assert rec.body["method"] == "apple-script"
    assert uuid.UUID(rec.body["tempGuid"]).version == 4


def test_temp_guid_is_unique_per_send(client, fake):
    send_message(client, settings(ALLOWED), ALLOWED, TEXT)
    send_message(client, settings(ALLOWED), ALLOWED, TEXT)
    first, second = (r.body["tempGuid"] for r in fake.records)
    assert first != second


def test_allowed_send_to_email_is_case_insensitive(client, fake):
    result = send_message(client, settings(EMAIL), "Fixture-One@Example.com", TEXT)
    assert result["sent"] is True
    assert result["chat_guid"] == "iMessage;-;fixture-one@example.com"
    assert fake.records[0].body["chatGuid"] == "any;-;fixture-one@example.com"


def test_text_is_sent_verbatim_and_at_the_limit(client, fake):
    padded = "  " + "x" * (MAX_TEXT_LENGTH - 4) + "  "  # exactly 2000 characters
    assert len(padded) == MAX_TEXT_LENGTH
    send_message(client, settings(ALLOWED), ALLOWED, padded)
    assert fake.records[0].body["message"] == padded


def test_media_only_at_the_start_is_refused(client, fake):
    # MEDIA: later in the text is just text.
    result = send_message(client, settings(ALLOWED), ALLOWED, "the token is MEDIA: nothing")
    assert result["sent"] is True
    assert len(fake.records) == 1


# ---- refusal paths (one test each; nothing reaches the fake) -------------


def refused(client, cfg, handle, text) -> str:
    with pytest.raises(SendError) as excinfo:
        send_message(client, cfg, handle, text)
    assert excinfo.value.outcome == "refused"
    return str(excinfo.value)


def test_refused_when_allowlist_is_empty(client, fake):
    message = refused(client, settings(), ALLOWED, TEXT)
    assert config.ENV_ALLOWLIST in message
    assert "empty" in message
    assert fake.records == []


def test_refused_when_handle_is_not_on_the_allowlist(client, fake):
    message = refused(client, settings(ALLOWED, EMAIL), "+16085550199", TEXT)
    assert "+16085550199" in message
    assert config.ENV_ALLOWLIST in message
    # The error names the rejected handle, never the allowlist itself.
    assert ALLOWED not in message
    assert EMAIL not in message
    assert fake.records == []


def test_refused_when_handle_is_blank(client, fake):
    for blank in ("", "   ", "( ) -"):
        message = refused(client, settings(ALLOWED), blank, TEXT)
        assert "handle is required" in message
    assert fake.records == []


def test_refused_when_text_is_empty(client, fake):
    for empty in ("", "   ", "\n\t"):
        message = refused(client, settings(ALLOWED), ALLOWED, empty)
        assert "text is empty" in message
    assert fake.records == []


def test_refused_when_text_is_too_long(client, fake):
    too_long = "x" * (MAX_TEXT_LENGTH + 1)
    message = refused(client, settings(ALLOWED), ALLOWED, too_long)
    assert str(MAX_TEXT_LENGTH + 1) in message
    assert str(MAX_TEXT_LENGTH) in message
    assert fake.records == []


def test_refused_when_text_starts_with_media(client, fake):
    for text in ("MEDIA:/tmp/x.png", "media: anything", "  MEDIA:"):
        message = refused(client, settings(ALLOWED), ALLOWED, text)
        assert "MEDIA:" in message
        assert "attachments" in message
    assert fake.records == []


def test_refused_when_log_cannot_be_appended(client, fake, tmp_path):
    missing_dir = tmp_path / "no-such-dir" / "send.log"
    message = refused(client, settings(ALLOWED, log=missing_dir), ALLOWED, TEXT)
    assert config.ENV_LOG in message
    assert fake.records == []
    assert not missing_dir.exists()


def test_bluebubbles_error_is_failed_not_refused(fake, log_file):
    with pytest.raises(SendError) as excinfo:
        send_message(fake.client(password="wrong"), settings(ALLOWED, log=log_file), ALLOWED, TEXT)
    assert excinfo.value.outcome == "failed"
    assert "401" in str(excinfo.value)
    assert [r.path for r in fake.records] == [SEND_PATH]
    (line,) = log_lines(log_file)
    assert line["outcome"] == "failed"
    assert "401" in line["reason"]


# ---- the documented matching rule ------------------------------------------


def test_local_format_number_does_not_match_e164_entry(client, fake):
    # Documented rule: no country code is inferred, so (608) 555-0100 does
    # not match an allowlist entry written as +16085550100 ...
    refused(client, settings("+16085550100"), "(608) 555-0100", TEXT)
    assert fake.records == []

    # ... but it does match one written without the country code, in any
    # of the formats normalisation collapses.
    for entry in ("608-555-0100", "6085550100", "(608) 555 0100"):
        fake.clear()
        result = send_message(client, settings(entry), "(608) 555-0100", TEXT)
        assert result["sent"] is True
        assert fake.records[0].body["chatGuid"] == "any;-;6085550100"


def test_allowlist_entries_are_normalised_too(client, fake):
    result = send_message(client, settings("+1 (608) 555-0100"), ALLOWED, TEXT)
    assert result["sent"] is True
    assert fake.records[0].body["chatGuid"] == "any;-;" + ALLOWED


# ---- the attempt log --------------------------------------------------------


def test_log_records_every_attempt_without_the_text(client, fake, log_file):
    cfg = settings(ALLOWED, log=log_file)
    secret = "Fixture secret text that must never be logged."

    send_message(client, cfg, "+1 (608) 555-0100", secret)
    refused(client, cfg, "+16085550199", secret)
    refused(client, cfg, ALLOWED, "")
    refused(client, cfg, ALLOWED, "MEDIA:" + secret)

    lines = log_lines(log_file)
    assert [l["outcome"] for l in lines] == ["sent", "refused", "refused", "refused"]
    assert [l["handle"] for l in lines] == [ALLOWED, "+16085550199", ALLOWED, ALLOWED]
    assert [l["length"] for l in lines] == [len(secret), len(secret), 0, len("MEDIA:" + secret)]
    assert lines[0]["message_id"] == "p:0/FIXTURE-MSG-0003"
    assert "reason" not in lines[0]
    for line in lines[1:]:
        assert line["reason"]
        assert "message_id" not in line
    for line in lines:
        assert set(line) >= {"ts", "handle", "length", "outcome"}
        assert line["ts"].endswith("+00:00")
    raw = log_file.read_text(encoding="utf-8")
    assert secret not in raw
    assert "secret" not in raw
    assert raw.endswith("\n")


def test_log_appends_across_calls(client, log_file):
    log_file.write_text('{"outcome": "earlier"}\n', encoding="utf-8")
    send_message(client, settings(ALLOWED, log=log_file), ALLOWED, TEXT)
    lines = log_lines(log_file)
    assert len(lines) == 2
    assert lines[0]["outcome"] == "earlier"
    assert lines[1]["outcome"] == "sent"


def test_no_log_file_when_unset(client, tmp_path):
    send_message(client, settings(ALLOWED), ALLOWED, TEXT)
    refused(client, settings(), ALLOWED, TEXT)
    assert list(tmp_path.iterdir()) == []


def test_config_reads_log_path():
    cfg = config.load_settings({config.ENV_PASSWORD: "pw", config.ENV_LOG: " /tmp/fixture-send.log "})
    assert cfg.log_path == "/tmp/fixture-send.log"
    assert config.load_settings({config.ENV_PASSWORD: "pw"}).log_path == ""


# ---- through the MCP server -----------------------------------------------


def _call(server, name, args):
    return asyncio.run(server.call_tool(name, args))


def _tool_names(server) -> list[str]:
    return [t.name for t in asyncio.run(server.list_tools())]


def _tool_error_class():
    try:
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError:  # pragma: no cover - mcp 1.x
        from mcp.server.fastmcp.exceptions import ToolError
    return ToolError


def test_mcp_lists_send_message_with_schema(fake):
    server = build_server(settings(ALLOWED), client=fake.client())
    tools = {t["name"]: t for t in (t.model_dump(by_alias=True) for t in asyncio.run(server.list_tools()))}
    assert list(tools) == ["server_health", "list_chats", "read_thread", "search_messages", "send_message"]

    schema = tools["send_message"]["inputSchema"]
    assert set(schema["properties"]) == {"handle", "text"}
    assert sorted(schema["required"]) == ["handle", "text"]
    assert "MEDIA:" in tools["send_message"]["description"]


def test_mcp_lists_send_message_even_with_empty_allowlist(fake):
    # Registered but refusing: the model gets a clear error, not an unknown tool.
    server = build_server(settings(), client=fake.client())
    assert "send_message" in _tool_names(server)
    with pytest.raises(_tool_error_class()) as excinfo:
        _call(server, "send_message", {"handle": ALLOWED, "text": TEXT})
    assert "empty" in str(excinfo.value)
    assert fake.records == []


def test_mcp_read_only_hides_send_message(fake):
    server = build_server(settings(ALLOWED, read_only=True), client=fake.client())
    assert _tool_names(server) == ["server_health", "list_chats", "read_thread", "search_messages"]
    with pytest.raises(_tool_error_class()):
        _call(server, "send_message", {"handle": ALLOWED, "text": TEXT})
    assert fake.records == []


def test_mcp_send_returns_structured_result(fake, log_file):
    server = build_server(settings(ALLOWED, log=log_file), client=fake.client())
    result = _call(server, "send_message", {"handle": ALLOWED, "text": TEXT})
    assert result.is_error is False
    assert result.structured_content == {
        "sent": True,
        "chat_guid": "iMessage;-;+16085550100",
        "message_id": "p:0/FIXTURE-MSG-0003",
    }
    assert [l["outcome"] for l in log_lines(log_file)] == ["sent"]


def test_mcp_refusals_are_tool_errors_not_crashes(fake, log_file):
    server = build_server(settings(ALLOWED, log=log_file), client=fake.client())
    ToolError = _tool_error_class()

    with pytest.raises(ToolError) as excinfo:
        _call(server, "send_message", {"handle": "+16085550199", "text": TEXT})
    assert "not in" in str(excinfo.value)
    assert not isinstance(excinfo.value, SendError)

    with pytest.raises(ToolError) as excinfo:
        _call(server, "send_message", {"handle": ALLOWED, "text": "x" * (MAX_TEXT_LENGTH + 1)})
    assert "limit" in str(excinfo.value)

    with pytest.raises(ToolError):
        _call(server, "send_message", {"handle": ALLOWED})  # missing required argument

    assert fake.records == []
    assert [l["outcome"] for l in log_lines(log_file)] == ["refused", "refused"]


# ---- read-only over real stdio ---------------------------------------------


def test_stdio_tools_list_omits_send_message_in_read_only_mode():
    env = {k: v for k, v in os.environ.items() if not k.startswith("IMESSAGE_MCP_")}
    env.pop(config.ENV_URL, None)
    env[config.ENV_PASSWORD] = "x"
    env[config.ENV_READ_ONLY] = "1"
    env["PYTHONPATH"] = str(ROOT)
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    responses, stderr = drive_stdio(requests, env)

    assert 2 in responses, stderr
    names = [t["name"] for t in responses[2]["result"]["tools"]]
    assert names == ["server_health", "list_chats", "read_thread", "search_messages"]
    assert "send_message" not in names
