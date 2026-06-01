"use client";

/**
 * Provider list table (Requirements 4.4, 9.4, 9.5, 14.1).
 *
 * Columns: provider_type, name, model, context_length, status badge,
 * last_tested_at (relative time), and a per-row action menu (Test /
 * Edit / Disable / Delete).
 *
 * The table renders `api_key_masked` only — never the raw credential.
 * The parent page owns the data and the action handlers; the table is
 * a pure presentational component so the property-test surface stays
 * deterministic.
 */

import type { ProviderRow } from "./types";
import StatusBadge from "./StatusBadge";

interface ProviderTableProps {
  rows: ProviderRow[];
  onTest: (row: ProviderRow) => void;
  onEdit: (row: ProviderRow) => void;
  onDisable: (row: ProviderRow) => void;
  onDelete: (row: ProviderRow) => void;
}

export default function ProviderTable({
  rows,
  onTest,
  onEdit,
  onDisable,
  onDelete,
}: ProviderTableProps): JSX.Element {
  return (
    <table
      className="min-w-full border-separate border-spacing-y-1 text-sm"
      data-testid="llm-provider-table"
    >
      <thead className="text-left text-gray-600">
        <tr>
          <th className="px-3 py-2 font-medium">Type</th>
          <th className="px-3 py-2 font-medium">Name</th>
          <th className="px-3 py-2 font-medium">Model</th>
          <th className="px-3 py-2 font-medium">Context</th>
          <th className="px-3 py-2 font-medium">API key</th>
          <th className="px-3 py-2 font-medium">Status</th>
          <th className="px-3 py-2 font-medium">Last tested</th>
          <th className="px-3 py-2 font-medium text-right">Actions</th>
        </tr>
      </thead>
      <tbody>
        {rows.length === 0 ? (
          <tr>
            <td
              colSpan={8}
              className="px-3 py-6 text-center text-gray-400"
              data-testid="llm-provider-empty"
            >
              No providers configured yet — click <em>Add Provider</em>{" "}
              to create one.
            </td>
          </tr>
        ) : (
          rows.map((row) => (
            <tr
              key={row.id}
              className="rounded bg-white shadow-sm"
              data-testid="llm-provider-row"
              data-provider-id={row.id}
              data-provider-status={row.status}
            >
              <td className="px-3 py-2 font-mono uppercase text-xs text-gray-700">
                {row.provider_type}
              </td>
              <td className="px-3 py-2 font-medium">{row.name}</td>
              <td className="px-3 py-2 font-mono text-xs text-gray-700">
                {row.model}
              </td>
              <td className="px-3 py-2 text-gray-600 tabular-nums">
                {row.context_length.toLocaleString()}
              </td>
              <td
                className="px-3 py-2 font-mono text-xs text-gray-700"
                data-testid="llm-provider-api-key-masked"
              >
                {row.api_key_masked}
              </td>
              <td className="px-3 py-2">
                <StatusBadge row={row} />
              </td>
              <td className="px-3 py-2 text-gray-600 tabular-nums">
                {row.last_tested_at
                  ? formatRelative(row.last_tested_at)
                  : "—"}
              </td>
              <td className="px-3 py-2 text-right">
                <div className="inline-flex gap-2">
                  <button
                    type="button"
                    className="text-blue-600 hover:underline"
                    onClick={() => onTest(row)}
                  >
                    Test
                  </button>
                  <button
                    type="button"
                    className="text-blue-600 hover:underline"
                    onClick={() => onEdit(row)}
                  >
                    Edit
                  </button>
                  <button
                    type="button"
                    className="text-amber-600 hover:underline"
                    onClick={() => onDisable(row)}
                    disabled={row.status === "inactive"}
                  >
                    Disable
                  </button>
                  <button
                    type="button"
                    className="text-red-600 hover:underline"
                    onClick={() => onDelete(row)}
                  >
                    Delete
                  </button>
                </div>
              </td>
            </tr>
          ))
        )}
      </tbody>
    </table>
  );
}

/**
 * Lightweight relative-time formatter. Avoids pulling in a heavy
 * intl library — the table only needs a single-glance summary.
 */
function formatRelative(iso: string): string {
  const now = Date.now();
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const diff = Math.max(0, now - then);
  const seconds = Math.floor(diff / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}
