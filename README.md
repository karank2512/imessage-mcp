# imessage-mcp

MCP server for iMessage through [BlueBubbles](https://bluebubbles.app): search and read threads, send only to an allowlist, and a health check for the stalled-listener failure.

## Status

Pre-alpha. This release ships the scaffold, configuration, a BlueBubbles client, and one tool: `server_health`. Reading, searching and sending land in later packets.

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

### `server_health`

Calls `GET /api/v1/server/info` with the configured password and returns:

```json
{"ok": true, "version": "1.9.9", "detail": "BlueBubbles 1.9.9 on macOS 14.5.0; private_api=False; helper_connected=False; detected_imessage=..."}
```

`ok` is false, with the reason in `detail`, when the server is unreachable, the password is wrong, the request times out (10 s), or the body is not the JSON envelope BlueBubbles normally returns. The tool never raises.

## The stalled-listener problem

BlueBubbles can stop emitting webhooks and socket events while its REST API keeps answering. From the outside the server looks healthy: `/api/v1/server/info` returns 200, `server_health` reports `ok: true`, and yet nothing that depends on new-message events fires. `server_health` alone does not detect that condition. A later packet (P5) adds `stall_check`, which compares the newest message BlueBubbles knows about against what the listener last delivered. Until then, treat `ok: true` as "the API answers", not "iMessage is flowing".

## What it does not do

- No attachments: it does not download, upload or send images, files or audio.
- No group creation: it can read existing chats but cannot create new group chats or change participants.
- No Private API: sending goes through BlueBubbles' AppleScript path, so no reactions, replies, typing indicators or read receipts.
- No contact lookup: handles are phone numbers and email addresses as BlueBubbles reports them; it never reads your Contacts.

## Development

```bash
ln -sfn ~/venvs/imessage-mcp .venv   # or: python -m venv .venv && .venv/bin/pip install -e '.[dev]'
make test
make lint                             # runs pyflakes if it is installed, otherwise skips
```

Tests run against a stdlib fake of the BlueBubbles API in `tests/fake_bb.py`; they never contact a real server. Fixture data in `tests/fixtures/` is invented.
