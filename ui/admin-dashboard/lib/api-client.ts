/**
 * Thin API client stub for the admin dashboard.
 *
 * Calls the admin-dashboard-api service via `NEXT_PUBLIC_ADMIN_API_BASE_URL`,
 * falling back to `http://localhost:8082` for local standalone runs.
 *
 * This is intentionally a minimal placeholder for the multi-service scaffold;
 * authentication, retries, and typed wrappers will be layered in later.
 *
 * Two call patterns:
 *   1. `await apiFetch(path, init?)` returns `Response` — caller is
 *      responsible for `res.ok` / `await res.json()`. This is the
 *      original behavior and is preserved byte-for-byte.
 *   2. `await apiFetch<T>(path, init?)` types the awaited value as
 *      `T`. The runtime call is identical to (1); the generic is a
 *      type-level convenience for call sites that already treat the
 *      result as a parsed body.
 */
export function apiFetch(
  path: string,
  init?: RequestInit,
): Promise<Response>;
export function apiFetch<T>(
  path: string,
  init?: RequestInit,
): Promise<T>;
export async function apiFetch<T = Response>(
  path: string,
  init?: RequestInit,
): Promise<T | Response> {
  const baseUrl = getAdminApiBaseUrl();

  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  const url = `${baseUrl}${normalizedPath}`;

  return fetch(url, {
    ...init,
    headers: getAdminAuthHeaders(init?.headers),
  });
}

export function getAdminApiBaseUrl(): string {
  return process.env.NEXT_PUBLIC_ADMIN_API_BASE_URL ?? "http://localhost:8082";
}

export function getAdminAuthHeaders(
  headers?: HeadersInit,
): HeadersInit {
  // In dev mode, send a dev bearer token for auth bypass.
  // The admin-dashboard-api with AUTH_MODE=dev accepts any non-empty token.
  const devToken = process.env.NEXT_PUBLIC_DEV_TOKEN ?? "dev-admin-token";
  const merged = new Headers(headers);
  if (!merged.has("Content-Type")) {
    merged.set("Content-Type", "application/json");
  }
  merged.set("Authorization", `Bearer ${devToken}`);
  return merged;
}
