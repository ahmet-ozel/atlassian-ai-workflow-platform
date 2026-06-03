/**
 * Sensitive_Env_Key matcher — TypeScript half of the TS↔Python ikiz modül.
 *
 * This module is the **single source of truth** for which environment
 * variable keys count as a Sensitive_Env_Key on the browser/Next.js side
 * of the admin-dashboard control plane. The Python twin lives at
 * `services/admin-dashboard-api/src/lifecycle/sensitive.py` and uses the
 * **identical** regex source strings in the **identical** order so
 * TS/Python parity holds character-for-character.
 *
 * Sensitive_Env_Key definition:
 *
 *   Adı `*_TOKEN`, `*_KEY`, `*_SECRET`, `*_PASSWORD`, `*_DSN`,
 *   `*_CREDENTIAL`, `*_PRIVATE_*` örüntülerinden birine uyan ortam
 *   değişkeni anahtarı.
 *
 * A key is *sensitive* iff it ends with one of the suffixes `_TOKEN`,
 * `_KEY`, `_SECRET`, `_PASSWORD`, `_DSN`, `_CREDENTIAL`, **or** contains
 * the infix `_PRIVATE_`. Bare names like `TOKEN` (no leading underscore)
 * do not match — this mirrors the glob notation `*_TOKEN`.
 *
 * The module is intentionally **pure** (no side effects, no I/O) and
 * safe to import from React Server Components, client components and
 * Node test runners alike.
 */

/**
 * Regex patterns identifying a Sensitive_Env_Key, in the exact order
 * documented for the browser and API implementations.
 *
 * The source strings must remain **character-by-character identical**
 * to the Python literals in
 * `services/admin-dashboard-api/src/lifecycle/sensitive.py` so parity checks
 * C4 (TS↔Python parity) can compare both sides on the same input set.
 * can compare both sides on the same input set.
 * `RegExp.prototype.test` is used (no leading `^`), aligning with the
 * Python helper which uses `re.search` semantics so the suffix anchors
 * `$` and the infix pattern `_PRIVATE_` behave identically on the
 * upper-case, no-newline keys produced by the property strategy
 * `[A-Z][A-Z0-9_]{3,40}`.
 */
export const SENSITIVE_ENV_KEY_PATTERNS: readonly RegExp[] = [
  /_TOKEN$/,
  /_KEY$/,
  /_SECRET$/,
  /_PASSWORD$/,
  /_DSN$/,
  /_CREDENTIAL$/,
  /_PRIVATE_/,
];

/**
 * Return `true` iff `key` matches a {@link SENSITIVE_ENV_KEY_PATTERNS}
 * entry.
 *
 * The check is case-sensitive — environment variable conventions in
 * this codebase are uppercase-only — and uses `RegExp.prototype.test`,
 * so suffix anchors (`$`) bind to the end of the string and the infix
 * pattern (`_PRIVATE_`) matches anywhere inside.
 *
 * @example
 * ```ts
 * isSensitiveEnvKey("VAULT_TOKEN");      // true
 * isSensitiveEnvKey("API_KEY");          // true
 * isSensitiveEnvKey("DB_PRIVATE_HOST");  // true
 * isSensitiveEnvKey("TOKEN");            // false
 * isSensitiveEnvKey("LOG_LEVEL");        // false
 * ```
 */
export function isSensitiveEnvKey(key: string): boolean {
  return SENSITIVE_ENV_KEY_PATTERNS.some((pattern) => pattern.test(key));
}
