/**
 * StateBadge — Lifecycle state pill rendered next to a service row /
 * the service detail header.
 *
 * Implements platform-mimari-uyumluluk task 7.6 (R12 / Q14): renders the
 * full :class:`ServiceState` literal (six values) with a colour matching
 * design §3.10 plus the ``running_unmonitored`` extension.
 *
 * Colour scheme
 * -------------
 * - ``stopped``              → grey (#9ca3af)
 * - ``starting``             → blue (#3b82f6)
 * - ``running``              → green (#16a34a)
 * - ``unhealthy``            → yellow (#facc15)
 * - ``failed``               → red (#dc2626)
 * - ``running_unmonitored``  → grey (#cbd5e1) + tooltip (Requirement 12.3)
 *
 * The component is a *pure* presentational widget. Polling, refresh,
 * error handling and modal state remain in the parent page so that the
 * existing services catalog test fixtures keep working unchanged.
 */

import type { CSSProperties } from "react";

export type ServiceState =
  | "stopped"
  | "starting"
  | "running"
  | "unhealthy"
  | "failed"
  | "running_unmonitored";

const STATE_BADGE_STYLE: Record<ServiceState, CSSProperties> = {
  stopped: { background: "#9ca3af", color: "#1f2937" }, // grey
  starting: { background: "#3b82f6", color: "#ffffff" }, // blue
  running: { background: "#16a34a", color: "#ffffff" }, // green
  unhealthy: { background: "#facc15", color: "#1f2937" }, // yellow
  failed: { background: "#dc2626", color: "#ffffff" }, // red
  running_unmonitored: { background: "#cbd5e1", color: "#1f2937" }, // light grey
};

/**
 * Tooltip surfaced via the native ``title`` attribute. Only the
 * ``running_unmonitored`` state currently sets a non-empty tooltip
 * (Requirement 12.3 — design §3.x: "Compose healthcheck tanımlı değil
 * — monitorlanmıyor").
 */
const STATE_TOOLTIP: Partial<Record<ServiceState, string>> = {
  running_unmonitored:
    "Compose healthcheck tanımlı değil — monitorlanmıyor",
};

/**
 * Display label rendered inside the pill. Lifecycle states map 1:1 to
 * their wire literal except ``running_unmonitored`` which uses a
 * compact, human-readable rendering.
 */
const STATE_LABEL: Record<ServiceState, string> = {
  stopped: "stopped",
  starting: "starting",
  running: "running",
  unhealthy: "unhealthy",
  failed: "failed",
  running_unmonitored: "running (unmonitored)",
};

export type StateBadgeProps = {
  state: ServiceState;
};

export default function StateBadge({ state }: StateBadgeProps) {
  const style: CSSProperties = {
    display: "inline-block",
    padding: "0.15rem 0.5rem",
    borderRadius: "0.75rem",
    fontSize: "0.8rem",
    fontWeight: 600,
    textTransform: "uppercase",
    letterSpacing: "0.03em",
    whiteSpace: "nowrap",
    ...STATE_BADGE_STYLE[state],
  };
  const tooltip = STATE_TOOLTIP[state];
  return (
    <span
      style={style}
      title={tooltip}
      aria-label={tooltip ? `${STATE_LABEL[state]} — ${tooltip}` : STATE_LABEL[state]}
      data-state={state}
    >
      {STATE_LABEL[state]}
    </span>
  );
}
