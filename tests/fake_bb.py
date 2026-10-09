"""A stdlib fake of the BlueBubbles REST API for tests.

Serves canned JSON from ``tests/fixtures`` for the four endpoints the client
uses, checks the ``password`` query parameter, and records every request so
tests can assert on paths and bodies. All data is invented.

Usage::

    with FakeBlueBubbles(password="fixture-pw") as fake:
        client = BBClient(fake.url, "fixture-pw")
        ...
        fake.records[0].path == "/api/v1/server/info"
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

FIXTURES = Path(__file__).parent / "fixtures"

ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/v1/server/info"): "server_info.json",
    ("POST", "/api/v1/chat/query"): "chat_query.json",
    ("POST", "/api/v1/message/query"): "message_query.json",
    ("POST", "/api/v1/message/text"): "message_text.json",
}


@dataclass
class Recorded:
    method: str
    path: str
    query: dict[str, list[str]]
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def password(self) -> str | None:
        values = self.query.get("password")
        return values[0] if values else None


def load_fixture(name: str) -> dict[str, Any]:
    with (FIXTURES / name).open("r", encoding="utf-8") as fh:
        return json.load(fh)


class _Handler(BaseHTTPRequestHandler):
    server: "FakeBlueBubbles"  # type: ignore[assignment]

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass

    def _read_body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return None
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return raw.decode("utf-8", "replace")

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self, method: str) -> None:
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        body = self._read_body() if method == "POST" else None
        rec = Recorded(
            method=method,
            path=parts.path,
            query=query,
            body=body,
            headers={k: v for k, v in self.headers.items()},
        )
        with self.server.lock:
            self.server.records.append(rec)
            override = self.server.overrides.get((method, parts.path))

        if rec.password != self.server.password:
            self._send(
                401,
                {
                    "status": 401,
                    "message": "Unauthorized",
                    "error": {
                        "type": "Authentication Error",
                        "message": "Invalid password",
                    },
                },
            )
            return

        if override is not None:
            status, payload = override
            if isinstance(payload, (bytes, str)):
                raw = payload if isinstance(payload, bytes) else payload.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            else:
                self._send(status, payload)
            return

        fixture = ROUTES.get((method, parts.path))
        if fixture is None:
            self._send(
                404,
                {
                    "status": 404,
                    "message": "Not Found",
                    "error": {"type": "Not Found", "message": f"no route for {method} {parts.path}"},
                },
            )
            return

        self._send(200, load_fixture(fixture))

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        self._handle("POST")


class FakeBlueBubbles(ThreadingHTTPServer):
    """Fake BlueBubbles bound to 127.0.0.1 on a free port."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, password: str = "fixture-pw"):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.password = password
        self.records: list[Recorded] = []
        self.overrides: dict[tuple[str, str], tuple[int, Any]] = {}
        self.lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> "FakeBlueBubbles":
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def override(self, method: str, path: str, status: int, payload: Any) -> None:
        """Make ``method path`` answer ``status`` with ``payload`` (JSON, or raw str/bytes)."""
        with self.lock:
            self.overrides[(method, path)] = (status, payload)

    def clear(self) -> None:
        with self.lock:
            self.records.clear()
            self.overrides.clear()

    def __enter__(self) -> "FakeBlueBubbles":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()
