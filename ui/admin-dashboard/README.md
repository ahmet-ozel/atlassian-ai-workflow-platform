# `admin-dashboard`

Next.js 14 (App Router) admin UI for the multi-service scaffold.
This package is intentionally a thin scaffold at this stage — the
nine pages under `app/` (`services`, `workflows`, `departments`,
`prompts`, `audit`, `costs`, `notifications`, `security`,
`feature-flags`) are placeholders, and `lib/api-client.ts` exposes
a single `apiFetch` stub that talks to `admin-dashboard-api`.

See `MIMARI.md` §2 and `.kiro/specs/multi-service-scaffold/design.md`
§3.3 for the broader architecture.

## Standalone build & run

This Component is designed to be runnable on its own without the
full Compose stack (per Requirement 15). The backing API
(`admin-dashboard-api`) is reachable via
`NEXT_PUBLIC_ADMIN_API_BASE_URL`; if unset, `apiFetch` falls back to
`http://localhost:8082`.

Prerequisites:

- Node.js `>=20` (matches `engines.node` in `package.json`)
- npm (or your package manager of choice)

### Local (without Docker)

From this directory (`ui/admin-dashboard/`):

```bash
# 1. Install dependencies
npm install

# 2. Type-check
npm run typecheck

# 3. Production build
npm run build

# 4. Start the production server on :3000
npm run start

# Or, for iterative development with hot reload:
npm run dev
```

Then open http://localhost:3000 — you should see the
`Admin Dashboard — scaffold` placeholder.

### With Docker (standalone, no Compose)

A multi-stage `Dockerfile` will be added in task 6.3. Once present,
this Component can be built and run in isolation:

```bash
# From this directory
docker build -t admin-dashboard:dev .
docker run --rm \
  --env-file .env \
  -p 3000:3000 \
  admin-dashboard:dev
```

## Layout

```
ui/admin-dashboard/
├── app/
│   ├── layout.tsx              # RootLayout (html/body wrapper)
│   ├── page.tsx                # "Admin Dashboard — scaffold"
│   ├── services/page.tsx
│   ├── workflows/page.tsx
│   ├── departments/page.tsx
│   ├── prompts/page.tsx
│   ├── audit/page.tsx
│   ├── costs/page.tsx
│   ├── notifications/page.tsx
│   ├── security/page.tsx
│   └── feature-flags/page.tsx
├── components/.gitkeep         # reserved for shared React components
├── lib/
│   └── api-client.ts           # apiFetch(path, init) stub
├── package.json                # next ^14, react ^18, react-dom ^18
├── next.config.mjs             # minimal Next.js 14 config
├── tsconfig.json               # target ES2022, jsx: preserve, strict
└── README.md
```

## Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `NEXT_PUBLIC_ADMIN_API_BASE_URL` | Base URL of `admin-dashboard-api` | `http://localhost:8082` |
| `PORT` | Port the Next.js server listens on | `3000` |
| `LOG_LEVEL` | Log verbosity | `INFO` |

A component-local `.env.example` will be added in task 7.3.
