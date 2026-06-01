"use client";

/**
 * LlmUsageTable — renders LLM token/cost usage per activity.
 * Shows prompt_path, prompt_version, model, token_in, token_out, cost_usd.
 */

import type { LlmUsageRow } from "../page";

interface LlmUsageTableProps {
  rows: LlmUsageRow[];
}

export default function LlmUsageTable({ rows }: LlmUsageTableProps): JSX.Element {
  const totalCost = rows.reduce((sum, r) => {
    const c = parseFloat(r.cost_usd ?? "0");
    return sum + (isNaN(c) ? 0 : c);
  }, 0);

  const totalTokenIn = rows.reduce((sum, r) => sum + (r.token_in ?? 0), 0);
  const totalTokenOut = rows.reduce((sum, r) => sum + (r.token_out ?? 0), 0);

  return (
    <section style={{ margin: "1.5rem 0" }}>
      <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "0.5rem" }}>
        LLM Usage
      </h2>
      {rows.length === 0 ? (
        <p style={{ color: "#9ca3af", fontSize: "0.875rem" }}>(no LLM usage recorded)</p>
      ) : (
        <>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.875rem" }}>
            <thead>
              <tr style={{ borderBottom: "2px solid #e5e7eb", textAlign: "left" }}>
                <th style={{ padding: "0.5rem" }}>Activity</th>
                <th style={{ padding: "0.5rem" }}>Prompt</th>
                <th style={{ padding: "0.5rem" }}>Version</th>
                <th style={{ padding: "0.5rem" }}>Model</th>
                <th style={{ padding: "0.5rem", textAlign: "right" }}>Token In</th>
                <th style={{ padding: "0.5rem", textAlign: "right" }}>Token Out</th>
                <th style={{ padding: "0.5rem", textAlign: "right" }}>Cost (USD)</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, idx) => (
                <tr key={row.activity_id ?? idx} style={{ borderBottom: "1px solid #f3f4f6" }}>
                  <td style={{ padding: "0.5rem" }}>
                    <code style={{ fontSize: "0.8rem" }}>{row.activity_id}</code>
                  </td>
                  <td style={{ padding: "0.5rem", fontSize: "0.8rem" }}>
                    {row.prompt_path ?? "—"}
                  </td>
                  <td style={{ padding: "0.5rem" }}>{row.prompt_version ?? "—"}</td>
                  <td style={{ padding: "0.5rem" }}>{row.model ?? "—"}</td>
                  <td style={{ padding: "0.5rem", textAlign: "right" }}>
                    {row.token_in?.toLocaleString() ?? "—"}
                  </td>
                  <td style={{ padding: "0.5rem", textAlign: "right" }}>
                    {row.token_out?.toLocaleString() ?? "—"}
                  </td>
                  <td style={{ padding: "0.5rem", textAlign: "right" }}>
                    {row.cost_usd != null ? `$${row.cost_usd}` : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr style={{ borderTop: "2px solid #e5e7eb", fontWeight: 600 }}>
                <td colSpan={4} style={{ padding: "0.5rem" }}>Total</td>
                <td style={{ padding: "0.5rem", textAlign: "right" }}>
                  {totalTokenIn.toLocaleString()}
                </td>
                <td style={{ padding: "0.5rem", textAlign: "right" }}>
                  {totalTokenOut.toLocaleString()}
                </td>
                <td style={{ padding: "0.5rem", textAlign: "right" }}>
                  ${totalCost.toFixed(4)}
                </td>
              </tr>
            </tfoot>
          </table>
        </>
      )}
    </section>
  );
}
