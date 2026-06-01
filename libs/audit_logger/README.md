# audit_logger

Audit event dataclass + writer for the platform-mimari-foundation spec
(`MIMARI.md` §15, `design.md` §`libs/audit_logger`). The package
captures every state-changing action — capability gating decisions,
RBAC denials, webhook drops, credential rotations, and the like — so
operators can trace every effect back to a concrete actor, role, and
department.

## Public API

```python
from audit_logger import AuditEvent, AuditLogger

event = AuditEvent(
    actor_id="bot.payment.jira",
    actor_role="system",
    dept_id="payment",
    action="capability_denied",
    resource="workflow:code_change_with_test",
    result="denied",
    timestamp=datetime.now(timezone.utc),
    payload={"missing": ["bitbucket_write"]},
)

# When wired up to db-shared (see ``writer.py``):
await AuditLogger(session=tenant_aware_session).write(event)
```

`AuditEvent` is `frozen=True` and uses `Literal` types for
`actor_role` and `result`, which mirror the Postgres `CHECK` columns
declared in `infra/postgres/init/10_automation.sql` (see task group 4
of `platform-mimari-foundation/tasks.md`).

## Invariant: `actor_role` is mandatory

`AuditLogger.write()` raises `ValueError` if `event.actor_role` is
`None` (or the empty string). Postgres also enforces this with a
`CHECK (actor_role IS NOT NULL ...)` constraint — the application
check exists so callers fail fast with a clear message *before* the
round-trip to Postgres. This is the same defence-in-depth pattern
captured by Property 13 (`test_audit_one_to_one.py`,
`tests/property/`).

## DB integration

Writes go through `db-shared`'s tenant-aware session so the row lands
in `audit_events` with `app.current_dept_id` and `app.current_role`
set by the caller's RLS context. The actual `INSERT` SQL is emitted
in task group 4 alongside the schema migration; the scaffold here
only validates inputs and delegates to a session-like writer
interface.

## Standalone build & run

```bash
# from libs/audit_logger/
python -m pip install --upgrade build
python -m build              # produces dist/audit_logger-*.whl

# install into a target environment
python -m pip install dist/audit_logger-*.whl
```
