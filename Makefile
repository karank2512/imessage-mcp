.PHONY: test lint check
PY = .venv/bin/python

# Override to narrow a run: make test TESTS=tests/test_read.py PYTEST_ARGS=-x
TESTS =
PYTEST_ARGS =

test:
	$(PY) -m pytest -q $(PYTEST_ARGS) $(TESTS)

lint:
	@if $(PY) -c "import pyflakes" 2>/dev/null; then \
		$(PY) -m pyflakes imessage_mcp tests; \
	else \
		echo "pyflakes not available in .venv; skipping lint"; \
	fi

# Drives the server over stdio: initialize, initialized, tools/list. Stdin is
# kept open until the tools/list reply arrives, because the server exits on
# EOF and may drop in-flight replies otherwise.
define CHECK_STDIO_PY
import json, os, subprocess, sys, threading
env = dict(os.environ, BLUEBUBBLES_PASSWORD="x")
proc = subprocess.Popen([sys.executable, "-m", "imessage_mcp.server"], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, text=True, env=env)
killer = threading.Timer(30, proc.kill)
killer.start()
reqs = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05",
        "capabilities": {}, "clientInfo": {"name": "make-check", "version": "0"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
]
for r in reqs:
    proc.stdin.write(json.dumps(r) + "\n")
proc.stdin.flush()
tools = None
while tools is None:
    line = proc.stdout.readline()
    if not line:
        break
    msg = json.loads(line)
    if msg.get("id") == 2:
        tools = [t["name"] for t in msg["result"]["tools"]]
proc.stdin.close()
proc.wait()
killer.cancel()
assert tools == ["server_health", "list_chats", "read_thread"], tools
print("stdio tools/list ->", tools, ": ok")
endef
export CHECK_STDIO_PY

# Acceptance checks that do not need a BlueBubbles server:
#  1. --help exits 0 and names every env variable
#  2. an MCP initialize + tools/list over stdio lists server_health, list_chats, read_thread
check:
	$(PY) -m imessage_mcp.server --help
	@for v in BLUEBUBBLES_URL BLUEBUBBLES_PASSWORD IMESSAGE_MCP_ALLOWLIST IMESSAGE_MCP_READ_ONLY; do \
		$(PY) -m imessage_mcp.server --help | grep -q "$$v" || { echo "--help does not mention $$v"; exit 1; }; \
	done
	@echo "--help names all env variables: ok"
	$(PY) -c "$$CHECK_STDIO_PY"
