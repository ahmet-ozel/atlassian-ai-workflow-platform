"use client";

/**
 * `/admin/llm-providers` - operator surface for the LLM provider
 * management spec (Requirements 14.1, 14.2, 14.4 - 14.8).
 * * Owns the `providers` state and re-fetches after every mutation/test
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
import { apiFetch } from "@/lib/api-client";
import type {
  ConnectionTestResult,
  ProviderRow,
} from "./_components/types";

interface InlineToast {
  kind: "info" | "success" | "error";
  message: string;
}

interface ModelUsageRow {
  model: string;
  usd: string;
  row_count: number;
}

interface ModelUsageResponse {
  window: string;
  by_model: ModelUsageRow[];
}

const MODEL_SERVICE_BINDINGS = [
  {
    service: "streamlit-ui",
    source: "LLM_PROVIDER + LLM_MODEL_NAME",
    notes: "Dashboard Start modalindan girilen OpenAI, vLLM veya Anthropic modeli.",
  },
  {
    service: "opencode-sidecar",
    source: "LLM_PROVIDER + LLM_MODEL_NAME",
    notes: "Kod uretim sidecar'i Start modalindan secilen provider ile calisir.",
  },
  {
    service: "agent-runner-worker",
    source: "LLM_PROVIDER + LLM_MODEL_NAME",
    notes: "Task analizi bu provider ile yapilir; opencode islemleri sidecar modelini kullanir.",
  },
  {
    service: "automation-worker",
    source: "LLM_PROVIDER + LLM_MODEL_NAME",
    notes: "Otomasyon task analizi icin provider secimi burada yonetilir.",
  },
  {
    service: "automation-service",
    source: "LLM_PROVIDER + LLM_MODEL_NAME",
    notes: "Otomasyon HTTP katmani modelsiz baslatilmaz.",
  },
  {
    service: "atlassian-mcp",
    source: "LLM kullanmaz",
    notes: "Jira, Confluence ve Bitbucket tool server; model cagrisi chat katmanindan gelir.",
  },
];

export default function LLMProvidersPage(): JSX.Element {
  const api = useProviderApi();
  const [rows, setRows] = useState<ProviderRow[]>([]);
  const [usageRows, setUsageRows] = useState<ModelUsageRow[]>([]);
  const [usageError, setUsageError] = useState<string | null>(null);
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

  useEffect(() => {
    let cancelled = false;
    async function loadUsage() {
      try {
        const response = await apiFetch("/admin/costs/model");
        if (!response.ok) {
          const text = await response.text().catch(() => "");
          throw new Error(
            `GET /admin/costs/model -> HTTP ${response.status}${
              text ? `: ${text.slice(0, 160)}` : ""
            }`,
          );
        }
        const payload = (await response.json()) as ModelUsageResponse;
        if (!cancelled) {
          setUsageRows(Array.isArray(payload.by_model) ? payload.by_model : []);
          setUsageError(null);
        }
      } catch (exc) {
        if (!cancelled) {
          setUsageRows([]);
          setUsageError(exc instanceof Error ? exc.message : String(exc));
        }
      }
    }
    void loadUsage();
    return () => {
      cancelled = true;
    };
  }, []);

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
          <h1 className="text-xl font-semibold">AI Modelleri</h1>
          <p className="text-sm text-gray-600">
            OpenAI, vLLM ve Anthropic provider kayitlari burada yonetilir.
            Kayit eklenmeden once model baglantisi test edilir; credential
            degerleri Vault'ta tutulur ve sadece maskeli gorunur.
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
          Provider ekle
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

      <section className="mb-4 grid gap-4 lg:grid-cols-2">
        <div className="rounded border border-gray-200 bg-white p-4">
          <h2 className="mb-2 text-sm font-semibold">Servis model kaynaklari</h2>
          <div className="overflow-x-auto">
            <table className="min-w-full text-sm">
              <thead className="text-left text-gray-500">
                <tr>
                  <th className="py-2 pr-3 font-medium">Servis</th>
                  <th className="py-2 pr-3 font-medium">Model kaynagi</th>
                  <th className="py-2 font-medium">Not</th>
                </tr>
              </thead>
              <tbody>
                {MODEL_SERVICE_BINDINGS.map((binding) => (
                  <tr key={binding.service} className="border-t border-gray-100">
                    <td className="py-2 pr-3 font-mono text-xs">{binding.service}</td>
                    <td className="py-2 pr-3 font-mono text-xs">{binding.source}</td>
                    <td className="py-2 text-gray-600">{binding.notes}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="rounded border border-gray-200 bg-white p-4">
          <h2 className="mb-2 text-sm font-semibold">Model istekleri (30 gun)</h2>
          {usageError ? (
            <p className="rounded bg-amber-50 px-3 py-2 text-xs text-amber-700">
              Kullanim sayaci okunamadi: {usageError}
            </p>
          ) : usageRows.length === 0 ? (
            <p className="text-sm text-gray-500">Kayitli model kullanimi yok.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="min-w-full text-sm">
                <thead className="text-left text-gray-500">
                  <tr>
                    <th className="py-2 pr-3 font-medium">Model</th>
                    <th className="py-2 pr-3 font-medium">Istek</th>
                    <th className="py-2 font-medium">Maliyet</th>
                  </tr>
                </thead>
                <tbody>
                  {usageRows.map((row) => (
                    <tr key={row.model} className="border-t border-gray-100">
                      <td className="py-2 pr-3 font-mono text-xs">{row.model}</td>
                      <td className="py-2 pr-3 tabular-nums">{row.row_count}</td>
                      <td className="py-2 tabular-nums">${row.usd}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>

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
