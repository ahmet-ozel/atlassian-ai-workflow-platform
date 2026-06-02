/**
 * Centralized runtime configuration — single source of truth for every
 * external URL the admin dashboard talks to.
 *
 * Best-practice rationale
 * -----------------------
 * Ports / hostnames MUST NOT be hard-coded across components. They are
 * resolved here, once, from build-time public env vars
 * (``NEXT_PUBLIC_*``) injected by Compose / the deployment ``.env``. To
 * change a port you edit ``infra/.env`` (or the Compose ``environment:``
 * block) — never the TypeScript source.
 *
 * The fallback values below are DEV-ONLY conveniences for ``npm run dev``
 * without a backing ``.env``; in any real deployment the ``NEXT_PUBLIC_*``
 * variables are always set, so these literals are never reached.
 *
 * ``NEXT_PUBLIC_*`` is the only env surface a Next.js browser bundle can
 * read, so these are the canonical knobs:
 *   - ``NEXT_PUBLIC_ADMIN_API_BASE_URL`` — admin-dashboard-api base URL.
 *   - ``NEXT_PUBLIC_STREAMLIT_URL``      — end-user Streamlit base URL.
 *   - ``NEXT_PUBLIC_DEV_TOKEN``          — dev bearer token (AUTH_MODE=dev).
 */

/** Strip a single trailing slash so callers can safely append paths. */
function trimTrailingSlash(value: string): string {
  return value.replace(/\/$/, "");
}

/** Base URL of ``admin-dashboard-api`` (resolved from env, dev fallback). */
export function getAdminApiBaseUrl(): string {
  return trimTrailingSlash(
    process.env.NEXT_PUBLIC_ADMIN_API_BASE_URL ?? "http://localhost:38082",
  );
}

/** Base URL of the end-user Streamlit app (resolved from env, dev fallback). */
export function getStreamlitUrl(): string {
  return trimTrailingSlash(
    process.env.NEXT_PUBLIC_STREAMLIT_URL ?? "http://localhost:38501",
  );
}

/** Dev bearer token used when ``AUTH_MODE=dev`` (any non-empty token works). */
export function getDevToken(): string {
  return process.env.NEXT_PUBLIC_DEV_TOKEN ?? "dev-admin-token";
}
