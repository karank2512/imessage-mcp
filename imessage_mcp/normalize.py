"""Shape raw BlueBubbles chat and message records into the tool results.

Pure functions, no I/O. Dates arrive from BlueBubbles as epoch milliseconds
and leave as ISO 8601 strings in the local timezone with an explicit offset,
e.g. ``2023-11-14T22:13:20+00:00``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


# ---- dates ----------------------------------------------------------------


def iso_from_ms(ms: Any) -> str | None:
    """Epoch milliseconds -> ISO 8601 with offset, or None when absent/invalid."""
    if ms is None or isinstance(ms, bool):
        return None
    try:
        seconds = int(ms) / 1000
    except (TypeError, ValueError):
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def ms_from_iso(text: str) -> int:
    """ISO 8601 -> epoch milliseconds.

    A trailing ``Z`` is accepted. A timestamp without an offset is read as
    local time, matching what ``iso_from_ms`` produces. Raises ``ValueError``
    on anything else.
    """
    value = (text or "").strip()
    if not value:
        raise ValueError("empty timestamp")
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return round(parsed.timestamp() * 1000)


# ---- messages -------------------------------------------------------------


def attributed_text(attributed: Any) -> str:
    """Pull plain text out of BlueBubbles' parsed ``attributedBody``.

    BlueBubbles emits it as a list of ``{"string": ..., "runs": [...]}``
    fragments; a bare dict or string is tolerated too.
    """
    if isinstance(attributed, str):
        return attributed
    if isinstance(attributed, dict):
        value = attributed.get("string")
        return value if isinstance(value, str) else ""
    if isinstance(attributed, list):
        return "".join(attributed_text(part) for part in attributed)
    return ""


def message_text(msg: dict[str, Any]) -> str:
    """``text``, else the ``attributedBody`` text, else ``""``."""
    text = msg.get("text")
    if isinstance(text, str) and text:
        return text
    return attributed_text(msg.get("attributedBody"))


def message_sender(msg: dict[str, Any]) -> str:
    handle = msg.get("handle")
    if isinstance(handle, dict):
        address = handle.get("address")
        if isinstance(address, str) and address:
            return address
    return "me" if msg.get("isFromMe") else ""


def normalize_message(msg: dict[str, Any]) -> dict[str, Any]:
    attachments = msg.get("attachments")
    reply_to = msg.get("threadOriginatorGuid")
    return {
        "id": str(msg.get("guid") or ""),
        "date": iso_from_ms(msg.get("dateCreated")),
        "from_me": bool(msg.get("isFromMe")),
        "sender": message_sender(msg),
        "text": message_text(msg),
        "attachments": len(attachments) if isinstance(attachments, list) else 0,
        "reply_to": str(reply_to) if reply_to else None,
    }


def message_ms(msg: dict[str, Any]) -> int | None:
    """``dateCreated`` as an int, or None when missing or not a number."""
    value = msg.get("dateCreated")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def normalize_messages(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise and order oldest -> newest (stable; undated messages first)."""
    records = [m for m in raw if isinstance(m, dict)]
    records.sort(key=lambda m: (message_ms(m) is not None, message_ms(m) or 0))
    return [normalize_message(m) for m in records]


# ---- chats ----------------------------------------------------------------


def chat_participants(chat: dict[str, Any]) -> list[str]:
    handles: list[str] = []
    for p in chat.get("participants") or []:
        if isinstance(p, dict):
            address = p.get("address")
            if isinstance(address, str) and address and address not in handles:
                handles.append(address)
    return handles


def chat_unread(chat: dict[str, Any]) -> bool | None:
    """Best effort from the embedded last message.

    True when the last message came from someone else and has no ``dateRead``;
    False when it is ours or has been read; None when there is no last message
    or it lacks the fields needed to tell.
    """
    last = chat.get("lastMessage")
    if not isinstance(last, dict) or "isFromMe" not in last:
        return None
    if last.get("isFromMe"):
        return False
    if "dateRead" not in last:
        return None
    return last.get("dateRead") is None


def normalize_chat(chat: dict[str, Any]) -> dict[str, Any]:
    last = chat.get("lastMessage")
    last_at = iso_from_ms(last.get("dateCreated")) if isinstance(last, dict) else None
    return {
        "chat_guid": str(chat.get("guid") or ""),
        "display_name": str(chat.get("displayName") or ""),
        "participants": chat_participants(chat),
        "last_message_at": last_at,
        "unread": chat_unread(chat),
    }


def normalize_chats(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise and order newest activity first; chats without one go last."""
    items = [normalize_chat(c) for c in raw if isinstance(c, dict)]
    items.sort(key=lambda c: (c["last_message_at"] is None, _negate_ms(c["last_message_at"])))
    return items


def _negate_ms(iso: str | None) -> float:
    if iso is None:
        return 0.0
    return -datetime.fromisoformat(iso).timestamp()
