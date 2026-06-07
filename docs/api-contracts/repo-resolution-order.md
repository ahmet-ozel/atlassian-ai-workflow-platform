# Repo Resolution Order - Canonical Contract

> **Audience:** End users writing tasks (so they know where to put the repo
> name), the LLM that runs `task_analysis.md` (so its prompt instructions
> match the runtime), and contributors editing
> `task_creation_assistant.md` or webhook handlers (so the doc, the prompt,
> and the code stay aligned).
>
> **Parity invariant:** `platform/tests/property/test_repo_resolution_doc.py`
> asserts the precedence list below stays in sync with the prompt
> instructions in `prompts/task_creation_assistant.md` (the user-facing
> guidance) and `workers/agent-runner-worker/prompts/task_analysis.md`
> (the LLM decision prompt).

---

## TL;DR

The bot resolves the target repo by walking these sources **in order** and
stopping at the first match:

| # | Source | Set by | Example |
|---|---|---|---|
| 1 | Jira custom field (department-configured) | Jira admin | Custom field "Bot Repo" = `payment-callbacks` |
| 2 | Jira label `repo:<name>` | Task reporter | Label `repo:payment-callbacks` |
| 3 | Description YAML front-matter `ai-bot.repo` | Task reporter | `repo: payment-callbacks` inside the `---` block |
| 4 | Description body explicit "Repo:" line | Task reporter | `Repo: payment-callbacks` in markdown body |
| 5 | Department single-repo fallback | `departments.json.repo_mappings` | dept has exactly one repo → auto-select |
| 6 | LLM inference + structured-choice ambiguity guard | `task_analysis.md` LLM | "callback retry" → asks the user A/B/C if multiple repos match |
| 7 | `needs_info` Jira comment | Bot | "🤖 Hangi repo üzerinde çalışılsın?" |

**Rule of thumb for users:** Put the repo name in **one** of the first
three places. Don't scatter it across multiple fields with different values
- the highest-priority source wins and others are ignored.

---

## Source-by-source contract

### 1. Jira custom field (highest priority)

**When configured:** Departments may opt-in to a "Bot Repo" custom field on
their Jira project. The field's display name is mapped to a tenant-local
field id by
`automation-service.automation_service.jira_field_resolver.JiraFieldResolver.resolve_field_id`,
and the value is read directly from the issue payload during webhook
handling.

**Pros:** Strict, machine-readable, autocomplete-friendly in Jira UI.
**Cons:** Requires Jira admin to add the field per project; not all
departments use it.

**When to use:** Departments with multiple repos and a strong governance
need ("the Jira ticket type forces this field to be filled in").

### 2. Jira label `repo:<name>`

**Format:** `repo:<repo-slug>` (case-insensitive). Multi-word repos use
hyphens - Jira labels strip whitespace.

**Examples:** `repo:payment-callbacks`, `repo:hr-portal`.

**Pros:** Visible in the Jira sidebar, easy to add/remove without editing
description.
**Cons:** No autocomplete; typo-prone. Multiple `repo:*` labels are
ambiguous and trigger source #6 (structured-choice).

### 3. Description YAML front-matter `ai-bot.repo`

The canonical task description format defined in
`prompts/task_creation_assistant.md` v2.0:

```yaml
---
ai-bot:
  workflow_type: code_change_with_test
  repo: payment-callbacks
  branch: develop
  ...
---

## Amaç
[task description]
```

**Pros:** Structured, machine-parseable, lives next to the rest of the
bot directives (workflow_type, output_actions, cleanup).
**Cons:** Users must know YAML syntax - the Task Creation Assistant
helps generate it, but a manually-typed task may have a malformed front
matter that the bot rejects with a `needs_info` comment.

### 4. Description body explicit `Repo:` line

When the user does not use the YAML front-matter, the LLM scans the
markdown body for a literal "Repo:" or "Repository:" line:

```markdown
## Teknik Bilgiler
- Workspace: company-payment
- Repo: payment-callbacks
- Branch: develop
```

**Pros:** Natural for users following the markdown templates in
`task_creation_assistant.md` §"Markdown Açıklama Şablonları".
**Cons:** LLM-driven heuristic - robust for the documented templates but
brittle if the user writes "I want to work on the payment-callbacks
repo" (the LLM may resolve it correctly, but at lower confidence,
risking source #6).

### 5. Department single-repo fallback

When the resolved department has exactly one entry in
`departments.json.repo_mappings`, the bot auto-selects it without asking.

**Configured in:** `platform/config/departments.json` per department:

```json
"repo_mappings": [
  {
    "bitbucket_workspace": "company-payment",
    "bitbucket_repo": "payment-callbacks",
    "jira_project_key": "PAY",
    "default_branch": "develop"
  }
]
```

**Pros:** Zero-friction for small departments - the user does not type
the repo name at all.
**Cons:** Silently broken when a second repo is added later; the bot
suddenly starts asking via source #6 even though existing tasks worked
without it.

**Mitigation:** When you add a second repo, update the dept's existing
in-flight tasks' descriptions or labels to the explicit form (sources
#2-4).

### 6. LLM inference + structured-choice guard

When sources #1-5 do not yield a unique answer, the LLM running
`task_analysis.md` (in `workers/agent-runner-worker/prompts/`) tries to
infer the repo from free-form description text:

- **Single-repo confidence:** Substring match on the dept's
  `repo_mappings`. If exactly one repo's slug appears in the description,
  the LLM picks it.
- **Multi-match:** When two or more repo slugs match (substring or
  fuzzy similarity > 0.7), the LLM **does not guess**. Instead it returns
  `confidence: low` and a `needs_info_question` shaped as a
  structured choice:

  ```
  🤖 "callback retry" ifadesi birden fazla repo'ya uyabilir.
     Hangisinde çalışayım?

     A) `payment-callbacks` - callback gönderme servisi
     B) `callback-gateway`  - callback router/proxy
     C) `callback-router`   - eski legacy callback dispatcher

     Yanıt olarak `[A]`, `[B]` veya `[C]` yazın
     (veya repo adını tam olarak tekrar yazın).
  ```

  The Jira webhook handler treats the user's `[A]` reply as a
  workflow signal and resumes execution.

### 7. `needs_info` fallback

When sources #1-6 all fail (no field, no label, no front-matter, no body
mention, multi-repo dept, LLM cannot infer), the bot posts a
`needs_info` comment to the Jira issue and waits up to 7 days for a
reply (the same waiting protocol as any other missing-info loop -
3-iteration cap, then `out_of_scope`).

---

## Conflicts and tie-breakers

When **multiple sources are populated**, the lower-numbered source wins
unconditionally:

| Sources populated | Resolved repo | Why |
|---|---|---|
| #1 (`Bot Repo` field) = `repo-a` AND #2 (label `repo:repo-b`) | `repo-a` | Custom field beats label |
| #2 (`repo:repo-a`) AND #3 (front-matter `repo: repo-b`) | `repo-a` | Label beats description |
| #3 (front-matter `repo-a`) AND #4 (body `Repo: repo-b`) | `repo-a` | Front-matter beats body text |
| #4 (body `Repo: repo-a`) AND #5 (single-repo dept = `repo-b`) | `repo-a` | Explicit beats fallback |

**Rationale:** Higher-priority sources are stricter / more deliberate
("the user explicitly typed it in a structured place"). Lower-priority
sources are weaker signals ("we're guessing from context"). Treating
conflicts as a hard error would force users to clean up multiple
fields after every revision; treating them as silent merges would lose
the user's stated intent. The "highest wins" rule keeps user intent
authoritative.

**Audit trail:** When sources #1-4 differ, the workflow audit row
records the resolved source and the dropped values
(`audit.action="repo_resolved"`, `payload.source="custom_field"`,
`payload.dropped={"label": "repo-b", "front_matter": "repo-c"}`). Operators
can spot accidental conflicts after the fact in the costs / audit panel.

---

## Cross-references

- **User-facing prompt:** `platform/prompts/task_creation_assistant.md`
  v2.0 - §"WORKFLOW TYPE SEÇİM REHBERİ" / "Repo / Workspace / Branch
  otomatik türetme" mirrors the precedence list above.
- **LLM decision prompt:** `platform/workers/agent-runner-worker/prompts/task_analysis.md`
  - §"Target Repository Selection" enforces sources #5 (single-repo
  fallback) and #6 (LLM inference + structured choice).
- **Field resolver:** `platform/services/automation-service/src/automation_service/jira_field_resolver.py`
  - translates the dept's "Bot Repo" custom field display name to a
  tenant-local field id at startup.
- **Department config schema:** `platform/config/departments.schema.json`
  - `repo_mappings` definition (source #5).

---

## Migration notes

### Adding a new precedence source

1. Edit this doc first - bump the table to N+1 rows, document the new
   source's contract, conflict rules, audit shape.
2. Update `prompts/task_creation_assistant.md` and
   `agent-runner-worker/prompts/task_analysis.md` so the user-facing
   prompt and the LLM decision prompt agree.
3. Add the parser path in the webhook handler (or wherever resolution
   runs).
4. Extend the property test
   `platform/tests/property/test_repo_resolution_doc.py` so the parity
   check covers the new source.

### Removing or renaming a source

Same order: doc first, prompts next, code last. The property test
fails when the doc and the prompts disagree, which is the canary that
catches a partial rename.
