/**
 * Centralized runtime configuration - single source of truth for every
 * external URL the admin dashboard talks to.
 * * Best-practice rationale
 * -----------------------
 * Ports / hostnames MUST NOT be hard-coded across components. They are
 * resolved here, once, from env vars injected by Compose / the deployment
 * ``.env``. To change a host port you edit ``infra/.env`` (or the Compose
 * ``environment:`` block) - never scattered call sites.
 * * ``NEXT_PUBLIC_*`` is the only env surface a Next.js browser bundle can
 * read. The admin API intentionally uses same-origin URLs because Next.js
 * rewrites proxy API paths to the internal service. Streamlit only needs the
 * public host port:
 * - ``NEXT_PUBLIC_STREAMLIT_HOST_PORT`` - Streamlit host port for same-host deploys.
 * - ``NEXT_PUBLIC_DEV_TOKEN``          - dev bearer token (AUTH_MODE=dev).
 */

/** Base URL of ``admin-dashboard-api``.
 *
 * Empty string intentionally means same-origin. The Next.js server rewrites
 * API paths to the Compose-internal admin-dashboard-api URL, so remote hosts
 * work without baking a machine-specific hostname into the browser bundle.
 */
export function getAdminApiBaseUrl(): string {
  return "";
}

/** Base URL of the end-user Streamlit app. */
export function getStreamlitUrl(): string {
  const hostPort = process.env.NEXT_PUBLIC_STREAMLIT_HOST_PORT?.trim();
  if (hostPort && typeof window !== "undefined") {
    return `${window.location.protocol}//${window.location.hostname}:${hostPort}`;
  }

  return "";
}

/** Dev bearer token used when ``AUTH_MODE=dev`` (any non-empty token works). */
export function getDevToken(): string {
  return process.env.NEXT_PUBLIC_DEV_TOKEN ?? "dev-admin-token";
}
