# vault_client

Pluggable Vault KV-v2 client used by every service that resolves
credentials from `vault:<...>` references. Implements **R6** of
`platform-mimari-foundation`:

- `VaultPath` value-object that validates the
  `^vault:[a-zA-Z0-9/_-]+$` convention (R3.3, R6.1) and rejects
  anything else with `ValueError`.
- `VaultClient` `Protocol` defining `read`, `write`, `delete`,
  `rotate_ssh_key`, `rotate_webhook_secret` (design §"libs/vault_client").
- Two pluggable backends:
  - `HashicorpBackend` — real Hashicorp Vault HTTP (KV-v2,
    `data/<path>` semantics).
  - `LocalDevBackend` — file-backed development store using
    libsodium (`pynacl.secret.SecretBox`) authenticated encryption.
    Plain-text writes are **rejected** (R6.6).
- `make_client(env)` factory that selects the backend from the
  `VAULT_BACKEND` environment variable (`hashicorp` or `local-dev`).

This task (2.2) lands the **value-object, protocol, backends and
factory**. SSH dual-slot rotation (R6.7) and webhook secret 1h overlap
(R6.8) implementations live alongside in this package; task 2.3 wires
the rotation slots' invariants into property tests
(`test_ssh_rotation.py`, `test_vault_backends.py`).

## Layout

```
src/vault_client/
├── __init__.py             # public re-exports
├── path.py                 # VaultPath value-object
├── client.py               # VaultClient Protocol + dataclasses
├── hashicorp_backend.py    # real Vault HTTP (KV v2)
├── local_dev_backend.py    # libsodium encrypted file backend
└── factory.py              # make_client(env) — VAULT_BACKEND-driven
```

## Standalone build & run

```bash
# from the repository root
cd libs/vault_client

# create an isolated environment and install in editable mode
python -m venv .venv
. .venv/bin/activate          # on Windows: .venv\Scripts\activate
pip install -e .

# import smoke-test
python -c "from vault_client import VaultPath, make_client; \
  p = VaultPath.parse('vault:atlassian/payments/jira'); \
  print(p)"
```

## Path convention

The base grammar is intentionally permissive (`a-zA-Z0-9/_-`) so existing
`departments.schema.json` references stay valid; the lowercase /
kebab-case convention is enforced by the project style guide rather
than the regex (design §"Tasarım Kararları").

### Foundation paths (Spec 1)

```
vault:atlassian/<dept_id>/<service>            # Atlassian credentials
vault:atlassian/_staging/<request_id>/<svc>    # atomic-create staging
vault:webhooks/<provider>/<dept_id>            # per-dept HMAC secret (V3)
vault:infrastructure/openai/api_key            # LLM fallback
vault:minio/access_keys                        # object storage
vault:ssh/runners/<runner_id>/active           # SSH key (current slot)
vault:ssh/runners/<runner_id>/previous         # SSH key (rotation overlap)
```

### Ops-scope paths (Spec 3 — `platform-mimari-ops`)

The constants below live in `vault_client.path` and are re-exported from
the package root. Build concrete references with `str.format` and parse
the result through `VaultPath.parse(...)`:

```python
from vault_client import USER_SESSION_PATH_TEMPLATE, VaultPath

ref = "vault:" + USER_SESSION_PATH_TEMPLATE.format(
    session_id=session_id, service="jira",
)
path = VaultPath.parse(ref)
```

| Constant | Path Pattern | Owner | Content | TTL |
|---|---|---|---|---|
| `USER_SESSION_PATH_TEMPLATE` | `vault:atlassian/_user_session/{session_id}/{service}` | `assistant-service` (write), `automation-service` (read) | Per-user session credential (Q6/Q7) — Streamlit user's own Atlassian token, scoped to the active session | session lifetime; deleted on logout, 24h cron sweep for orphans (R3.4, R8.4) |
| `USER_PERSISTED_PATH_TEMPLATE` | `vault:atlassian/_user_persisted/{user_id}/{service}` | `streamlit-ui` (PIN-encrypted client-side) | Opt-in "remember me" persistence (Z7); ciphertext only — bytes are AES-encrypted with a PIN-derived key before write | 30 days (matches signed cookie TTL) |
| `NOTIFICATION_SMTP_PATH` | `vault:notifications/smtp/credential` | `notification_service` | SMTP server credentials for outbound email (R5.1) | rotation-driven (no fixed TTL) |
| `NOTIFICATION_SLACK_PATH_TEMPLATE` | `vault:notifications/{dept_id}/slack` | `notification_service` | Per-department Slack webhook URL (R5.1) | rotation-driven (no fixed TTL) |

**Critical rule:** Plain credentials are **never** stored in cookies or
local storage. The `_user_persisted/...` path stores AES-encrypted
ciphertext only; the PIN-derived key never leaves the user's browser
session.
