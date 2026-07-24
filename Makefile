VENV := .venv
PY := $(VENV)/bin/python

.PHONY: help venv install install-dev browsers run test test-frontend test-all clean

help: ## List targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-15s %s\n", $$1, $$2}'

$(VENV)/bin/python:
	python3 -m venv $(VENV)

venv: $(VENV)/bin/python ## Create the virtualenv

install: venv ## Install runtime dependencies
	$(PY) -m pip install -r requirements.txt

install-dev: install ## Install runtime + dev/test dependencies
	$(PY) -m pip install -r requirements-dev.txt

browsers: install-dev ## Install chromium for the frontend tests
	$(PY) -m playwright install chromium

run: ## Start the editor on http://127.0.0.1:5005
	$(PY) server.py

test: ## Run backend tests (skips browser-driven suite, matches CI matrix)
	$(PY) -m pytest -q -m "not frontend"

test-frontend: ## Run browser-driven frontend tests (needs `make browsers` once)
	$(PY) -m pytest -q -m frontend

test-all: ## Run the full test suite
	$(PY) -m pytest -q

clean: ## Remove caches (keeps .venv, _edit/, _backup_mdl/)
	rm -rf .pytest_cache __pycache__ tests/__pycache__
