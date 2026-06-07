/**
 * Thin API client for the admin dashboard.
 * * All URL / port resolution is centralized in ``lib/config.ts`` (single
 * source of truth, env-driven). This module only composes requests; it
 * does not hard-code any host or port.
 * * Two call patterns:
 * 1. `await apiFetch(path, init?)` returns `Response` - caller is
 * responsible for `res.ok` / `await res.json()`.
 * 2. `await apiFetch<T>(path, init?)` types the awaited value as `T`.
 * The runtime call is identical to (1); the generic is a type-level
 * convenience for call sites that already treat the result as a body.
 */
import { getAdminApiBaseUrl, getDevToken } from "@/lib/config";

export { getAdminApiBaseUrl } from "@/lib/config";

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

export function getAdminAuthHeaders(
  headers?: HeadersInit,
): HeadersInit {
  // In dev mode, send a dev bearer token for auth bypass.
  // The admin-dashboard-api with AUTH_MODE=dev accepts any non-empty token.
  const merged = new Headers(headers);
  if (!merged.has("Content-Type")) {
    merged.set("Content-Type", "application/json");
  }
  merged.set("Authorization", `Bearer ${getDevToken()}`);
  return merged;
}
