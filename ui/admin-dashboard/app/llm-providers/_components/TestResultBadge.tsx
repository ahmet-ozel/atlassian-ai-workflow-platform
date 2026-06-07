"use client";

/**
 * Inline badge for a `ConnectionTestResult` (R14.3, R14.5).
 * * Renders:
 * - A green check + `{latency_ms}ms` on `success === true`.
 * - A red cross + the redacted `error.message` on `success === false`.
 * * The component never reaches into raw credential material - the
 * backend has already projected the result through
 * `http_shared.redact_text` before returning the body, so the
 * `error.message` we render is safe to display verbatim.
 */

import type { ConnectionTestResult } from "./types";

interface TestResultBadgeProps {
  result: ConnectionTestResult;
}

export default function TestResultBadge({
  result,
}: TestResultBadgeProps): JSX.Element {
  if (result.success) {
    return (
      <span
        className={
          "inline-flex items-center gap-1 rounded-full bg-green-100 px-2 " +
          "py-0.5 text-xs font-medium text-green-700"
        }
        data-testid="llm-test-result-badge"
        data-success="true"
      >
        <span aria-hidden="true"></span>
        <span>{result.latency_ms}ms</span>
      </span>
    );
  }
  return (
    <span
      className={
        "inline-flex items-center gap-1 rounded-full bg-red-100 px-2 " +
        "py-0.5 text-xs font-medium text-red-700"
      }
      data-testid="llm-test-result-badge"
      data-success="false"
      title={result.error?.message ?? ""}
    >
      <span aria-hidden="true"></span>
      <span>{result.error?.message ?? "unknown error"}</span>
    </span>
  );
}
