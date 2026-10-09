"""Settings for imessage-mcp, read from environment variables only.

Nothing here touches the network or the filesystem. ``load_settings`` raises
``ConfigError`` on a bad environment; the CLI turns that into exit status 2.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

DEFAULT_URL = "http://localhost:1234"

ENV_URL = "BLUEBUBBLES_URL"
ENV_PASSWORD = "BLUEBUBBLES_PASSWORD"
ENV_ALLOWLIST = "IMESSAGE_MCP_ALLOWLIST"
ENV_READ_ONLY = "IMESSAGE_MCP_READ_ONLY"

# (name, description) in the order they appear in --help and the README.
ENV_HELP: tuple[tuple[str, str], ...] = (
    (ENV_URL, f"BlueBubbles server URL (default {DEFAULT_URL})"),
    (ENV_PASSWORD, "BlueBubbles server password (required)"),
    (
        ENV_ALLOWLIST,
        "comma-separated handles that send_message may target; empty disables sending",
    ),
    (ENV_READ_ONLY, "set to 1 to drop the send tool entirely"),
)

_TRUTHY = frozenset({"1", "true", "yes", "on"})


class ConfigError(Exception):
    """The environment does not describe a usable configuration."""


@dataclass(frozen=True)
class Settings:
    url: str = DEFAULT_URL
    password: str = ""
    allowlist: tuple[str, ...] = ()
    read_only: bool = False

    @property
    def sending_enabled(self) -> bool:
        """True when a send tool should be exposed at all."""
        return bool(self.allowlist) and not self.read_only


def parse_allowlist(raw: str | None) -> tuple[str, ...]:
    """Split a comma-separated handle list, dropping blanks and duplicates."""
    if not raw:
        return ()
    seen: list[str] = []
    for item in raw.split(","):
        handle = item.strip()
        if handle and handle not in seen:
            seen.append(handle)
    return tuple(seen)


def parse_bool(raw: str | None) -> bool:
    return (raw or "").strip().lower() in _TRUTHY


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build ``Settings`` from ``env`` (defaults to ``os.environ``)."""
    if env is None:
        env = os.environ

    password = env.get(ENV_PASSWORD, "")
    if not password:
        raise ConfigError(
            f"{ENV_PASSWORD} is not set; export the BlueBubbles server password and retry"
        )

    url = env.get(ENV_URL, "").strip() or DEFAULT_URL
    url = url.rstrip("/")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ConfigError(f"{ENV_URL} must start with http:// or https://, got {url!r}")

    return Settings(
        url=url,
        password=password,
        allowlist=parse_allowlist(env.get(ENV_ALLOWLIST)),
        read_only=parse_bool(env.get(ENV_READ_ONLY)),
    )
