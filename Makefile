.DEFAULT_GOAL := help
PYTHON ?= .venv/bin/python
export PATH := $(CURDIR)/.venv/bin:$(PATH)
TEST_ARGS ?=
SOURCES := factory tests tools modules/examples examples
EXAMPLE_DIR ?= .venv/text-classification

.PHONY: help setup install format lint typecheck check test test-all build release-candidate example

help:
	@printf '%s\n' 'make setup       Create the locked development environment' 'make check       Check formatting, lint, and types' 'make test        Run tests; select with TEST_ARGS="tests/test_agent.py -q"' 'make test-all    Run checks and all tests with branch coverage' 'make format      Format code and apply safe lint fixes' 'make build       Build wheel and source distribution'

setup install:
	python3 tools/bootstrap.py

format:
	$(PYTHON) -m ruff check --fix $(SOURCES)
	$(PYTHON) -m ruff format $(SOURCES)

lint:
	$(PYTHON) -m ruff check $(SOURCES)
	$(PYTHON) -m ruff format --check $(SOURCES)

typecheck:
	$(PYTHON) -m mypy

check: lint typecheck

test:
	$(PYTHON) -m pytest $(TEST_ARGS)

test-all: check
	$(PYTHON) -m pytest --cov=openfoundry --cov-report=term $(TEST_ARGS)

build:
	$(PYTHON) -m build --no-isolation

example:
	$(PYTHON) examples/text-classification/run.py $(EXAMPLE_DIR)

release-candidate:
	$(PYTHON) tools/release.py --candidate --output dist \
		--source-revision "$$(git rev-parse HEAD)" \
		--source-date-epoch "$$(git show -s --format=%ct HEAD)"
