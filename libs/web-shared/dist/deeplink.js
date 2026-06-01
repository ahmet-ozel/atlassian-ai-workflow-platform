/**
 * Deep-link helpers for the admin dashboard and other web UIs.
 *
 * This module is part of the multi-service scaffold and currently exposes
 * stub builders only. Real URL conventions (base URL prefix, tenant scoping,
 * fragment routing) will be filled in once the admin-dashboard routes are
 * finalised. See MIMARI.md §2 for the target URL scheme.
 */
/**
 * Build a deep-link path for a workflow detail view.
 *
 * Returns a leading-slash, URL-safe path of the form `/workflows/<id>` so it
 * can be appended to any base URL by the caller (e.g. `NEXT_PUBLIC_ADMIN_API_BASE_URL`).
 *
 * @param workflowId - Stable workflow identifier (Temporal workflow id).
 * @returns A relative path string suitable for use in `<a href>` or `router.push`.
 */
export function workflowDeeplink(workflowId) {
    return `/workflows/${encodeURIComponent(workflowId)}`;
}
//# sourceMappingURL=deeplink.js.map