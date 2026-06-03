"use client";

/**
 * Pure status pill for an LLM provider row.
 *
 * Color rules:
 *
 * - **Green** when the provider has been tested AND the last test
 *   succeeded (`last_tested_at != null && last_test_error == null`).
 * - **Red** when the provider has been tested AND the last test failed
 *   (`last_tested_at != null && last_test_error != null`).
 * - **Grey** when the provider has never been tested
 *   (`last_tested_at == null`).
 *
 * The component renders nothing else — no labels, no tooltips, no
 * click handlers. Composition with the rest of the provider table is
 * the parent's job.
 */

import type { ProviderRow } from "./types";

interface StatusBadgeProps {
  row: Pick<ProviderRow, "last_tested_at" | "last_test_error">;
}

export default function StatusBadge({ row }: StatusBadgeProps): JSX.Element {
  const { last_tested_at, last_test_error } = row;
  let label: string;
  let className: string;
  let color: "green" | "red" | "grey";

  if (last_tested_at === null) {
    color = "grey";
    label = "Not tested";
    className =
      "inline-flex items-center rounded-full bg-gray-100 px-2 py-0.5 " +
      "text-xs font-medium text-gray-600";
  } else if (last_test_error === null) {
    color = "green";
    label = "Healthy";
    className =
      "inline-flex items-center rounded-full bg-green-100 px-2 py-0.5 " +
      "text-xs font-medium text-green-700";
  } else {
    color = "red";
    label = "Failing";
    className =
      "inline-flex items-center rounded-full bg-red-100 px-2 py-0.5 " +
      "text-xs font-medium text-red-700";
  }

  return (
    <span
      className={className}
      data-testid="llm-provider-status-badge"
      data-color={color}
    >
      {label}
    </span>
  );
}
