# db-shared

Tenant-aware database session primitives consumed by the HTTP services
and Temporal workers. The current scaffold ships only the
`TenantAwareSession` placeholder; the real implementation will issue
`SET LOCAL app.tenant_id = ...` so Postgres row-level security policies
can filter per tenant.

## Standalone build & run

```bash
# from the repository root
cd libs/db-shared

# create an isolated environment and install in editable mode
python -m venv .venv
. .venv/bin/activate          # on Windows: .venv\Scripts\activate
pip install -e .

# import smoke-test
python -c "from db_shared import TenantAwareSession; \
  s = TenantAwareSession('payment', 'postgresql://localhost/ai'); \
  s.set_rls(); print(s.tenant_id, s.dsn)"

# build a wheel (optional)
pip install build
python -m build
```

The package has no runtime dependencies today; once the real session
manager is implemented it will pull in `asyncpg` (and/or `sqlalchemy`).
