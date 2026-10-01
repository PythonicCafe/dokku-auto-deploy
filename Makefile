PYTHON = python3
SHELL_SCRIPTS = commands cron-entries help-functions internal-functions install update uninstall post-delete post-app-rename-setup subcommands/default

help:					# List all make commands
	@awk -F ':.*#' '/^[a-zA-Z_-]+:.*?#/ { printf "\033[36m%-15s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST) | sort

dev-install:				# Install dev dependencies (pytest, mypy, ruff, shellcheck)
	$(PYTHON) -m pip install --user --group dev

test:					# Execute pytest
	$(PYTHON) -m pytest tests/ -v --tb=short

test-coverage:				# Execute pytest with coverage report
	$(PYTHON) -m pytest tests/ -v --tb=short --cov=dokku_auto_deploy --cov-report=term-missing

test-fast:				# Execute pytest quietly (no verbose, last failures only)
	$(PYTHON) -m pytest tests/ -q --tb=line -x --lf

mypy:					# Execute `mypy --strict` in the codebase
	$(PYTHON) -m mypy

lint:					# Run ruff (check --fix + format)
	$(PYTHON) -m ruff check . --fix
	$(PYTHON) -m ruff format --line-length 120 .

lint-check:				# Run ruff without changing files, and shellcheck on the plugin scripts
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --line-length 120 --check .
	shellcheck $(SHELL_SCRIPTS)

check: lint-check mypy test		# Run everything (lint-check, mypy, test)

clean:					# Remove caches
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -exec rm -rf {} +

tags:					# Generate tags file for the entire project (requires universal-ctags)
	@git ls-files | ctags -L - --tag-relative=yes --quiet --append -f ".tags"

.PHONY: help dev-install test test-coverage test-fast mypy lint lint-check check clean tags
