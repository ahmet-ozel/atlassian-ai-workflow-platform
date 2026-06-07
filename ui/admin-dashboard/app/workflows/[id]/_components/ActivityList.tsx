"use client";

/**
 * ActivityList - renders each Temporal activity with collapsible input/output,
 * elapsed_ms, retry_count and status.
 */

import { useState } from "react";

interface Activity {
  activity_id?: string;
  activity_type?: string;
  status?: string;
  elapsed_ms?: number | null;
  retry_count?: number;
  input?: unknown;
  output?: unknown;
  error?: string | null;
  [key: string]: unknown;
}

interface ActivityListProps {
  activities: unknown[];
}

function CollapsibleJson({ label, value }: { label: string; value: unknown }): JSX.Element {
  const [open, setOpen] = useState(false);
  const text = value != null ? JSON.stringify(value, null, 2) : null;
  if (!text) return <span style={{ color: "#9ca3af" }}>{label}: -</span>;

  return (
    <span>
      <button
        onClick={() => setOpen((o) => !o)}
        style={{
          background: "none",
          border: "1px solid #d1d5db",
          borderRadius: "4px",
          cursor: "pointer",
          fontSize: "0.75rem",
          padding: "1px 6px",
        }}
      >
        {open ? "▲" : "▼"} {label}
      </button>
      {open && (
        <pre style={{
          background: "#f9fafb",
          border: "1px solid #e5e7eb",
          borderRadius: "4px",
          fontSize: "0.75rem",
          marginTop: "0.25rem",
          maxHeight: "200px",
          overflow: "auto",
          padding: "0.5rem",
          whiteSpace: "pre-wrap",
          wordBreak: "break-all",
        }}>
          {text}
        </pre>
      )}
    </span>
  );
}

const ACTIVITY_STATUS_COLORS: Record<string, string> = {
  completed: "#dcfce7",
  failed: "#fee2e2",
  running: "#dbeafe",
  scheduled: "#f3f4f6",
};

export default function ActivityList({ activities }: ActivityListProps): JSX.Element {
  const typedActivities = activities as Activity[];

  return (
    <section style={{ margin: "1.5rem 0" }}>
      <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "0.5rem" }}>
        Activities
      </h2>
      {typedActivities.length === 0 ? (
        <p style={{ color: "#9ca3af", fontSize: "0.875rem" }}>(no activities)</p>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.875rem" }}>
          <thead>
            <tr style={{ borderBottom: "2px solid #e5e7eb", textAlign: "left" }}>
              <th style={{ padding: "0.5rem" }}>Activity</th>
              <th style={{ padding: "0.5rem" }}>Status</th>
              <th style={{ padding: "0.5rem" }}>Elapsed</th>
              <th style={{ padding: "0.5rem" }}>Retries</th>
              <th style={{ padding: "0.5rem" }}>Input / Output</th>
            </tr>
          </thead>
          <tbody>
            {typedActivities.map((act, idx) => (
              <tr key={act.activity_id ?? idx} style={{ borderBottom: "1px solid #f3f4f6" }}>
                <td style={{ padding: "0.5rem" }}>
                  <code style={{ fontSize: "0.8rem" }}>{act.activity_type ?? act.activity_id ?? `#${idx + 1}`}</code>
                </td>
                <td style={{ padding: "0.5rem" }}>
                  <span style={{
                    padding: "2px 6px",
                    borderRadius: "4px",
                    background: ACTIVITY_STATUS_COLORS[act.status ?? ""] ?? "#f3f4f6",
                  }}>
                    {act.status ?? "-"}
                  </span>
                </td>
                <td style={{ padding: "0.5rem" }}>
                  {act.elapsed_ms != null ? `${act.elapsed_ms}ms` : "-"}
                </td>
                <td style={{ padding: "0.5rem" }}>
                  {act.retry_count ?? 0}
                </td>
                <td style={{ padding: "0.5rem", display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
                  <CollapsibleJson label="Input" value={act.input} />
                  <CollapsibleJson label="Output" value={act.output} />
                  {act.error && (
                    <span style={{ color: "#dc2626", fontSize: "0.75rem" }}>
                      Error: {act.error}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
