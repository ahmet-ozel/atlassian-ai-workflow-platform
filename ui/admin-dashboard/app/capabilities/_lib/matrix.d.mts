/**
 * Type declarations for the framework-free :file:`matrix.mjs`
 * runtime helpers. The implementation lives in plain ESM so Node's
 * built-in :mod:`node:test` runner can validate it without a
 * TypeScript compiler in the loop; this declaration file gives the
 * Next.js / TypeScript editor experience full type checking when
 * the React page imports the helpers.
 *
 * If you change the runtime contract in :file:`matrix.mjs` you MUST
 * mirror the change here — keep the typedef JSDoc in the .mjs and
 * the types here in lockstep.
 */

export type ProbeStatus =
  | "healthy"
  | "unhealthy"
  | "not_configured"
  | "unknown";

export interface ProbeCell {
  dept_id: string;
  service: string;
  status: ProbeStatus;
  /** Human-readable error from the most recent probe; null when the
   *  probe was healthy or never ran. */
  error: string | null;
  /** Probe round-trip in milliseconds; null when not measured. */
  latency_ms: number | null;
  /** ISO-8601 timestamp of the most recent probe; null when never
   *  probed. */
  probed_at: string | null;
}

export interface DeptRow {
  dept_id: string;
  display_name: string | null | undefined;
  services: Record<string, ProbeCell>;
}

export interface CapabilityMatrix {
  departments: DeptRow[];
  supported_services: string[];
}

export const SUPPORTED_SERVICES: readonly string[];

export const STATUS_TO_COLOR: Readonly<Record<ProbeStatus, "green" | "red" | "grey">>;

export const AUTO_REFRESH_INTERVAL_MS: number;

export const STATUS_LABEL: Readonly<Record<ProbeStatus, string>>;

/**
 * Validate + parse the raw payload from
 * ``GET /api/v1/departments/capabilities``. Throws when the
 * payload does not match the contract.
 */
export function parseMatrix(raw: unknown): CapabilityMatrix;

/**
 * Validate + parse the raw payload from
 * ``POST /api/v1/departments/{dept_id}/probe/{service}``. Falls
 * back to the expected dept_id / service when the body omits
 * those fields.
 */
export function parseProbeCell(
  raw: unknown,
  expectedDeptId: string,
  expectedService: string,
): ProbeCell;

/** Map a probe status to its colour bucket. Unknown values fall
 *  back to grey. */
export function statusColor(status: ProbeStatus | string): "green" | "red" | "grey";

/** Render latency for display: "42ms" / "1.2s" / "—". */
export function formatLatency(ms: number | null | undefined): string;

/**
 * Replace one cell inside an existing matrix and return a new
 * matrix object (immutable update). Returns the input matrix
 * verbatim when the dept_id / service is not present.
 */
export function applyCellUpdate(
  matrix: CapabilityMatrix,
  updated: ProbeCell,
): CapabilityMatrix;
