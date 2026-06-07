# `@platform/web-shared`

Shared TypeScript helpers used by the web UIs in this monorepo
(`ui/admin-dashboard` today, more later). Currently exports deeplink
builders such as `workflowDeeplink(workflowId)`.

This package is part of the platform UI shared layer and is intentionally
a thin stub at this stage.

## Standalone build & run

This package has no runtime services to start; the "run" surface is
its compiled output, which other packages consume via TypeScript imports.

Prerequisites:

- Node.js `>=20` (matches `engines.node` in `package.json`)
- npm (or your package manager of choice)

From this directory (`libs/web-shared/`):

```bash
# 1. Install dev dependencies (only `typescript` is declared)
npm install

# 2. Type-check without emitting output
npm run typecheck

# 3. Build the package - emits `dist/` with `.js`, `.d.ts`, and source maps
npm run build

# 4. (Optional) Smoke-test the built output from a Node 20 REPL
node --input-type=module -e "import('./dist/index.js').then(m => console.log(m.workflowDeeplink('wf-123')))"
# expected output: /workflows/wf-123
```

## Layout

```
libs/web-shared/
├── package.json        # name: @platform/web-shared, type: module
├── tsconfig.json       # target ES2022, module ESNext, strict
├── src/
│   ├── index.ts        # re-exports from ./deeplink
│   └── deeplink.ts     # workflowDeeplink(workflowId) stub
└── README.md
```

## Public API (current package)

| Export | Signature | Notes |
| --- | --- | --- |
| `workflowDeeplink` | `(workflowId: string) => string` | Returns `/workflows/<encodedId>`; base URL is the caller's responsibility. |

Additional helpers (e.g. `taskDeeplink`, `runDeeplink`) will be added as the
admin dashboard grows.
