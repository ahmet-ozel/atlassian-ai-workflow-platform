# Multi-Service Scaffold — Test Suite

This directory hosts the workspace-level property, unit, and integration
tests for the `multi-service-scaffold` spec. Each in-scope Component
under `services/`, `workers/`, `libs/`, and `ui/` ships its own
component-level tests; this suite focuses on **structural invariants**
(directory tree, port uniqueness, Compose DAG, env coverage, Dockerfile
shape, schema validation) and on the small amount of cross-component
behavior that needs to be exercised end-to-end.

## Layout

```
tests/
├── conftest.py            # shared fixtures + sys.path wiring for libs/
├── property/              # Hypothesis property tests (Properties 1–12)
├── unit/                  # focused unit tests (PBT-free)
├── integration/           # Docker Compose smoke tests (--run-docker gated)
├── fixtures/              # baselines (e.g. atlassian_unified snapshot)
├── pyproject.toml         # test-package dependency manifest
└── requirements.txt       # pip-installable mirror of the manifest
```

## Install

The suite has no transitive coupling to the in-scope service packages;
installing the dependencies pinned in `tests/requirements.txt` is
sufficient to run every property and unit test.

```bash
# from the workspace root
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # Linux / macOS

pip install -r tests/requirements.txt
```

Equivalently, the same set is declared in `tests/pyproject.toml` and can
be installed with:

```bash
pip install -e tests/
```

The two manifests are kept in lock-step; either is sufficient. The
authoritative dependency list lives in design §6.8 of
`.kiro/specs/multi-service-scaffold/design.md`.

## Run

The workspace-root `pytest.ini` already points `testpaths` at this
directory and wires every `libs/<lib>/src/` onto `pythonpath`, so tests
can be invoked from the workspace root:

```bash
pytest                    # full property + unit suite
pytest tests/             # explicit path (equivalent)
pytest tests/property/    # only Hypothesis property tests
pytest tests/unit/        # only unit tests
```

### Integration tests (opt-in)

The Compose-bound smoke tests under `tests/integration/` require Docker
Desktop / Engine to be running and are gated by the `--run-docker`
flag declared in `conftest.py`:

```bash
pytest tests/integration/ --run-docker
```

Without the flag these tests are collected but skipped, keeping CI's
fast lane free of Docker dependencies.

## Hypothesis settings

Property tests use the default Hypothesis profile (`max_examples` per
test ranges from 100 to 200, see individual files). Failing examples
are persisted under `.hypothesis/` at the workspace root; this
directory is `.gitignore`'d.

## Adding a new test dependency

1. Add the pinned range to **both** `tests/requirements.txt` and the
   `[project.dependencies]` array in `tests/pyproject.toml`.
2. If the dependency is only needed for integration / Docker tests,
   add it to `[project.optional-dependencies].docker` instead so the
   default install footprint stays minimal.
3. Update design §6.8 if the dependency reflects a new structural
   property rather than a one-off fixture concern.
