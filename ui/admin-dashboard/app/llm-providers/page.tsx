"use client";

/**
 * `/admin/llm-providers` — operator surface for the LLM provider
 * management spec (Requirements 14.1, 14.2, 14.4 — 14.8).
 *
 * Owns the `providers` state and re-fetches after every mutation/test
 * so the table stays in sync with the backend. Composes
 * `<ProviderTable>`, `<ProviderModal>` and `<DeleteConfirm>` into a
 * single CRUD screen; the inline test action calls the saved-test
 * endpoint and surfaces the result via a small toast at the top of
 * the table.
 */

import { useCallback, useEffect, useState } from "react";

import DeleteConfirm from "./_components/DeleteConfirm";
import ProviderModal from "./_components/ProviderModal";
import ProviderTable from "./_components/ProviderTable";
import TestResultBadge from "./_components/TestResultBadge";
import { ApiError, useProviderApi } from "./_components/useProviderApi";
import type {
  ConnectionTestResult,
  ProviderRow,
} from "./_components/types";

interface InlineToast {
  kind: "info" | "success" | "error";
  message: string;
}

export default function LLMProvidersPage(): JSX.Element {
  const api = useProviderApi();
  const [rows, setRows] = useState<ProviderRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [toast, setToast] = useState<InlineToast | null>(null);
  const [testResult, setTestResult] = useState<
    { row: ProviderRow; result: ConnectionTestResult } | null
  >(null);

  const [modalRow, setModalRow] = useState<
    { mode: "create" } | { mode: "edit"; row: ProviderRow } | null
  >(null);
  const [deletingRow, setDeletingRow] = useState<ProviderRow | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setToast(null);
    try {
      const data = await api.list();
      setRows(data);
    } catch (exc) {
      setToast({ kind: "error", message: formatError(exc) });
    } finally {
      setLoading(false);
    }
  }, [api]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleTest = async (row: ProviderRow) => {
    setToast(null);
    try {
      const result = await api.testSaved(row.id);
      setTestResult({ row, result });
      // Refresh so the row's `last_tested_at` / `last_test_error`
      // badge moves in lock-step with the test result.
      await refresh();
    } catch (exc) {
      setToast({ kind: "error", message: formatError(exc) });
    }
  };

  const handleDisable = async (row: ProviderRow) => {
    setToast(null);
    try {
      await api.disable(row.id);
      setToast({
        kind: "info",
        message: `Provider “${row.name}” marked as inactive.`,
      });
      await refresh();
    } catch (exc) {
      setToast({ kind: "error", message: formatError(exc) });
    }
  };

  return (
    <div className="p-6">
      <header className="mb-4 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">LLM Providers</h1>
          <p className="text-sm text-gray-600">
            Manage the LLM providers that the automation workflows can
            dispatch to. Credentials live in Vault — only the masked
            last-4 characters are surfaced here.
          </p>
        </div>
        <button
          type="button"
          className={
            "rounded bg-blue-600 px-3 py-1 text-sm text-white " +
            "hover:bg-blue-700"
          }
          onClick={() => setModalRow({ mode: "create" })}
          data-testid="llm-provider-add-button"
        >
          Add Provider
        </button>
      </header>

      {toast ? (
        <p
          role="status"
          className={
            "mb-3 rounded px-3 py-2 text-sm " +
            (toast.kind === "error"
              ? "bg-red-50 text-red-700"
              : toast.kind === "success"
              ? "bg-green-50 text-green-700"
              : "bg-blue-50 text-blue-700")
          }
          data-testid="llm-provider-page-toast"
        >
          {toast.message}
        </p>
      ) : null}

      {testResult ? (
        <p
          className="mb-3 flex items-center gap-2 text-sm"
          data-testid="llm-provider-test-toast"
        >
          <span>Test result for “{testResult.row.name}”:</span>
          <TestResultBadge result={testResult.result} />
          <button
            type="button"
            className="ml-2 text-xs text-gray-500 hover:underline"
            onClick={() => setTestResult(null)}
          >
            Dismiss
          </button>
        </p>
      ) : null}

      {loading ? (
        <p
          className="text-sm text-gray-500"
          data-testid="llm-provider-loading"
        >
          Loading…
        </p>
      ) : (
        <ProviderTable
          rows={rows}
          onTest={handleTest}
          onEdit={(row) => setModalRow({ mode: "edit", row })}
          onDisable={handleDisable}
          onDelete={(row) => setDeletingRow(row)}
        />
      )}

      {modalRow ? (
        <ProviderModal
          initial={modalRow.mode === "edit" ? modalRow.row : undefined}
          onClose={() => setModalRow(null)}
          onSaved={() => {
            void refresh();
          }}
        />
      ) : null}

      {deletingRow ? (
        <DeleteConfirm
          row={deletingRow}
          onClose={() => setDeletingRow(null)}
          onDeleted={() => {
            void refresh();
          }}
        />
      ) : null}
    </div>
  );
}

function formatError(exc: unknown): string {
  if (exc instanceof ApiError) {
    const body = exc.body as { error?: string } | null;
    if (body && typeof body === "object" && body.error) {
      return `${body.error} (HTTP ${exc.status})`;
    }
    return `Request failed (HTTP ${exc.status})`;
  }
  if (exc instanceof Error) return exc.message;
  return String(exc);
}
