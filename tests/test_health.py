"""server_health against the fake BlueBubbles, plus config and CLI behaviour."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from imessage_mcp import config
from imessage_mcp.bb import BBClient, BBError
from imessage_mcp.config import ConfigError, Settings, load_settings
from imessage_mcp.server import build_parser, check_health, main

from fake_bb import FakeBlueBubbles, load_fixture

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "fixture-pw"


@pytest.fixture
def fake():
    with FakeBlueBubbles(password=PASSWORD) as server:
        yield server


@pytest.fixture
def clean_env(monkeypatch):
    for name in (
        config.ENV_URL,
        config.ENV_PASSWORD,
        config.ENV_ALLOWLIST,
        config.ENV_READ_ONLY,
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


# ---- server_health --------------------------------------------------------


def test_server_health_ok(fake):
    client = BBClient(fake.url, PASSWORD, timeout=5)
    result = check_health(client)

    expected = load_fixture("server_info.json")["data"]
    assert result["ok"] is True
    assert result["version"] == expected["server_version"]
    assert expected["os_version"] in result["detail"]
    assert "private_api=False" in result["detail"]

    assert [r.path for r in fake.records] == ["/api/v1/server/info"]
    assert fake.records[0].method == "GET"
    assert fake.records[0].password == PASSWORD


def test_server_health_wrong_password(fake):
    client = BBClient(fake.url, "not-the-password", timeout=5)
    result = check_health(client)

    assert result["ok"] is False
    assert result["version"] == ""
    assert "401" in result["detail"]
    assert "Invalid password" in result["detail"]
    assert fake.records[0].password == "not-the-password"


def test_server_health_unreachable():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # The socket is closed again, so nothing listens on ``port``.
    client = BBClient(f"http://127.0.0.1:{port}", PASSWORD, timeout=2)
    result = check_health(client)

    assert result["ok"] is False
    assert result["version"] == ""
    assert "unreachable" in result["detail"]


def test_server_health_non_json_body(fake):
    fake.override("GET", "/api/v1/server/info", 200, "<html>not json</html>")
    client = BBClient(fake.url, PASSWORD, timeout=5)
    result = check_health(client)

    assert result["ok"] is False
    assert "not JSON" in result["detail"]


# ---- BBClient -------------------------------------------------------------


def test_client_wraps_http_errors_in_bberror(fake):
    fake.override(
        "POST",
        "/api/v1/message/query",
        500,
        {"status": 500, "message": "Server Error", "error": {"type": "boom", "message": "fixture failure"}},
    )
    client = BBClient(fake.url, PASSWORD, timeout=5)
    with pytest.raises(BBError) as excinfo:
        client.query_messages({"limit": 1})
    assert excinfo.value.status == 500
    assert excinfo.value.message == "fixture failure"


def test_client_posts_json_bodies(fake):
    client = BBClient(fake.url, PASSWORD, timeout=5)

    messages = client.query_messages({"limit": 2, "sort": "DESC"})
    chats = client.chats(limit=7)
    sent = client.send_text("iMessage;-;fixture-one@example.com", "Fixture outgoing text.")

    assert [m["guid"] for m in messages] == [
        m["guid"] for m in load_fixture("message_query.json")["data"]
    ]
    assert len(chats) == 2
    assert sent["guid"] == "p:0/FIXTURE-MSG-0003"

    by_path = {r.path: r for r in fake.records}
    assert by_path["/api/v1/message/query"].body == {"limit": 2, "sort": "DESC"}
    assert by_path["/api/v1/chat/query"].body["limit"] == 7
    assert by_path["/api/v1/message/text"].body == {
        "chatGuid": "iMessage;-;fixture-one@example.com",
        "message": "Fixture outgoing text.",
        "method": "apple-script",
    }
    for rec in fake.records:
        assert rec.method == "POST"
        assert rec.headers.get("Content-Type") == "application/json"
        assert rec.password == PASSWORD


# ---- config ---------------------------------------------------------------


def test_config_missing_password_raises():
    with pytest.raises(ConfigError) as excinfo:
        load_settings({})
    assert config.ENV_PASSWORD in str(excinfo.value)


def test_config_defaults():
    settings = load_settings({config.ENV_PASSWORD: "pw"})
    assert settings == Settings(url=config.DEFAULT_URL, password="pw", allowlist=(), read_only=False)
    assert settings.sending_enabled is False


def test_config_allowlist_and_read_only():
    settings = load_settings(
        {
            config.ENV_PASSWORD: "pw",
            config.ENV_URL: "http://bb.example.test:4321/",
            config.ENV_ALLOWLIST: " +15550100001, fixture-one@example.com ,,+15550100001 ",
            config.ENV_READ_ONLY: "1",
        }
    )
    assert settings.url == "http://bb.example.test:4321"
    assert settings.allowlist == ("+15550100001", "fixture-one@example.com")
    assert settings.read_only is True
    assert settings.sending_enabled is False

    writable = load_settings({config.ENV_PASSWORD: "pw", config.ENV_ALLOWLIST: "+15550100001"})
    assert writable.sending_enabled is True


def test_config_rejects_bad_url():
    with pytest.raises(ConfigError):
        load_settings({config.ENV_PASSWORD: "pw", config.ENV_URL: "localhost:1234"})


# ---- CLI ------------------------------------------------------------------


def test_main_exits_2_without_password(clean_env, capsys):
    assert main([]) == 2
    err = capsys.readouterr().err
    assert err.count("\n") == 1
    assert config.ENV_PASSWORD in err


def test_help_names_env_variables(capsys):
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    for name, _ in config.ENV_HELP:
        assert name in out


def test_tools_list_over_stdio_is_exactly_server_health():
    env = {k: v for k, v in os.environ.items() if not k.startswith("IMESSAGE_MCP_")}
    env.pop(config.ENV_URL, None)
    env[config.ENV_PASSWORD] = "x"
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

    assert 1 in responses, stderr
    assert "result" in responses[1], responses[1]
    assert 2 in responses, stderr
    tools = responses[2]["result"]["tools"]
    assert [t["name"] for t in tools] == ["server_health"]


def drive_stdio(requests, env, timeout=30):
    """Send ``requests`` to the server over stdio and collect replies by id.

    Stdin stays open until every request with an id has been answered: the
    server exits on EOF and may drop in-flight replies if we close early.
    """
    proc = subprocess.Popen(
        [sys.executable, "-m", "imessage_mcp.server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=ROOT,
        env=env,
    )
    killer = threading.Timer(timeout, proc.kill)
    killer.start()
    wanted = {r["id"] for r in requests if "id" in r}
    responses = {}
    try:
        for r in requests:
            proc.stdin.write(json.dumps(r) + "\n")
        proc.stdin.flush()
        while wanted - responses.keys():
            line = proc.stdout.readline()
            if not line:
                break
            msg = json.loads(line)
            if "id" in msg:
                responses[msg["id"]] = msg
    finally:
        proc.stdin.close()
        stderr = proc.stderr.read()
        proc.wait()
        killer.cancel()
    return responses, stderr
