# Vault Production Runbook

The base compose file runs Vault in dev mode for local setup. Production must
use `infra/docker-compose.prod.yml`, which starts Vault with Raft storage at
`/vault/data` and no dev root token.

## First Boot

1. Provide production compose variables: `POSTGRES_USER`, `POSTGRES_PASSWORD`,
   `POSTGRES_DB`, `MINIO_ROOT_USER`, and `MINIO_ROOT_PASSWORD`.
2. Start Vault with the production override:

```powershell
docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml up -d vault
```

3. Initialize Vault once:

```powershell
docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml exec vault `
  vault operator init -key-shares=5 -key-threshold=3
```

4. Store unseal keys and the root token in the organization's break-glass
   secret process. Do not write them to the repository.
5. Unseal with three separate keys:

```powershell
docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml exec vault `
  vault operator unseal <key>
```

## Snapshot Backup

```powershell
docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml exec vault `
  vault operator raft snapshot save /vault/data/vault-$(Get-Date -Format yyyyMMdd-HHmmss).snap
```

Move the snapshot to encrypted backup storage after creation.

## Snapshot Restore

1. Stop application services that read/write secrets.
2. Restore the selected snapshot:

```powershell
docker compose -f infra/docker-compose.yml -f infra/docker-compose.prod.yml exec vault `
  vault operator raft snapshot restore -force /vault/data/<snapshot>.snap
```

3. Unseal Vault again if it becomes sealed.
4. Probe one known non-production test key before releasing application traffic.

## Local Gate

The production gate was validated by starting a separate clean compose project
with the production Vault override, initializing Raft storage, writing a KV v2
secret, saving a snapshot, changing the value, restoring the snapshot, and
verifying that the original value returned.
