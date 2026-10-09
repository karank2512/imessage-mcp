.PHONY: test lint
test:
	.venv/bin/python -m pytest -q

lint:
	@if .venv/bin/python -c "import pyflakes" 2>/dev/null; then \
		.venv/bin/python -m pyflakes imessage_mcp tests; \
	else \
		echo "pyflakes not available in .venv; skipping lint"; \
	fi
