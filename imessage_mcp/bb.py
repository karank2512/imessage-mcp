"""Thin client over the BlueBubbles REST API.

Only the stdlib is used. Every failure, from a refused connection to a 401 to
a body that is not JSON, surfaces as ``BBError``; callers never see urllib or
socket exceptions.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_TIMEOUT = 10.0

PATH_SERVER_INFO = "/api/v1/server/info"
PATH_CHAT_QUERY = "/api/v1/chat/query"
PATH_MESSAGE_QUERY = "/api/v1/message/query"
PATH_MESSAGE_TEXT = "/api/v1/message/text"


class BBError(Exception):
    """A BlueBubbles call failed.

    ``status`` is the HTTP status (or the ``status`` field of the response
    envelope). It is 0 when no HTTP response was received at all: refused
    connection, DNS failure, timeout, malformed URL.
    """

    def __init__(self, status: int, message: str):
        self.status = int(status)
        self.message = message
        if self.status:
            text = f"BlueBubbles returned {self.status}: {message}"
        else:
            text = f"BlueBubbles unreachable: {message}"
        super().__init__(text)


class BBClient:
    def __init__(self, base_url: str, password: str, timeout: float = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.timeout = timeout

    # ---- public API -----------------------------------------------------

    def server_info(self) -> dict[str, Any]:
        """GET /api/v1/server/info; returns the ``data`` object."""
        data = self._request("GET", PATH_SERVER_INFO)
        return data if isinstance(data, dict) else {}

    def query_messages(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        """POST /api/v1/message/query with ``body`` as-is; returns the ``data`` list."""
        data = self._request("POST", PATH_MESSAGE_QUERY, body)
        return data if isinstance(data, list) else []

    def chats(self, limit: int = 25) -> list[dict[str, Any]]:
        """POST /api/v1/chat/query for the most recently active chats."""
        body = {
            "limit": int(limit),
            "offset": 0,
            "with": ["lastMessage"],
            "sort": "lastmessage",
        }
        data = self._request("POST", PATH_CHAT_QUERY, body)
        return data if isinstance(data, list) else []

    def send_text(self, chat_guid: str, text: str) -> dict[str, Any]:
        """POST /api/v1/message/text via AppleScript (no Private API)."""
        body = {"chatGuid": chat_guid, "message": text, "method": "apple-script"}
        data = self._request("POST", PATH_MESSAGE_TEXT, body)
        return data if isinstance(data, dict) else {}

    # ---- transport ------------------------------------------------------

    def _url(self, path: str) -> str:
        query = urllib.parse.urlencode({"password": self.password})
        return f"{self.base_url}{path}?{query}"

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        headers = {"Accept": "application/json"}
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self._url(path), data=data, method=method, headers=headers)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = int(resp.status)
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise BBError(exc.code, _error_message(_safe_read(exc), str(exc.reason))) from None
        except urllib.error.URLError as exc:
            raise BBError(0, _reason_text(exc.reason)) from None
        except TimeoutError:
            raise BBError(0, f"timed out after {self.timeout:g}s") from None
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise BBError(0, _reason_text(exc)) from None

        payload = _decode(raw, status)
        if isinstance(payload, dict):
            env_status = payload.get("status")
            if isinstance(env_status, int) and env_status >= 400:
                raise BBError(env_status, _error_message(payload, "error"))
            return payload.get("data")
        return payload


# ---- helpers -------------------------------------------------------------


def _safe_read(exc: urllib.error.HTTPError) -> Any:
    try:
        raw = exc.read()
    except Exception:  # pragma: no cover - defensive
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None


def _error_message(payload: Any, fallback: str) -> str:
    """Pull the most specific message out of a BlueBubbles error envelope."""
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if payload.get("message"):
            return str(payload["message"])
    return fallback or "error"


def _reason_text(reason: Any) -> str:
    if isinstance(reason, TimeoutError):
        return "timed out"
    text = str(reason).strip()
    return text or reason.__class__.__name__


def _decode(raw: bytes, status: int) -> Any:
    if not raw:
        raise BBError(status, "empty response body")
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise BBError(status, "response body is not JSON") from None
