"""``send_message``: the one tool that writes, gated by the allowlist.

The allowlist comes from ``IMESSAGE_MCP_ALLOWLIST`` in the server's
environment and nowhere else; nothing the model passes can widen it. Every
attempt, allowed or refused, is appended as one JSON line to the file named
by ``IMESSAGE_MCP_LOG`` when that is set. The line carries the handle, the
text length and the outcome, never the text.

Handle matching: both the ``handle`` argument and every allowlist entry go
through ``normalize_handle`` and must then be equal. The normalisation
removes spaces, dashes and parentheses from phone numbers, keeps a leading
``+``, and lower-cases email addresses. It never adds a country code, so
``(608) 555-0100`` matches an allowlist entry written as ``608-555-0100`` or
``6085550100`` but not one written as ``+16085550100``. Write allowlist
entries the way the model will see handles in ``list_chats``, which is E.164
with the leading ``+`` for phone numbers.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import IO, Any, Iterable

from .bb import BBClient, BBError, PATH_MESSAGE_TEXT
from .config import ENV_ALLOWLIST, ENV_LOG, Settings

MAX_TEXT_LENGTH = 2000
MEDIA_PREFIX = "MEDIA:"
# BlueBubbles resolves "any;-;<handle>" to the existing one-to-one chat with
# that handle, iMessage or SMS, whichever exists.
CHAT_GUID_PREFIX = "any;-;"
SEND_METHOD = "apple-script"

_PHONE_NOISE = str.maketrans("", "", " -()")

OUTCOME_SENT = "sent"
OUTCOME_REFUSED = "refused"
OUTCOME_FAILED = "failed"


class SendError(Exception):
    """``send_message`` did not send.

    ``outcome`` is ``refused`` when a policy check stopped the attempt before
    any request was made, and ``failed`` when BlueBubbles was asked and did
    not accept the message.
    """

    def __init__(self, outcome: str, message: str):
        self.outcome = outcome
        super().__init__(message)


def normalize_handle(raw: Any) -> str:
    """Canonical form of a handle for allowlist comparison.

    Email addresses (anything containing ``@``) are trimmed and lower-cased.
    Phone numbers lose spaces, dashes and parentheses; a leading ``+`` stays.
    Nothing else changes: no country code is inferred, no digits are added.
    """
    if not isinstance(raw, str):
        return ""
    handle = raw.strip()
    if "@" in handle:
        return handle.lower()
    return handle.translate(_PHONE_NOISE)


def allowed_handles(allowlist: Iterable[str]) -> frozenset[str]:
    """The allowlist in normalised form, blanks dropped."""
    return frozenset(h for h in (normalize_handle(entry) for entry in allowlist) if h)


def check_send(settings: Settings, handle: Any, text: Any) -> str:
    """Apply every refusal rule; return the normalised handle to send to.

    Raises ``SendError`` with outcome ``refused`` on the first rule that
    fails. No request is made here.
    """
    if not settings.allowlist:
        raise SendError(
            OUTCOME_REFUSED, f"sending is disabled: {ENV_ALLOWLIST} is empty in the server's environment"
        )
    normalized = normalize_handle(handle)
    if not normalized:
        raise SendError(OUTCOME_REFUSED, "handle is required: a phone number or email address")
    if normalized not in allowed_handles(settings.allowlist):
        raise SendError(OUTCOME_REFUSED, f"handle {str(handle).strip()!r} is not in {ENV_ALLOWLIST}")
    if not isinstance(text, str) or not text.strip():
        raise SendError(OUTCOME_REFUSED, "text is empty")
    if len(text) > MAX_TEXT_LENGTH:
        raise SendError(
            OUTCOME_REFUSED, f"text is {len(text)} characters; the limit is {MAX_TEXT_LENGTH}"
        )
    if text.lstrip().upper().startswith(MEDIA_PREFIX):
        raise SendError(
            OUTCOME_REFUSED, f"text starts with {MEDIA_PREFIX}; attachments are not supported"
        )
    return normalized


def send_message(client: BBClient, settings: Settings, handle: str, text: str) -> dict[str, Any]:
    """Send ``text`` to ``handle`` if the allowlist permits it.

    Returns ``{sent, chat_guid, message_id}``. Raises ``SendError`` when the
    attempt is refused or BlueBubbles fails; either way exactly one log line
    is written when ``settings.log_path`` is set.
    """
    with _open_log(settings.log_path) as log:
        try:
            normalized = check_send(settings, handle, text)
            result = _post_text(client, normalized, text)
        except SendError as err:
            _log_line(log, normalize_handle(handle) or str(handle).strip(), text, err.outcome, reason=str(err))
            raise
        _log_line(log, normalized, text, OUTCOME_SENT, message_id=result["message_id"])
        return result


def _post_text(client: BBClient, normalized: str, text: str) -> dict[str, Any]:
    chat_guid = CHAT_GUID_PREFIX + normalized
    body = {
        "chatGuid": chat_guid,
        "tempGuid": str(uuid.uuid4()),
        "message": text,
        "method": SEND_METHOD,
    }
    try:
        # BBClient.send_text omits tempGuid, which the text endpoint needs;
        # the body is posted here instead so this module owns the full shape.
        data = client._request("POST", PATH_MESSAGE_TEXT, body)
    except BBError as err:
        raise SendError(OUTCOME_FAILED, str(err)) from None
    if not isinstance(data, dict):
        data = {}
    for chat in data.get("chats") or []:
        if isinstance(chat, dict) and chat.get("guid"):
            chat_guid = str(chat["guid"])
            break
    return {"sent": True, "chat_guid": chat_guid, "message_id": str(data.get("guid") or "")}


# ---- attempt log ------------------------------------------------------------


class _NoLog:
    def write(self, _: str) -> None:
        pass

    def flush(self) -> None:
        pass

    def __enter__(self) -> "_NoLog":
        return self

    def __exit__(self, *exc: Any) -> None:
        pass


def _open_log(path: str) -> IO[str] | _NoLog:
    """Open the attempt log for append, before anything is decided.

    A log that cannot be written is a refusal: a send that could not be
    recorded must not happen.
    """
    if not path:
        return _NoLog()
    try:
        return open(path, "a", encoding="utf-8")
    except OSError as err:
        raise SendError(OUTCOME_REFUSED, f"cannot append to {ENV_LOG} ({path}): {err}") from None


def _log_line(log: Any, handle: str, text: Any, outcome: str, **extra: Any) -> None:
    entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "handle": handle,
        "length": len(text) if isinstance(text, str) else 0,
        "outcome": outcome,
    }
    entry.update(extra)
    log.write(json.dumps(entry) + "\n")
    log.flush()
