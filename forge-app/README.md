# AI Bot Task — Forge Add-On

Atlassian Forge skeleton that ships the **`AI Bot Task`** Jira issue type with
mandatory custom fields so end-users cannot create AI bot tasks with missing
metadata.

> **Status:** skeleton. The
> handler is a placeholder and the add-on is opt-in behind
> `FEATURE_FLAG_FORGE_ADDON_ENABLED` (default `false`). When the flag is
> off, the platform falls back to the plain Markdown template rendered by
> `prompts/task_creation_assistant.md`.

## What ships

| Module          | Purpose                                                         |
| --------------- | --------------------------------------------------------------- |
| `jira:issueType` | Custom issue type **`AI Bot Task`** (`hierarchyLevel: 0`).      |
| `jira:customField` × 5 | `AI Görev Tipi`, `Hedef Repo`, `Branch`, `Output Hedefi`, `Cleanup Policy`. |
| `function`      | `index.handler` (event entrypoint), `index.populateHedefRepo` (option resolver for `Hedef Repo`). |

The `Hedef Repo` dropdown options are department-specific and resolved at
runtime by `populateHedefRepo`; today it returns an empty list and is
populated by a follow-up task that wires it to the automation-service
department config.

## Quick start

```bash
# 1. Install the Forge CLI (one-time)
npm install -g @forge/cli

# 2. Authenticate with your Atlassian developer account
forge login

# 3. Register a fresh app id (writes back to manifest.yml > app.id)
forge register

# 4. Deploy to the development environment
forge deploy

# 5. Install the add-on on a target site
forge install --site <your-instance>.atlassian.net --product jira
```

For the full deployment runbook (including the `FEATURE_FLAG_FORGE_ADDON_ENABLED`
opt-in flag), see [`platform/docs/forge-app-deployment.md`](../docs/forge-app-deployment.md).

## Repository layout

```
platform/forge-app/
├── manifest.yml      Forge manifest: issue type, custom fields, function refs.
├── src/
│   └── index.js      Handler placeholder + Hedef Repo option resolver.
├── package.json      @forge/cli + @forge/api pins.
├── README.md         You are here.
└── .gitignore        node_modules/ + .forge/ build cache.
```

## Notes

- The add-on requests only `read:jira-work` and `write:jira-work` scopes.
  Wider scopes require a separate review and a manifest update.
- The five custom field names (`AI Görev Tipi`, `Hedef Repo`, `Branch`,
  `Output Hedefi`, `Cleanup Policy`) intentionally match the labels used by
  the rest of the platform and the Markdown task template; do not rename
  them without updating `prompts/task_creation_assistant.md` and the
  automation-service consumers in lockstep.
- Local development uses `forge tunnel`; production deploys go through the
  CI pipeline once it is wired up.
