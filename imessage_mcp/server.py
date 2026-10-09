"""The MCP server (stdio) and the ``imessage-mcp`` console entry point.

Tools: ``server_health``, ``list_chats``, ``read_thread``. Everything that
talks to BlueBubbles goes through ``BBClient`` so the tools never see urllib
errors. Each tool is a plain function taking the client (easy to test) plus a
thin MCP wrapper that turns ``ToolFailure`` into the SDK's tool error.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from . import __version__
from .bb import BBClient, BBError
from .config import ConfigError, ENV_HELP, Settings, load_settings
from .normalize import ms_from_iso, normalize_chats, normalize_messages

SERVER_NAME = "imessage-mcp"

DEFAULT_CHAT_LIMIT = 20
MAX_CHAT_LIMIT = 200
DEFAULT_THREAD_LIMIT = 50
MAX_THREAD_LIMIT = 500

SERVER_HEALTH_DOC = (
    "Check that the BlueBubbles server answers /api/v1/server/info.\n\n"
    "Returns {ok, version, detail}. ok is true when the server replied with a "
    "valid JSON envelope using the configured password. This does NOT detect a "
    "stalled webhook listener: BlueBubbles can stop emitting events while its "
    "REST API still answers. A later release adds stall_check for that."
)

LIST_CHATS_DOC = (
    "List the most recently active chats, newest first.\n\n"
    "Returns a list of {chat_guid, display_name, participants, last_message_at, "
    "unread}. participants are handles (phone numbers or email addresses) as "
    "BlueBubbles reports them; display_name is empty for one-to-one chats. "
    "last_message_at is ISO 8601 with offset, or null for a chat with no "
    "messages. unread is true/false when it can be told from the last message "
    "and null otherwise. Pass chat_guid to read_thread."
)

READ_THREAD_DOC = (
    "Read messages from one chat, oldest to newest.\n\n"
    "Returns {chat_guid, messages, has_more}. Each message is {id, date, "
    "from_me, sender, text, attachments, reply_to}: date is ISO 8601 with "
    "offset, sender is the handle (or 'me'), text is '' for attachment-only "
    "messages, attachments is a count (nothing is downloaded), reply_to is the "
    "id of the message replied to or null. The newest `limit` messages are "
    "returned; to page back, call again with before = the date of the oldest "
    "message you have. has_more is true when older messages exist. Fails "
    "with an error for an unknown chat_guid or an unparseable before."
)


class ToolFailure(Exception):
    """A tool cannot produce a result; the message is meant for the caller."""


def _clamp(value: Any, default: int, maximum: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < 1:
        return default
    return min(n, maximum)


def health_from_info(info: dict[str, Any]) -> dict[str, Any]:
    """Shape a successful /server/info payload into the tool result."""
    version = str(info.get("server_version") or "")
    os_version = str(info.get("os_version") or "unknown")
    bits = [f"BlueBubbles {version or 'unknown version'} on macOS {os_version}"]
    for key in ("private_api", "helper_connected", "detected_imessage"):
        if key in info:
            bits.append(f"{key}={info[key]}")
    if not version:
        bits.append("server_version missing from response")
    return {"ok": True, "version": version, "detail": "; ".join(bits)}


def health_from_error(err: BBError) -> dict[str, Any]:
    return {"ok": False, "version": "", "detail": str(err)}


def check_health(client: BBClient) -> dict[str, Any]:
    try:
        info = client.server_info()
    except BBError as err:
        return health_from_error(err)
    return health_from_info(info)


def list_chats(client: BBClient, limit: int = DEFAULT_CHAT_LIMIT) -> list[dict[str, Any]]:
    """Most recently active chats, newest first."""
    n = _clamp(limit, DEFAULT_CHAT_LIMIT, MAX_CHAT_LIMIT)
    try:
        raw = client.chats(limit=n)
    except BBError as err:
        raise ToolFailure(str(err)) from None
    return normalize_chats(raw)[:n]


def read_thread(
    client: BBClient,
    chat_guid: str,
    limit: int = DEFAULT_THREAD_LIMIT,
    before: str | None = None,
) -> dict[str, Any]:
    """The newest ``limit`` messages of ``chat_guid`` (optionally before
    ``before``), returned oldest to newest with a ``has_more`` flag."""
    guid = (chat_guid or "").strip()
    if not guid:
        raise ToolFailure("chat_guid is required; get one from list_chats")
    n = _clamp(limit, DEFAULT_THREAD_LIMIT, MAX_THREAD_LIMIT)

    before_ms: int | None = None
    if before is not None and str(before).strip():
        try:
            before_ms = ms_from_iso(str(before))
        except ValueError:
            raise ToolFailure(
                f"before must be an ISO 8601 timestamp such as 2024-01-31T09:30:00+00:00, got {before!r}"
            ) from None

    try:
        # One extra row tells us whether an older page exists.
        raw = client.thread_messages(guid, limit=n + 1, before_ms=before_ms)
        if not raw:
            _require_chat(client, guid)
    except BBError as err:
        raise ToolFailure(str(err)) from None

    has_more = len(raw) > n
    page = raw[:n]  # newest first from BlueBubbles; drop the probe row (the oldest)
    return {"chat_guid": guid, "messages": normalize_messages(page), "has_more": has_more}


def _require_chat(client: BBClient, chat_guid: str) -> None:
    """Raise ``ToolFailure`` when BlueBubbles does not know ``chat_guid``."""
    try:
        client.chat(chat_guid)
    except BBError as err:
        if err.status == 404:
            raise ToolFailure(f"unknown chat_guid {chat_guid!r}; get one from list_chats") from None
        raise


def _server_class():
    """Return the decorator-style server class for the installed ``mcp``.

    mcp 2.x renamed FastMCP to MCPServer; the registration and ``run`` APIs we
    use are the same in both. Imported lazily so ``--help`` and config errors
    never depend on the SDK import.
    """
    try:
        from mcp.server.mcpserver import MCPServer  # mcp >= 2
    except ImportError:  # pragma: no cover - exercised only on mcp 1.x
        from mcp.server.fastmcp import FastMCP as MCPServer
    return MCPServer


def _tool_error_class():
    """The SDK exception that becomes an ``isError`` tool result (not a crash)."""
    try:
        from mcp.server.mcpserver.exceptions import ToolError  # mcp >= 2
    except ImportError:  # pragma: no cover - exercised only on mcp 1.x
        from mcp.server.fastmcp.exceptions import ToolError
    return ToolError


def build_server(settings: Settings, client: BBClient | None = None):
    """Create the MCP server with its tools registered."""
    bb = client or BBClient(settings.url, settings.password)
    server = _server_class()(SERVER_NAME)
    tool_error = _tool_error_class()

    @server.tool(name="server_health", description=SERVER_HEALTH_DOC)
    def server_health() -> dict[str, Any]:
        return check_health(bb)

    @server.tool(name="list_chats", description=LIST_CHATS_DOC)
    def list_chats_tool(limit: int = DEFAULT_CHAT_LIMIT) -> list[dict[str, Any]]:
        try:
            return list_chats(bb, limit=limit)
        except ToolFailure as err:
            raise tool_error(str(err)) from None

    @server.tool(name="read_thread", description=READ_THREAD_DOC)
    def read_thread_tool(
        chat_guid: str, limit: int = DEFAULT_THREAD_LIMIT, before: str | None = None
    ) -> dict[str, Any]:
        try:
            return read_thread(bb, chat_guid, limit=limit, before=before)
        except ToolFailure as err:
            raise tool_error(str(err)) from None

    return server


def build_parser() -> argparse.ArgumentParser:
    width = max(len(name) for name, _ in ENV_HELP) + 2
    env_lines = "\n".join(f"  {name.ljust(width)}{desc}" for name, desc in ENV_HELP)
    parser = argparse.ArgumentParser(
        prog="imessage-mcp",
        description=(
            "MCP server for iMessage through BlueBubbles. Speaks MCP over "
            "stdio; configured entirely through environment variables."
        ),
        epilog=f"environment variables:\n{env_lines}\n\nexit status 2 means the environment is incomplete.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"imessage-mcp {__version__}"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as err:
        print(f"imessage-mcp: {err}", file=sys.stderr)
        return 2
    server = build_server(settings)
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
