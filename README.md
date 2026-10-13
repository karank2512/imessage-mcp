# imessage-mcp

MCP server for iMessage through [BlueBubbles](https://bluebubbles.app): search and read threads, send only to an allowlist, and a health check for the stalled-listener failure.

## Status

Pre-alpha. This release ships the scaffold, configuration, a BlueBubbles client, four read-only tools (`server_health`, `list_chats`, `read_thread`, `search_messages`) and one write tool, `send_message`, which only sends to handles on an allowlist you set in the server's environment. See [Safety](#safety) before enabling it.

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
| `IMESSAGE_MCP_ALLOWLIST` | no | empty | Comma-separated handles (phone numbers or email addresses) that `send_message` may target. Empty means every send is refused. See [Safety](#safety) for how handles are matched. |
| `IMESSAGE_MCP_READ_ONLY` | no | unset | Set to `1` to drop the send tool entirely, regardless of the allowlist: `tools/list` does not include `send_message`. |
| `IMESSAGE_MCP_LOG` | no | unset | Path of a file to append one JSON line per send attempt to, allowed or refused. If the file cannot be opened for append, sends are refused. |

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
| `search_messages` | `query` (2+ characters), `chat_guid` (optional), `since` (ISO 8601, optional), `limit` (default 25, max 100) | list of `{id, chat_guid, date, from_me, sender, text, attachments, reply_to}`, newest first | `POST /api/v1/message/query` with a `where` clause `message.text LIKE :text ESCAPE '\'` (plus `GET /api/v1/chat/{guid}` when a chat-filtered search is empty) |
| `send_message` | `handle`, `text` (1 to 2000 characters) | `{sent, chat_guid, message_id}` | `POST /api/v1/message/text` with `chatGuid: "any;-;<handle>"`, `method: "apple-script"` and a fresh `tempGuid`; absent when `IMESSAGE_MCP_READ_ONLY=1` |

The first four are read-only; `send_message` is the only tool that writes, and only to the allowlist. All of them report failures (server unreachable, wrong password, unknown `chat_guid`, unparseable `before`/`since`, empty or one-character `query`, a refused send) as MCP tool errors with a message, never as a crash. Dates are ISO 8601 with an offset in the local timezone of the machine running `imessage-mcp`.

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

### `search_messages`

```json
[
  {"id": "p:0/FIXTURE-MSG-0008", "chat_guid": "iMessage;+;chat000000000000000001", "date": "2023-11-14T22:20:20+00:00", "from_me": false, "sender": "+15550100002", "text": "Fixture message eight: newest in the group.", "attachments": 0, "reply_to": null},
  {"id": "p:0/FIXTURE-MSG-0007", "chat_guid": "iMessage;-;fixture-one@example.com", "date": "2023-11-14T22:19:20+00:00", "from_me": false, "sender": "fixture-one@example.com", "text": "Fixture message seven: newest in the direct chat.", "attachments": 0, "reply_to": null}
]
```

`query` is matched as a literal, case-insensitive substring of each message's text: `%`, `_` and `\` in the query are escaped before they reach BlueBubbles, so `50% off` finds only messages containing `50% off`, not every message with `50` somewhere before `off`. Leading and trailing whitespace is trimmed; what remains must be at least 2 characters. The newest `limit` hits are returned, newest first, each in the `read_thread` message shape plus the `chat_guid` it belongs to. `chat_guid` restricts the search to one chat (an unknown one is an error), and `since` keeps only messages created after that instant. There is no paging: narrow the query or raise `since` to see more.

The match runs on the `text` column in the Messages database. On recent macOS versions some messages store their text only in `attributedBody`; `read_thread` shows those, but `search_messages` cannot find them.

### `send_message`

```json
{"sent": true, "chat_guid": "iMessage;-;+16085550100", "message_id": "p:0/FIXTURE-MSG-0003"}
```

Sends `text` to the one-to-one chat with `handle` through BlueBubbles' AppleScript path. `handle` is a phone number or email address as `list_chats` shows it. The call is refused, with a tool error and nothing sent, when:

- `IMESSAGE_MCP_ALLOWLIST` is empty (the tool is still listed, so the model gets a clear error rather than an unknown tool);
- `handle`, after normalisation, is not on the allowlist;
- `text` is empty or whitespace, or longer than 2000 characters;
- `text` starts with `MEDIA:` (BlueBubbles has no attachment path here, and nothing that looks like one is sent);
- `IMESSAGE_MCP_LOG` is set but cannot be opened for append.

`chat_guid` in the result is the chat BlueBubbles resolved `any;-;<handle>` to, and `message_id` is the guid of the message it created. A BlueBubbles failure after the checks pass (unreachable, wrong password, a 4xx or 5xx) is also a tool error. With `IMESSAGE_MCP_READ_ONLY=1` the tool is not registered at all.

## Safety

The allowlist is the only gate. `send_message` sends to a handle if and only if that handle is on `IMESSAGE_MCP_ALLOWLIST` in the server's environment. There is no confirmation step, no rate limit and no content filter beyond the length and `MEDIA:` checks above. The server never reads the allowlist from the model: it is not a tool argument, it cannot be changed over MCP, and the model sees it only as a refusal message naming the handle it tried. Treat an entry on the allowlist as "the model may message this person at any time while the server runs".

How a handle is matched: both the `handle` argument and every allowlist entry are normalised the same way, then compared for equality. Phone numbers lose spaces, dashes and parentheses and keep a leading `+`; email addresses are lower-cased. No country code is ever inferred, so `(608) 555-0100` matches an allowlist entry written as `608-555-0100` or `6085550100` but **not** one written as `+16085550100`. Write allowlist entries exactly as `list_chats` reports the handle, which for phone numbers is E.164 with the leading `+`.

Every send attempt, allowed or refused, is appended to `IMESSAGE_MCP_LOG` when it is set, as one JSON line with the timestamp, the normalised handle, the text length and the outcome (`sent` with the `message_id`, `refused` or `failed` with the reason). The text itself is never logged. If the log file cannot be opened for append, the send is refused, so a configured log is never silently incomplete.

Recommendations:

- Run BlueBubbles under a dedicated Apple ID used for nothing else, so the worst case is bounded by that account's conversations, not your own.
- Leave `IMESSAGE_MCP_READ_ONLY=1` set unless you need sending; the examples above do.
- Keep the allowlist short and set `IMESSAGE_MCP_LOG` whenever sending is enabled.

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
make check                            # --help names the env vars; stdio tools/list is server_health, list_chats, read_thread, search_messages, send_message
```

`make check` runs the server as `python -m imessage_mcp.server`, so it works before the console script is installed. Tests run against an in-process fake of the BlueBubbles API in `tests/fake_bb.py`, plugged into `BBClient` through its `transport` argument; they never open a network connection or bind a port. The fake evaluates the search `where` clause with real `LIKE ... ESCAPE` semantics and rejects any other statement, so the search tests prove what the tool sends as well as what it matches; it also validates a send body before answering it, so the send tests prove the exact request. Fixture data in `tests/fixtures/` is invented; no real handle appears anywhere in the repo.

The server targets the `mcp` 2.x SDK (`MCPServer`) and falls back to `FastMCP` on 1.x.
