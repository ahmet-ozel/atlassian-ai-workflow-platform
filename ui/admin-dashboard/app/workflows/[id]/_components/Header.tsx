"use client";

/**
 * Header component for the workflow detail page.
 * Displays workflow_id, type, dept, status, started_at, duration and cost_usd.
 */

import type { WorkflowDetail } from "../page";

interface HeaderProps {
  detail: WorkflowDetail;
}

function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return "—";
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainingSeconds = seconds % 60;
  return `${minutes}m ${remainingSeconds}s`;
}

const STATUS_COLORS: Record<string, string> = {
  running: "#dbeafe",
  completed: "#dcfce7",
  failed: "#fee2e2",
  cancelled: "#f3f4f6",
  partial: "#fef9c3",
};

export default function Header({ detail }: HeaderProps): JSX.Element {
  const statusColor = STATUS_COLORS[detail.status ?? ""] ?? "#f3f4f6";

  return (
    <section style={{ margin: "1rem 0", padding: "1rem", border: "1px solid #e5e7eb", borderRadius: "8px" }}>
      <h1 style={{ fontSize: "1.25rem", marginBottom: "0.75rem" }}>
        Workflow Detail
      </h1>
      <dl style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "0.25rem 1rem" }}>
        <dt style={{ fontWeight: 600, color: "#6b7280" }}>ID</dt>
        <dd><code style={{ fontSize: "0.875rem" }}>{detail.workflow_id}</code></dd>

        <dt style={{ fontWeight: 600, color: "#6b7280" }}>Type</dt>
        <dd>{detail.workflow_type ?? "—"}</dd>

        <dt style={{ fontWeight: 600, color: "#6b7280" }}>Department</dt>
        <dd>{detail.dept_id ?? "—"}</dd>

        <dt style={{ fontWeight: 600, color: "#6b7280" }}>Status</dt>
        <dd>
          <span style={{
            padding: "2px 8px",
            borderRadius: "4px",
            background: statusColor,
            fontSize: "0.875rem",
          }}>
            {detail.status ?? "unknown"}
          </span>
        </dd>

        <dt style={{ fontWeight: 600, color: "#6b7280" }}>Started</dt>
        <dd>{detail.started_at ? new Date(detail.started_at).toLocaleString() : "—"}</dd>

        <dt style={{ fontWeight: 600, color: "#6b7280" }}>Duration</dt>
        <dd>{formatDuration(detail.duration_ms)}</dd>

        <dt style={{ fontWeight: 600, color: "#6b7280" }}>Cost (USD)</dt>
        <dd>{detail.cost_usd != null ? `$${detail.cost_usd}` : "—"}</dd>
      </dl>
    </section>
  );
}
