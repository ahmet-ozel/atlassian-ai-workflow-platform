# Production Rollback Runbook

This runbook defines the minimum rollback procedure for the platform compose
deployment. A production rollback is accepted only when the rollback image,
database backup, Vault snapshot, object-store backup, and health probes are
all known-good.

## Preconditions

- The previous release image tags are still available in the registry.
- Postgres, MinIO, and Vault backups for the rollback point are available.
- Vault runs in production mode with Raft storage, not dev/in-memory mode.
- Prometheus alert rules and Alertmanager are loaded, and platform health
  probes are green.
- The operator has explicit approval to roll back the selected services.

## Procedure

1. Freeze incoming release changes and pause non-critical automations.
2. Record current image digests for every service being rolled back.
3. Restore data only when the release changed persisted schema or object shape:
   Postgres first, then Vault, then MinIO.
4. Deploy the previous image tag for the selected service or service group.
5. Verify `/healthz`, Prometheus `probe_success`, and service-specific smoke tests.
6. Keep the system under watch for at least one alert evaluation window.
7. Record the rollback event in the audit log with operator, reason, old image,
   new image, backup identifiers, and verification output.

## Local/Prod-Like Smoke

The repository includes a local rollback-mechanics smoke:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/prod_rollback_verify.ps1 `
  -Service admin-dashboard-api `
  -HealthUrl http://localhost:8082/healthz
```

This tags the currently running service image as a rollback image, redeploys
that tag, verifies health, and then restores the normal compose definition. It
does not replace a real registry rollback test, but it prevents the rollback
mechanics from drifting.

## Failure Policy

- If health does not recover inside the timeout, restore the pre-rollback image
  immediately and keep the incident open.
- If data restore fails, do not deploy application containers; escalate to the
  database/object-store owner.
- If Vault unseal or snapshot restore fails, stop the rollback and keep services
  that require secrets offline until Vault is healthy.
