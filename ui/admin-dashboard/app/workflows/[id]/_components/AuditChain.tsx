"use client";

/**
 * AuditChain - renders the audit event chain for a workflow.
 * Shows action, actor, actor_role, timestamp and payload_summary.
 */

import type { AuditChainRow } from "../page";

interface AuditChainProps {
  rows: AuditChainRow[];
}

export default function AuditChain({ rows }: AuditChainProps): JSX.Element {
  return (
    <section style={{ margin: "1.5rem 0" }}>
      <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "0.5rem" }}>
        Audit Chain
      </h2>
      {rows.length === 0 ? (
        <p style={{ color: "#9ca3af", fontSize: "0.875rem" }}>(no audit events)</p>
      ) : (
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.875rem" }}>
          <thead>
            <tr style={{ borderBottom: "2px solid #e5e7eb", textAlign: "left" }}>
              <th style={{ padding: "0.5rem" }}>Timestamp</th>
              <th style={{ padding: "0.5rem" }}>Action</th>
              <th style={{ padding: "0.5rem" }}>Actor</th>
              <th style={{ padding: "0.5rem" }}>Role</th>
              <th style={{ padding: "0.5rem" }}>Summary</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, idx) => (
              <tr key={idx} style={{ borderBottom: "1px solid #f3f4f6" }}>
                <td style={{ padding: "0.5rem", whiteSpace: "nowrap", color: "#6b7280" }}>
                  {new Date(row.timestamp).toLocaleString()}
                </td>
                <td style={{ padding: "0.5rem" }}>
                  <code style={{ fontSize: "0.8rem" }}>{row.action}</code>
                </td>
                <td style={{ padding: "0.5rem", fontSize: "0.8rem" }}>
                  {row.actor ?? "-"}
                </td>
                <td style={{ padding: "0.5rem", fontSize: "0.8rem" }}>
                  {row.actor_role ?? "-"}
                </td>
                <td style={{ padding: "0.5rem", color: "#6b7280", fontSize: "0.8rem" }}>
                  {row.payload_summary ?? ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
