# MinIO (dev mode only)

Dev-mode MinIO; bucket bootstrap is performed by the
[`init.sh`](./init.sh) script (platform-mimari-ops task 13.4).

## Bucket inventory

| Bucket          | Purpose                                                                  | Owner / writer                                | Retention                              |
|-----------------|--------------------------------------------------------------------------|-----------------------------------------------|----------------------------------------|
| `ai-runs`       | Execution artifacts (stdout, stderr, exit_code) keyed by workflow id     | `execution-runner-worker.minio_upload_artifact` | Workflow lifetime + post-mortem window |
| `audit-archive` | Daily-partitioned audit-log archive — `audit-archive/{Y}/{M}/{D}/audit-N.jsonl.gz` | `automation-worker.archive_audit_to_minio`     | `RETENTION_DAYS` (default 90 days)     |

The `audit-archive` layout matches design.md §"MinIO arşiv yapısı"
(platform-mimari-ops Requirement 6.3) and is consumed by
`admin_dashboard_api.audit.archive_index.MinIOArchiveIndex` for the
audit panel's date-bucketed archive search (Requirement 6.5, 6.9).

## Bootstrap

The `minio` Compose service ships an empty data volume; running
[`init.sh`](./init.sh) creates the buckets above and is **idempotent**
— `BucketAlreadyOwnedByYou` is treated as success.

```sh
# After `docker compose --profile minio up -d minio`:
bash platform/infra/minio/init.sh
```

The script:

* Polls `GET /minio/health/live` until the server responds (≤ 60s
  total, 30 attempts × 2s).
* Prefers the `mc` MinIO client if installed and otherwise falls back
  to the S3-compatible HTTP API via `curl` + AWS Signature V4 (no
  extra runtime requirement).
* Honours these env vars (defaults match the dev Compose stack):

  | Variable                | Default                  |
  |-------------------------|--------------------------|
  | `MINIO_ENDPOINT`        | `localhost:9000`         |
  | `MINIO_USE_SSL`         | `false`                  |
  | `MINIO_ROOT_USER`       | `minio`                  |
  | `MINIO_ROOT_PASSWORD`   | `miniosecret_dev_only`   |
  | `AUDIT_ARCHIVE_BUCKET`  | `audit-archive`          |
  | `AI_RUNS_BUCKET`        | `ai-runs`                |

When run inside the Compose network (e.g. via `docker compose exec`),
override the endpoint:

```sh
MINIO_ENDPOINT=minio:9000 bash /workspace/platform/infra/minio/init.sh
```

## Production hardening (deferred — Requirement 18.5)

The dev-mode bootstrap intentionally **does not** apply object-lock,
versioning enforcement, or lifecycle policies. Production deployments
are expected to layer the following on top before going live:

1. **Object-lock + COMPLIANCE retention** for `audit-archive`. The
   bucket should be created with `mc mb --with-lock` and a default
   retention of at least `RETENTION_DAYS` days
   (write-once / WORM — protects archived audit data from
   tampering).
2. **Lifecycle transition** to a cold storage tier after N days for
   the `audit-archive` bucket; expiry is **disabled** so archived
   audit data is never auto-deleted.
3. **TLS** between Compose services and MinIO (set `MINIO_USE_SSL=true`).
4. **Per-service IAM policies** instead of root credentials for the
   `automation-worker` (write-only on `audit-archive/*`) and the
   `admin-dashboard-api` (read-only / list on `audit-archive/*`).

These are deliberately out of scope for the dev-mode scaffold per
multi-service-scaffold Requirement 18.5; track them under
production-hardening backlog when the platform graduates from dev
profile.
