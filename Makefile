.PHONY: lint format typecheck test audit check install-dev clean

PYTHON := .venv/bin/python

install-dev:
	$(PYTHON) -m pip install -e ".[dev]"

lint:
	$(PYTHON) -m ruff check vinsamlib/

format:
	$(PYTHON) -m ruff format vinsamlib/
	$(PYTHON) -m ruff check --fix vinsamlib/

typecheck:
	$(PYTHON) -m mypy vinsamlib/

test:
	$(PYTHON) -m pytest tests/ -x -q

audit:
	$(PYTHON) -m pip_audit --progress-spinner off --ignore-vuln PYSEC-2026-3721 --ignore-vuln PYSEC-2025-49 --ignore-vuln PYSEC-2026-1918 --ignore-vuln PYSEC-2026-3447
	$(PYTHON) -m vulture vinsamlib/ --min-confidence 80
	$(PYTHON) -m deptry .
	$(PYTHON) -m detect_secrets scan --baseline .secrets.baseline

check: lint typecheck test audit
	@echo "All checks passed."

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
