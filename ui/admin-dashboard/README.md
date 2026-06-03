# `admin-dashboard`

Next.js 14 (App Router) admin UI for the platform. It is the operator
control plane: bring services up/down, manage departments and credentials,
configure LLM providers, review workflows and PO requests, run live smoke
tests, and inspect costs / logs / audit trails.

All URL and port resolution lives in a single module, `lib/config.ts`
(env-driven, no hard-coded ports). `lib/api-client.ts` exposes `apiFetch`,
which talks to `admin-dashboard-api` via that config.

See the platform documentation for the broader architecture.

## Standalone build & run

This component can run on its own without the full Compose stack. The backing
API (`admin-dashboard-api`) is reached via `NEXT_PUBLIC_ADMIN_API_BASE_URL`;
when unset, `lib/config.ts` falls back to `http://localhost:38082` (dev only).

Prerequisites:

- Node.js `>=20` (matches `engines.node` in `package.json`)
- npm (or your package manager of choice)

### Local (without Docker)

From this directory (`ui/admin-dashboard/`):

```bash
npm install            # 1. install dependencies
npm run typecheck      # 2. type-check
npm run build          # 3. production build
npm run start          # 4. start the production server on :3000 (container port)

# Or, for iterative development with hot reload:
npm run dev
```

In the Compose stack the UI is published on the host at
**`http://localhost:33000`** (`ADMIN_DASHBOARD_UI_HOST_PORT`); standalone runs
listen on the container port `3000`.

### With Docker (standalone, no Compose)

```bash
docker build -t admin-dashboard:dev .
docker run --rm \
  --env-file .env \
  -p 33000:3000 \
  admin-dashboard:dev
```

## Layout

```
ui/admin-dashboard/
├── app/                        # App Router pages
│   ├── layout.tsx              # RootLayout + AppShell chrome
│   ├── page.tsx                # Home / Setup Wizard
│   ├── services/               # Service lifecycle + quick start
│   ├── workflows/              # Temporal workflow list + drill-down
│   ├── po-review/              # PO review of bot-opened draft PRs
│   ├── departments/            # Department CRUD + credentials
│   ├── capabilities/           # Capability matrix
│   ├── live-smoke/             # Live smoke tests
│   ├── llm-providers/          # LLM provider config
│   ├── prompts/ audit/ costs/ logs/ mcp-traffic/
│   ├── notifications/ feature-flags/ firecrawl/ security/ operations/
├── components/                 # Shared React components (AppShell, modals, …)
├── lib/
│   ├── config.ts               # single source of truth for URLs/ports (env-driven)
│   └── api-client.ts           # apiFetch(path, init)
├── package.json                # next ^14, react ^18, react-dom ^18
├── next.config.mjs             # minimal Next.js 14 config
├── tsconfig.json               # target ES2022, jsx: preserve, strict
└── README.md
```

## Environment variables

| Variable | Purpose | Default (dev) |
| --- | --- | --- |
| `NEXT_PUBLIC_ADMIN_API_BASE_URL` | Base URL of `admin-dashboard-api` | `http://localhost:38082` |
| `NEXT_PUBLIC_STREAMLIT_URL` | Base URL of the end-user Streamlit app | `http://localhost:38501` |
| `NEXT_PUBLIC_DEV_TOKEN` | Dev bearer token (when `AUTH_MODE=dev`) | `dev-admin-token` |
| `PORT` | Container port the Next.js server listens on | `3000` |
| `LOG_LEVEL` | Log verbosity | `INFO` |

> The dev fallbacks above only apply to `npm run dev` without a backing
> `.env`. In the Compose stack these `NEXT_PUBLIC_*` values come from
> `infra/.env` / the service `environment:` block. To change a port, edit the
> `*_HOST_PORT` variables in `infra/.env` — never the TypeScript source.
> This project uses `.env` files only; there is no `.env.example`.
