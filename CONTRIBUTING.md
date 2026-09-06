# Contributing

Start with [AGENTS.md](AGENTS.md) for the code map and engineering guidance.
[Architecture](docs/architecture.md) explains the lifecycle and invariants.

## Development environment

```sh
make setup
make check
make test TEST_ARGS='tests/test_agent.py -q'
```

Setup finds Python 3.11 or 3.12, creates `.venv/`, installs hash-locked
dependencies into a local wheel cache, and installs OpenFoundry in editable mode.
Installation tests use that cache to build isolated environments without
network access or dependencies inherited from system Python. Setup preserves an
existing environment and does not modify system Python or Git hooks. To select an
interpreter explicitly, run `python3 tools/bootstrap.py --python python3.11`
before creating the environment.

Make uses `.venv/bin/python`; CI runs the same setup and verification commands.
Use `PYTHON=python` to select another prepared environment. Full installation
tests still require the wheel cache from `make setup`. Run `make` to see the
available commands.

## Verification

Run focused tests during development. For changes across runtime boundaries,
run `make test-all`, which includes lint, formatting, strict types, the full
suite, and the existing 85% branch-coverage threshold. Run `make build` for
packaging, setup, or bundled-resource changes.

The full lifecycle requires Linux with unprivileged user namespaces. A Mac
can run the metadata, API, schema, and storage tests, but cannot verify Linux
network denial. Use a Linux environment for the complete suite; do not remove
that requirement to get a passing run.

Change formats, implementation, CLI/API contracts, relevant tests, and docs
together. Test meaningful behavior at the boundary that changed. Explain actual
compatibility or security implications in the review; avoid speculative support
claims. Keep generated state, secrets, payloads, coverage, and build output out
of commits.

By contributing, you agree that your contribution is licensed under Apache-2.0.
Use [SECURITY.md](SECURITY.md) to report vulnerabilities.
