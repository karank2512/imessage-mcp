"""The MCP server (stdio) and the ``imessage-mcp`` console entry point.

P1 exposes a single tool, ``server_health``. Everything that talks to
BlueBubbles goes through ``BBClient`` so the tool never sees urllib errors.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from . import __version__
from .bb import BBClient, BBError
from .config import ConfigError, ENV_HELP, Settings, load_settings

SERVER_NAME = "imessage-mcp"

SERVER_HEALTH_DOC = (
    "Check that the BlueBubbles server answers /api/v1/server/info.\n\n"
    "Returns {ok, version, detail}. ok is true when the server replied with a "
    "valid JSON envelope using the configured password. This does NOT detect a "
    "stalled webhook listener: BlueBubbles can stop emitting events while its "
    "REST API still answers. A later release adds stall_check for that."
)


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


def build_server(settings: Settings, client: BBClient | None = None):
    """Create the FastMCP server with its tools registered.

    The ``mcp`` import is local so ``--help`` and config errors work even when
    the import is slow or unavailable in a stripped-down environment.
    """
    from mcp.server.fastmcp import FastMCP

    bb = client or BBClient(settings.url, settings.password)
    server = FastMCP(SERVER_NAME)

    @server.tool(name="server_health", description=SERVER_HEALTH_DOC)
    def server_health() -> dict[str, Any]:
        return check_health(bb)

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
