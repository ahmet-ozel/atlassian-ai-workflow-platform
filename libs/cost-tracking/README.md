# cost-tracking

Shared lib bridging `shared.cost_tracking` Postgres table writes and
the `CostPredictor` pure function consumed by `automation-service`'s
budget cap policy.

* `CostTracker.record(entry)` — idempotent insert.
  Uses `INSERT ... ON CONFLICT (activity_id) DO NOTHING` so a Temporal
  activity retry never double-bills.
* `CostPredictor.predict(...)` — pure function, dept-history mean with
  global-fallback when `dept_history.task_count < 30`. Returns a
  `CostPrediction` carrying `predicted_usd`, confidence interval, and
  the `source` flag (`"dept"` | `"global_fallback"`) so the caller
  can audit the fallback transition.

The async insert path depends on `asyncpg` only as an optional extra;
unit tests exercise the protocol via a list-backed fake.
