# temporal-shared

Shared Temporal constants and pure-function helpers for the platform.

This package is the **single source of truth** for the workflow-type →
capability mapping and the capability-gate algebra defined in
`platform-mimari-foundation` design.md (libs/temporal-shared.capabilities).
Other services and workers import these symbols rather than redefining
them locally (Requirement 4.1, design Property 7).

## Public API

### `WORKFLOW_TYPE_CAPABILITIES`

Immutable `MappingProxyType[str, frozenset[str]]` with exactly 10 entries.
Each value is a `frozenset` of required capability strings drawn from
the closed vocabulary `{"jira_read", "jira_write", "bitbucket_read",
"bitbucket_write", "confluence_read", "confluence_write", "execution",
"web_search"}`.

### `derive_capabilities(dept, env) -> frozenset[str]`

Pure function (no I/O). Returns the capability set a department holds
based on:

1. `bot.jira` has credential → `jira_read`, `jira_write`.
2. `bot.bitbucket` has credential → `bitbucket_read`, `bitbucket_write`.
3. `bot.confluence` has credential → `confluence_read`, `confluence_write`.
4. Any `SSH_HOST_<n>` key in `env` → `execution`.
5. `dept.web_search_enabled` and `env["FIRECRAWL_ENABLED"] == "true"` →
   `web_search`.

`SSH_RUNNER_DEPT_PINNING_ENABLED` and `SSH_DEPT_QUOTA_ENABLED` are *not*
consulted here — both default to off and dept-pinning / quota are later
concerns (Requirements 4.8, 4.9).

### `gate(workflow_type, dept, env) -> GateDecision`

Pure function that returns `GateDecision(allowed, missing)`:

- `allowed`: True iff every required capability is present.
- `missing`: `frozenset[str]` of absent capabilities (empty when allowed).

Raises `KeyError` when `workflow_type` is not in
`WORKFLOW_TYPE_CAPABILITIES`.

## Usage

```python
from temporal_shared import (
    WORKFLOW_TYPE_CAPABILITIES,
    derive_capabilities,
    gate,
)

required = WORKFLOW_TYPE_CAPABILITIES["code_change_with_test"]
# frozenset({"jira_read", "jira_write", "bitbucket_read",
#            "bitbucket_write", "execution"})

decision = gate("code_change_with_test", department, env)
if not decision.allowed:
    # decision.missing is a frozenset of capability strings
    ...
```

## Standalone build & install

This is a library package (no entry point); there is no service container
to run. To build and install it locally:

```bash
# from libs/temporal-shared/
python -m pip install --upgrade build
python -m build              # produces dist/temporal_shared-*.whl

# install into a target environment
python -m pip install dist/temporal_shared-*.whl
```

To use it from another package in this repository, add a path/wheel
reference in that package's `pyproject.toml`. No Docker image or runtime
container is provided for this library.

## Notes

- `WORKFLOW_TYPE_CAPABILITIES` is wrapped in `MappingProxyType`; mutation
  attempts raise `TypeError`. Treat it as read-only.
- `derive_capabilities` and `gate` are pure functions — no network or
  filesystem I/O (Requirement 4.7). All inputs flow through arguments.
- `GateDecision` is a frozen dataclass; attribute assignment fails after
  construction.
