# Atlassian AI Workflow Platform

Self-hosted, multi-department **AI automation for Jira, Confluence and
Bitbucket**. Assign a Jira issue to the bot and the platform turns it into real
work: it reasons over the task with an LLM, makes code changes on a runner,
opens a draft Bitbucket pull request, publishes a Confluence page, and comments
back on the issue - all driven by durable Temporal workflows.

It ships as a single Docker Compose stack: a stateless Atlassian MCP gateway, a
webhook intake service, Temporal workers, an admin dashboard for governance, and
a Streamlit chat front end for end users.

**Keywords:** Atlassian automation, Jira bot, Confluence automation, Bitbucket
pull request automation, MCP (Model Context Protocol) server, Temporal
workflows, LLM agent, self-hosted, Docker Compose.

## Highlights

- **Stateless, multi-tenant MCP gateway** - every request carries its own
  Atlassian credentials in headers, so a single gateway serves many users and
  departments without server-side sessions.
- **Secrets in Vault** - department credentials and tokens are stored in
  HashiCorp Vault and referenced by handle; raw tokens never live in config or
  source.
- **Admin dashboard** - governance surface for departments, services,
  capabilities, workflows and pull-request review.
- **Streamlit front end** - the end-user surface for credentials, chat and task
  creation.
- **Temporal automation** - durable workflows turn a Jira event into an LLM
  decision, code changes on a runner, a draft pull request, a Confluence page
  and a Jira update.
- **Env-driven host ports** - every published port comes from `infra/.env`, so a
  deployment can avoid clashes without touching Compose or source.

## Quick Start

For a first-time setup we recommend **bootstrap-only** mode: only the
`admin-dashboard` and its dependencies start; every other service is activated
on demand, under operator control, from the Setup Wizard inside the dashboard.

```bash
cd platform
# infra/.env carries the bootstrap values (Postgres, Vault, host ports, LLM).
# It is git-ignored, so create it on a fresh clone before the first boot.
make boot               # starts postgres + vault + admin-dashboard-{api,ui}
```

Then open the dashboard in your browser:

```
http://localhost:33000
```

The Setup Wizard brings up the remaining services step by step and, on the
final step, asks you to add the first department.

> **Full walkthrough:** [`docs/runbooks/getting-started.md`](docs/runbooks/getting-started.md).

If `make` is not installed (the Windows default):

```powershell
.\scripts\up.ps1 boot
```

or, on a POSIX shell:

```bash
./scripts/up.sh boot
```

## Published host ports

Every service always listens on a fixed **container** port internally; only the
**host**-published port is configurable, so a deployment can avoid clashes with
other software on the same machine. All host ports are defined once in
`infra/.env` (`*_HOST_PORT` variables) - change a value there and re-run
`docker compose up -d`. Never edit the Compose file or application source.

| Service | Host port | Env variable |
|---|---|---|
| Admin Dashboard (UI) | `33000` | `ADMIN_DASHBOARD_UI_HOST_PORT` |
| Admin Dashboard API | `38082` | `ADMIN_DASHBOARD_API_HOST_PORT` |
| Streamlit (chat) | `38501` | `STREAMLIT_HOST_PORT` |
| Atlassian MCP | `38090` | `ATLASSIAN_MCP_HOST_PORT` |
| assistant-service | `38081` | `ASSISTANT_SERVICE_HOST_PORT` |
| automation-service | `38084` | `AUTOMATION_SERVICE_HOST_PORT` |
| task-intake-service | `38083` | `TASK_INTAKE_HOST_PORT` |
| Postgres | `35432` | `POSTGRES_HOST_PORT` |
| Redis | `36379` | `REDIS_HOST_PORT` |
| Vault | `38200` | `VAULT_HOST_PORT` |
| Temporal (gRPC) | `37233` | `TEMPORAL_HOST_PORT` |
| Temporal UI | `38233` | `TEMPORAL_UI_HOST_PORT` |
| Firecrawl | `33002` | `FIRECRAWL_HOST_PORT` |
| MinIO (API / console) | `39000` / `39001` | `MINIO_HOST_PORT` / `MINIO_CONSOLE_HOST_PORT` |
| Traefik (HTTP / HTTPS) | `8044` / `8444` | - (edit Compose) |

For day-to-day use you only need **`33000`** (dashboard) and **`38501`**
(Streamlit chat).

## Common commands

| Command | Behavior |
|---|---|
| `make boot` | Bootstrap-only (default - postgres, vault, admin-dashboard). |
| `make up` | Alias for `make boot` (backward compatibility). |
| `make up-all` | Start every service at once (CI / debug). Not for production. |
| `make ps` | List running services. |
| `make logs` | Tail logs of the active services. |
| `make down` | Stop all containers (volumes preserved). |
| `make restart` | `make down` + `make boot`. |
| `make profiles` | Print the profile list derived from the manifest. |
| `make help` | Show all targets and their descriptions. |

> **Note on `make up-all`:** it starts all services at once (for CI tests and
> full-stack debugging). Do not use it on the first boot - launching 12
> services in parallel before credentials are entered produces
> "started in the wrong order" errors. Follow the `make boot` + Setup Wizard
> flow instead.

## Directory layout

| Path | Contents |
|---|---|
| `infra/` | Docker Compose files (`docker-compose.yml`, `docker-compose.dev.yml`) and Postgres migrations. |
| `services/` | HTTP services - `automation-service`, `assistant-service`, `admin-dashboard-api`, `atlassian-mcp` (gateway). |
| `workers/` | Temporal workers - `automation-worker`, `agent-runner-worker`, `execution-runner-worker`. |
| `ui/` | Front ends - `admin-dashboard` (Next.js), `streamlit-app` (Streamlit). |
| `libs/` | Shared Python libraries (vault_client, audit, llm_client, etc.). |
| `config/` | Manifest files - `services.manifest.json`, `departments.json` and their schemas. |
| `prompts/` | LLM prompts - task_creation_assistant, assistant_chat, notification templates. |
| `docs/` | Runbooks, user guide, env reference. |
| `tests/` | Cross-service property/integration tests (each service also has its own `tests/`). |
| `scripts/` | `up.sh`, `up.ps1` wrappers + maintenance scripts. |

## Architecture

The stack is a set of small services around a shared MCP gateway:

- **Atlassian MCP gateway** (`services/atlassian_mcp_bitbucket/`) - a stateless
  HTTP surface that exposes Jira, Confluence and Bitbucket tools. Credentials
  arrive per-request as `X-Atlassian-*` headers.
- **task-intake-service** - receives Jira webhooks and starts workflows.
- **automation-service** - governance APIs (departments, credentials, PR review,
  branch scans) used by the admin dashboard.
- **assistant-service** - chat/assist APIs backed by the MCP gateway and an LLM.
- **admin-dashboard** (Next.js + API) - the operator/governance UI.
- **streamlit-app** - the end-user UI (credentials, chat, task creation).
- **Temporal workers** (`workers/`) - durable execution for long-running
  automation and runner/SSH steps.

A typical automation flow:

1. A Jira issue is assigned to the bot - Jira fires a webhook to
   `task-intake-service`.
2. A Temporal workflow starts and asks the LLM what the task needs (code change,
   docs, a runner, etc.).
3. The workflow drives the MCP gateway and, when needed, a remote runner over
   SSH to make changes.
4. It opens a draft pull request on Bitbucket, publishes a Confluence page, and
   comments back on the Jira issue.

## Built on

- **Atlassian MCP gateway** - `services/atlassian_mcp_bitbucket/` builds the
  [`jellythomas/mcp-atlassian-with-bitbucket`](https://github.com/jellythomas/mcp-atlassian-with-bitbucket)
  fork at a pinned commit. It bundles Jira, Confluence and Bitbucket support
  together, so all three Atlassian products share one stateless HTTP MCP surface.
- **[Temporal](https://temporal.io/)** - durable workflow execution for the
  automation tier.
- **[HashiCorp Vault](https://www.vaultproject.io/)** - secret storage for
  department credentials and tokens.
- **[Firecrawl](https://www.firecrawl.dev/)** - web content retrieval used by
  research/automation steps.
- **[Next.js](https://nextjs.org/)** (admin dashboard) and
  **[Streamlit](https://streamlit.io/)** (chat front end) for the two UIs;
  **[MinIO](https://min.io/)** for artifact storage and
  **[Traefik](https://traefik.io/)** as the optional edge router.

## Next steps

- **First-boot flow:** [`docs/runbooks/getting-started.md`](docs/runbooks/getting-started.md).
- **Webhook setup:** [`docs/runbooks/webhook-setup.md`](docs/runbooks/webhook-setup.md).
- **Department decommission:** [`docs/runbooks/dept-decommission.md`](docs/runbooks/dept-decommission.md).
- **Environment variables:** [`docs/env-reference.md`](docs/env-reference.md).
- **Connect an IDE / MCP credential headers:** [`docs/api-contracts/mcp-credential-headers.md`](docs/api-contracts/mcp-credential-headers.md) - Cloud vs Server/DC auth, Bearer/PAT vs Basic, and the SSRF allowlist.
- **End-user task-creation guide:** [`docs/user-guide/`](docs/user-guide/).

## License

This repository is for private use. The bundled Atlassian MCP gateway is built
from an upstream fork and retains its original upstream license.