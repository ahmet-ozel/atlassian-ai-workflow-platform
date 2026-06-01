/**
 * Capability matrix data shape + pure helpers — `platform-gap-fill`
 * task 9.2 (Requirement 10.6).
 *
 * The Admin Dashboard surfaces every ``(dept × service)`` connectivity
 * check the platform-gap-fill capability prober cares about
 * (Requirement 10.4 — Jira / Bitbucket / Confluence / LLM / SSH /
 * Docker). The corresponding API lives in
 * ``platform/services/admin-dashboard-api/src/routers/capabilities.py``
 * and was shipped under task 9.1.
 *
 * This module is **pure, framework-free ESM** so:
 *
 * * Next.js client components can import it directly
 *   (``app/capabilities/page.tsx`` consumes :func:`parseMatrix` /
 *   :func:`statusColor` / :data:`AUTO_REFRESH_INTERVAL_MS` /
 *   :data:`SUPPORTED_SERVICES`).
 * * Node's built-in ``node:test`` runner can validate the JSON
 *   shape contract without bringing in vitest / jest. The matching
 *   test sits next to this file at ``matrix.test.mjs``.
 *
 * The ``ProbeCell`` shape mirrors :class:`ProbeResult.to_response`
 * in ``capabilities.py`` exactly (``dept_id``, ``service``,
 * ``status``, ``error``, ``latency_ms``, ``probed_at``).
 *
 * Status values mirror the FE-friendly :data:`ProbeStatus` literal
 * type from the router (``healthy`` / ``unhealthy`` /
 * ``not_configured``). The router additionally surfaces ``unknown``
 * for cells that have never been probed yet but where the dept
 * config *does* declare the service — we treat that as "grey
 * pending" in the UI.
 */

/**
 * Service probe-cell status as surfaced to the UI.
 *
 * The Postgres column stores the canonical ``ok`` / ``error`` /
 * ``not_configured`` enum, but the router translates ``ok`` →
 * ``healthy`` and ``error`` → ``unhealthy`` before returning the
 * payload (see ``_STATUS_FROM_DB`` in ``capabilities.py``). The UI
 * additionally renders ``unknown`` for cells that have a configured
 * service but no cached probe row yet.
 *
 * @typedef {"healthy"|"unhealthy"|"not_configured"|"unknown"} ProbeStatus
 */

/**
 * Single probe-cell payload as returned under
 * ``departments[].services[<service>]`` in the matrix endpoint and
 * directly by the single-probe endpoint.
 *
 * @typedef {Object} ProbeCell
 * @property {string} dept_id
 * @property {string} service
 * @property {ProbeStatus} status
 * @property {string|null} error           Human-readable error from the
 *   most recent probe (eg. ``"connection_refused"``,
 *   ``"401 Unauthorized"``). ``null`` when the probe was healthy or
 *   never ran.
 * @property {number|null} latency_ms      Probe round-trip in
 *   milliseconds, or ``null`` when not measured.
 * @property {string|null} probed_at       ISO-8601 timestamp of the
 *   most recent probe attempt, or ``null`` when never probed.
 */

/**
 * One row of the matrix — a single department with its six probe
 * cells keyed by service name.
 *
 * @typedef {Object} DeptRow
 * @property {string} dept_id
 * @property {string|null|undefined} display_name
 * @property {Record<string, ProbeCell>} services
 */

/**
 * Top-level matrix payload returned by ``GET
 * /api/v1/departments/capabilities``.
 *
 * @typedef {Object} CapabilityMatrix
 * @property {DeptRow[]} departments
 * @property {string[]} supported_services
 */

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/**
 * The six service columns the matrix renders. Order is intentional:
 * it matches ``SUPPORTED_SERVICES`` in
 * :mod:`src.routers.capabilities` so the UI columns stay aligned
 * with the JSON the API returns.
 *
 * @type {readonly string[]}
 */
export const SUPPORTED_SERVICES = Object.freeze([
  "jira",
  "bitbucket",
  "confluence",
  "llm",
  "ssh",
  "docker",
]);

/**
 * Cell colour buckets (Requirement 10.6 — "yeşil/kırmızı/gri").
 *
 * The mapping deliberately collapses ``not_configured`` and
 * ``unknown`` onto the same grey colour: from an operator's
 * perspective both mean "nothing actionable here right now"
 * (either the dept config opts out of the service, or no probe has
 * landed yet).
 *
 * @type {Readonly<Record<ProbeStatus, "green"|"red"|"grey">>}
 */
export const STATUS_TO_COLOR = Object.freeze({
  healthy: "green",
  unhealthy: "red",
  not_configured: "grey",
  unknown: "grey",
});

/**
 * Auto-refresh interval — 10 minutes in milliseconds
 * (Requirement 10.6 — "10dk auto-refresh").
 *
 * Exposed as a named export so the test can pin the value and so
 * future tuning happens in one place.
 *
 * @type {number}
 */
export const AUTO_REFRESH_INTERVAL_MS = 10 * 60 * 1000;

/**
 * Human-readable Turkish labels for each status — surfaced in the
 * detail panel and as cell ``aria-label`` text. Keeping them in one
 * place lets the test assert the exact string contract.
 *
 * @type {Readonly<Record<ProbeStatus, string>>}
 */
export const STATUS_LABEL = Object.freeze({
  healthy: "Sağlıklı",
  unhealthy: "Hatalı",
  not_configured: "Tanımlı değil",
  unknown: "Bilinmiyor",
});

// ---------------------------------------------------------------------------
// Validation helpers
// ---------------------------------------------------------------------------

/** @type {ReadonlySet<ProbeStatus>} */
const _VALID_STATUSES = new Set([
  "healthy",
  "unhealthy",
  "not_configured",
  "unknown",
]);

/**
 * Parse + validate a raw payload from ``GET
 * /api/v1/departments/capabilities``.
 *
 * The function is intentionally strict on shape (so a backend bug
 * surfaces as a clear error instead of a silently empty UI) but
 * permissive on extra keys (so adding new fields server-side does
 * not break older clients). Specifically:
 *
 * * The top-level object MUST have ``departments`` (array) and
 *   ``supported_services`` (array of strings).
 * * Each department MUST have ``dept_id`` (non-empty string) and
 *   ``services`` (object mapping service-name → probe cell).
 * * Each probe cell MUST have ``dept_id``, ``service``, ``status``;
 *   ``error`` / ``latency_ms`` / ``probed_at`` may be missing or
 *   ``null``.
 * * Unknown status values raise — the UI cannot render a cell whose
 *   colour bucket is undefined.
 *
 * @param {unknown} raw
 * @returns {CapabilityMatrix}
 * @throws {Error} when the payload does not match the contract.
 */
export function parseMatrix(raw) {
  if (raw === null || typeof raw !== "object") {
    throw new Error("capability matrix payload must be an object");
  }

  const obj = /** @type {Record<string, unknown>} */ (raw);

  const supportedServices = obj.supported_services;
  if (!Array.isArray(supportedServices)) {
    throw new Error(
      "capability matrix payload missing 'supported_services' array",
    );
  }
  for (const svc of supportedServices) {
    if (typeof svc !== "string" || svc.length === 0) {
      throw new Error(
        "capability matrix 'supported_services' must contain non-empty strings",
      );
    }
  }

  const rawDepts = obj.departments;
  if (!Array.isArray(rawDepts)) {
    throw new Error(
      "capability matrix payload missing 'departments' array",
    );
  }

  /** @type {DeptRow[]} */
  const departments = [];
  for (const rawDept of rawDepts) {
    departments.push(_parseDept(rawDept, supportedServices));
  }

  return {
    departments,
    supported_services: /** @type {string[]} */ (supportedServices.slice()),
  };
}

/**
 * @param {unknown} raw
 * @param {readonly unknown[]} supportedServices
 * @returns {DeptRow}
 */
function _parseDept(raw, supportedServices) {
  if (raw === null || typeof raw !== "object") {
    throw new Error("department entry must be an object");
  }
  const dept = /** @type {Record<string, unknown>} */ (raw);

  const deptId = dept.dept_id;
  if (typeof deptId !== "string" || deptId.length === 0) {
    throw new Error("department entry missing non-empty 'dept_id'");
  }

  const displayName =
    typeof dept.display_name === "string" ? dept.display_name : null;

  const rawServices = dept.services;
  if (rawServices === null || typeof rawServices !== "object") {
    throw new Error(
      `department '${deptId}' missing 'services' object`,
    );
  }

  /** @type {Record<string, ProbeCell>} */
  const services = {};
  const servicesObj = /** @type {Record<string, unknown>} */ (rawServices);
  for (const svc of supportedServices) {
    if (typeof svc !== "string") continue;
    const rawCell = servicesObj[svc];
    if (rawCell === undefined) {
      throw new Error(
        `department '${deptId}' missing service cell for '${svc}'`,
      );
    }
    services[svc] = _parseCell(rawCell, deptId, svc);
  }

  return {
    dept_id: deptId,
    display_name: displayName,
    services,
  };
}

/**
 * Parse a single probe-cell payload (used both by :func:`parseMatrix`
 * for nested cells and by :func:`parseProbeCell` for the
 * single-probe endpoint).
 *
 * @param {unknown} raw
 * @param {string} expectedDeptId
 * @param {string} expectedService
 * @returns {ProbeCell}
 */
function _parseCell(raw, expectedDeptId, expectedService) {
  if (raw === null || typeof raw !== "object") {
    throw new Error(
      `probe cell for ${expectedDeptId}/${expectedService} must be an object`,
    );
  }
  const cell = /** @type {Record<string, unknown>} */ (raw);

  const deptId =
    typeof cell.dept_id === "string" && cell.dept_id.length > 0
      ? cell.dept_id
      : expectedDeptId;
  const service =
    typeof cell.service === "string" && cell.service.length > 0
      ? cell.service
      : expectedService;

  const status = cell.status;
  if (typeof status !== "string" || !_VALID_STATUSES.has(/** @type {ProbeStatus} */ (status))) {
    throw new Error(
      `probe cell ${deptId}/${service} has unknown status ` +
        JSON.stringify(status),
    );
  }

  const error = typeof cell.error === "string" ? cell.error : null;
  const latencyMs =
    typeof cell.latency_ms === "number" && Number.isFinite(cell.latency_ms)
      ? cell.latency_ms
      : null;
  const probedAt =
    typeof cell.probed_at === "string" && cell.probed_at.length > 0
      ? cell.probed_at
      : null;

  return {
    dept_id: deptId,
    service,
    status: /** @type {ProbeStatus} */ (status),
    error,
    latency_ms: latencyMs,
    probed_at: probedAt,
  };
}

/**
 * Parse the single-probe endpoint response (``POST
 * /api/v1/departments/{dept_id}/probe/{service}``). The response
 * is just a bare :class:`ProbeResult.to_response` payload.
 *
 * @param {unknown} raw
 * @param {string} expectedDeptId
 * @param {string} expectedService
 * @returns {ProbeCell}
 */
export function parseProbeCell(raw, expectedDeptId, expectedService) {
  return _parseCell(raw, expectedDeptId, expectedService);
}

// ---------------------------------------------------------------------------
// Presentation helpers
// ---------------------------------------------------------------------------

/**
 * Map a probe status to its colour bucket. Unknown / unexpected
 * values fall back to grey rather than throwing — the validator
 * already rejects unknown statuses; this helper treats grey as a
 * safe default for any future status the FE has not learned yet.
 *
 * @param {ProbeStatus|string} status
 * @returns {"green"|"red"|"grey"}
 */
export function statusColor(status) {
  // @ts-ignore — STATUS_TO_COLOR is keyed by ProbeStatus, fall back to grey.
  return STATUS_TO_COLOR[status] ?? "grey";
}

/**
 * Format a number of milliseconds for display in the matrix cell
 * (``"42ms"``) or detail panel (``"1.2s"``).
 *
 * @param {number|null|undefined} ms
 * @returns {string}
 */
export function formatLatency(ms) {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/**
 * Replace one cell inside an existing matrix and return a *new*
 * matrix object — used after the single-probe endpoint returns to
 * update the UI without re-fetching the whole grid.
 *
 * If the dept_id / service is not present in the matrix the input
 * matrix is returned verbatim (no surprise mutation).
 *
 * @param {CapabilityMatrix} matrix
 * @param {ProbeCell} updated
 * @returns {CapabilityMatrix}
 */
export function applyCellUpdate(matrix, updated) {
  let touched = false;
  const departments = matrix.departments.map((dept) => {
    if (dept.dept_id !== updated.dept_id) return dept;
    if (!(updated.service in dept.services)) return dept;
    touched = true;
    return {
      ...dept,
      services: {
        ...dept.services,
        [updated.service]: updated,
      },
    };
  });
  if (!touched) return matrix;
  return {
    ...matrix,
    departments,
  };
}
