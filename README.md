# imessage-mcp

MCP server for iMessage through [BlueBubbles](https://bluebubbles.app): search and read threads, send only to an allowlist, and a health check for the stalled-listener failure.

## Status

Pre-alpha. This release ships the scaffold, configuration, a BlueBubbles client, and three read-only tools: `server_health`, `list_chats` and `read_thread`. Searching and sending land in later packets.

## Requirements

- A Mac running the BlueBubbles server with its REST API enabled and a server password set.
- Python 3.11 or newer on the machine that runs the MCP client (it can be the same Mac or another host that can reach the BlueBubbles URL).

## Install

With [uv](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/karank2512/imessage-mcp
```

With [pipx](https://pipx.pypa.io/):

```bash
pipx install git+https://github.com/karank2512/imessage-mcp
```

Either way you get an `imessage-mcp` command that speaks MCP over stdio. Run `imessage-mcp --help` to see the environment variables it reads.

## Configuration

Everything is configured through environment variables. There is no config file.

| Variable | Required | Default | Meaning |
| --- | --- | --- | --- |
| `BLUEBUBBLES_URL` | no | `http://localhost:1234` | Base URL of the BlueBubbles server. |
| `BLUEBUBBLES_PASSWORD` | yes | | The BlueBubbles server password. Missing means the server exits with status 2 and a one-line message. |
| `IMESSAGE_MCP_ALLOWLIST` | no | empty | Comma-separated handles (phone numbers or email addresses) that `send_message` may target. Empty disables sending. |
| `IMESSAGE_MCP_READ_ONLY` | no | unset | Set to `1` to drop the send tool entirely, regardless of the allowlist. |

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "imessage": {
      "command": "imessage-mcp",
      "env": {
        "BLUEBUBBLES_URL": "http://localhost:1234",
        "BLUEBUBBLES_PASSWORD": "your-bluebubbles-password",
        "IMESSAGE_MCP_ALLOWLIST": "",
        "IMESSAGE_MCP_READ_ONLY": "1"
      }
    }
  }
}
```

### Claude Code

```bash
claude mcp add imessage \
  -e BLUEBUBBLES_URL=http://localhost:1234 \
  -e BLUEBUBBLES_PASSWORD=your-bluebubbles-password \
  -e IMESSAGE_MCP_READ_ONLY=1 \
  -- imessage-mcp
```

Or in `.mcp.json`:

```json
{
  "mcpServers": {
    "imessage": {
      "command": "imessage-mcp",
      "env": {
        "BLUEBUBBLES_URL": "http://localhost:1234",
        "BLUEBUBBLES_PASSWORD": "your-bluebubbles-password",
        "IMESSAGE_MCP_READ_ONLY": "1"
      }
    }
  }
}
```

## Tools

| Tool | Arguments | Returns | BlueBubbles call |
| --- | --- | --- | --- |
| `server_health` | none | `{ok, version, detail}` | `GET /api/v1/server/info` |
| `list_chats` | `limit` (default 20, max 200) | list of `{chat_guid, display_name, participants, last_message_at, unread}`, newest first | `POST /api/v1/chat/query` with `participants` and `lastmessage` |
| `read_thread` | `chat_guid`, `limit` (default 50, max 500), `before` (ISO 8601, optional) | `{chat_guid, messages, has_more}`; messages oldest to newest | `POST /api/v1/message/query` (plus `GET /api/v1/chat/{guid}` when the page is empty, to tell an empty chat from an unknown one) |

All three are read-only. `list_chats` and `read_thread` report failures (server unreachable, wrong password, unknown `chat_guid`, unparseable `before`) as MCP tool errors with a message, never as a crash. Dates are ISO 8601 with an offset in the local timezone of the machine running `imessage-mcp`.

### `server_health`

Calls `GET /api/v1/server/info` with the configured password and returns:

```json
{"ok": true, "version": "1.9.9", "detail": "BlueBubbles 1.9.9 on macOS 14.5.0; private_api=False; helper_connected=False; detected_imessage=..."}
```

`ok` is false, with the reason in `detail`, when the server is unreachable, the password is wrong, the request times out (10 s), or the body is not the JSON envelope BlueBubbles normally returns. The tool never raises.

### `list_chats`

```json
[
  {"chat_guid": "iMessage;+;chat000000000000000001", "display_name": "Fixture Group", "participants": ["+15550100001", "+15550100002"], "last_message_at": "2023-11-14T22:20:20+00:00", "unread": false},
  {"chat_guid": "iMessage;-;fixture-one@example.com", "display_name": "", "participants": ["fixture-one@example.com"], "last_message_at": "2023-11-14T22:19:20+00:00", "unread": true}
]
```

`participants` are handles exactly as BlueBubbles reports them. `display_name` is empty for one-to-one chats. `last_message_at` is `null` for a chat with no messages. `unread` is a best effort from the chat's last message: `true` when it came from someone else and has no read date, `false` when it is yours or has been read, `null` when there is no last message to judge from.

### `read_thread`

```json
{
  "chat_guid": "iMessage;-;fixture-one@example.com",
  "messages": [
    {"id": "p:0/FIXTURE-MSG-0004", "date": "2023-11-14T22:16:20+00:00", "from_me": false, "sender": "fixture-one@example.com", "text": "", "attachments": 1, "reply_to": null},
    {"id": "p:0/FIXTURE-MSG-0006", "date": "2023-11-14T22:18:20+00:00", "from_me": true, "sender": "me", "text": "Fixture reply six, answering message five.", "attachments": 0, "reply_to": "p:0/FIXTURE-MSG-0005"}
  ],
  "has_more": true
}
```

The newest `limit` messages are returned, oldest first. To page back, call again with `before` set to the `date` of the oldest message you already have; `has_more` is `true` while older messages exist. `text` comes from the message's `text` field, falling back to the text inside `attributedBody`; a message with only attachments has `text: ""`. `attachments` is a count only, nothing is downloaded. `sender` is the handle, or `me` for your own messages. `reply_to` is the `id` of the message replied to, or `null`.

## The stalled-listener problem

BlueBubbles can stop emitting webhooks and socket events while its REST API keeps answering. From the outside the server looks healthy: `/api/v1/server/info` returns 200, `server_health` reports `ok: true`, and yet nothing that depends on new-message events fires. `server_health` alone does not detect that condition. A later packet (P5) adds `stall_check`, which compares the newest message BlueBubbles knows about against what the listener last delivered. Until then, treat `ok: true` as "the API answers", not "iMessage is flowing".

## What it does not do

- No attachments: it does not download, upload or send images, files or audio.
- No group creation: it can read existing chats but cannot create new group chats or change participants.
- No Private API: sending goes through BlueBubbles' AppleScript path, so no reactions, replies, typing indicators or read receipts.
- No contact lookup: handles are phone numbers and email addresses as BlueBubbles reports them; it never reads your Contacts.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'     # offline: add --no-build-isolation (needs setuptools>=64 and wheel in the venv)
make test                             # pytest against the fake BlueBubbles
make test TESTS=tests/test_read.py    # one file; PYTEST_ARGS=-x etc. also pass through
make lint                             # pyflakes if it is installed, otherwise skips
make check                            # --help names the env vars; stdio tools/list is server_health, list_chats, read_thread
```

`make check` runs the server as `python -m imessage_mcp.server`, so it works before the console script is installed. Tests run against an in-process fake of the BlueBubbles API in `tests/fake_bb.py`, plugged into `BBClient` through its `transport` argument; they never open a network connection or bind a port. Fixture data in `tests/fixtures/` is invented.

The server targets the `mcp` 2.x SDK (`MCPServer`) and falls back to `FastMCP` on 1.x.
