"use client";

/**
 * WorkspacesTab - Services panel'inin alt sekmesi.
 *
 * SSH üzerindeki ``$RUNNER_BASE_PATH`` altındaki task workspace'lerini
 * tablo halinde listeler ve admin'in tek tıkla silmesini sağlar. UI
 * sözleşmesi ``GET /admin/runner/workspaces`` ve
 * ``DELETE /admin/runner/workspaces/{issue_key}`` endpoint'lerini
 * (``runner_workspaces`` router) tüketir.
 *
 * Akış:
 * 1. Mount edildiğinde liste fetch edilir; "Refresh" butonu manuel
 *    yeniden yüklemeye izin verir.
 * 2. Her satırın ``[Sil]`` butonu confirm modal açar - onay üzerine
 *    DELETE çağrılır, başarılıysa satır listeden çıkar.
 * 3. 400 ``invalid_issue_key_format`` (path-traversal red) UI'a düşmez -
 *    backend regex'ine zaten yalnızca regex uyan key'ler liste hâlinde
 *    geliyor; yine de defansif olarak hata banner'ı gösterilir.
 *
 * Sayfaya ``app/services/page.tsx`` `Workspaces` sekmesini açtığında
 * bu komponenti render eder. Şimdilik komponent kendisi sekme
 * mekanizmasına bağlanmaz; ``page.tsx`` UI tarafı services
 * panelinde sub-tab) çerçevesinde komponenti monte eder.
 */

import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api-client";

// --------------------------------------------------------------------------
// Types - backend response shape (src/routers/runner_workspaces.py)
// --------------------------------------------------------------------------

type Workspace = {
  issue_key: string;
  size_mb: number;
  last_modified: string; // ISO-8601
};

type ListResponse = {
  workspaces: Workspace[];
};

type ListState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ok"; rows: Workspace[]; lastRefreshed: Date }
  | { kind: "error"; message: string };

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function formatSize(sizeMb: number): string {
  if (!Number.isFinite(sizeMb) || sizeMb < 0) return "-";
  if (sizeMb < 1) return "<1 MB";
  if (sizeMb < 1024) return `${sizeMb} MB`;
  return `${(sizeMb / 1024).toFixed(2)} GB`;
}

// --------------------------------------------------------------------------
// Cell styles (mirror ServicesPage)
// --------------------------------------------------------------------------

const cellStyle: React.CSSProperties = {
  padding: "0.5rem 0.75rem",
  borderBottom: "1px solid #e5e7eb",
  verticalAlign: "middle",
};

const headerCellStyle: React.CSSProperties = {
  ...cellStyle,
  textAlign: "left",
  background: "#f9fafb",
  fontWeight: 600,
  borderBottom: "2px solid #d1d5db",
};

// --------------------------------------------------------------------------
// Confirm dialog (inline - no shadcn/portal dependency yet)
// --------------------------------------------------------------------------

type ConfirmState =
  | { kind: "idle" }
  | { kind: "asking"; issueKey: string }
  | { kind: "deleting"; issueKey: string };

type ConfirmDialogProps = {
  issueKey: string;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
};

function ConfirmDialog({
  issueKey,
  busy,
  onCancel,
  onConfirm,
}: ConfirmDialogProps) {
  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="workspaces-confirm-title"
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(15, 23, 42, 0.45)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 50,
      }}
    >
      <div
        style={{
          background: "#ffffff",
          padding: "1.25rem 1.5rem",
          borderRadius: "0.5rem",
          width: "min(420px, 90vw)",
          boxShadow: "0 10px 30px rgba(0,0,0,0.18)",
        }}
      >
        <h2
          id="workspaces-confirm-title"
          style={{ margin: 0, fontSize: "1rem" }}
        >
          Workspace silinsin mi?
        </h2>
        <p
          style={{
            margin: "0.6rem 0 1rem",
            color: "#374151",
            fontSize: "0.9rem",
          }}
        >
          <code>{issueKey}</code> için <code>$RUNNER_BASE_PATH/{issueKey}/</code>
          {" "}klasörü ve etiketli Docker container&rsquo;ları kaldırılacak.
          Bu işlem geri alınamaz.
        </p>
        <div
          style={{
            display: "flex",
            gap: "0.5rem",
            justifyContent: "flex-end",
          }}
        >
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            style={{
              padding: "0.4rem 0.85rem",
              border: "1px solid #d1d5db",
              borderRadius: "0.25rem",
              background: "#ffffff",
              cursor: busy ? "not-allowed" : "pointer",
            }}
          >
            Vazgeç
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            style={{
              padding: "0.4rem 0.85rem",
              border: "1px solid #b91c1c",
              borderRadius: "0.25rem",
              background: busy ? "#fca5a5" : "#dc2626",
              color: "#ffffff",
              cursor: busy ? "not-allowed" : "pointer",
            }}
          >
            {busy ? "Siliniyor…" : "Sil"}
          </button>
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Main component
// --------------------------------------------------------------------------

export default function WorkspacesTab() {
  const [state, setState] = useState<ListState>({ kind: "idle" });
  const [confirm, setConfirm] = useState<ConfirmState>({ kind: "idle" });
  const [actionError, setActionError] = useState<string | null>(null);

  const fetchOnce = useCallback(async () => {
    setState((prev) =>
      prev.kind === "ok" ? prev : { kind: "loading" },
    );
    try {
      const res = await apiFetch("/admin/runner/workspaces");
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        setState({
          kind: "error",
          message: `GET /admin/runner/workspaces → HTTP ${res.status}${
            text ? `: ${text.slice(0, 200)}` : ""
          }`,
        });
        return;
      }
      const body = (await res.json()) as ListResponse;
      setState({
        kind: "ok",
        rows: Array.isArray(body.workspaces) ? body.workspaces : [],
        lastRefreshed: new Date(),
      });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setState({ kind: "error", message });
    }
  }, []);

  useEffect(() => {
    void fetchOnce();
  }, [fetchOnce]);

  const handleAskConfirm = useCallback((issueKey: string) => {
    setActionError(null);
    setConfirm({ kind: "asking", issueKey });
  }, []);

  const handleCancelConfirm = useCallback(() => {
    setConfirm({ kind: "idle" });
  }, []);

  const handleConfirmDelete = useCallback(async () => {
    if (confirm.kind !== "asking") return;
    const issueKey = confirm.issueKey;
    setConfirm({ kind: "deleting", issueKey });
    try {
      const res = await apiFetch(
        `/admin/runner/workspaces/${encodeURIComponent(issueKey)}`,
        { method: "DELETE" },
      );
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        // 400 invalid_issue_key_format should not happen in practice
        // (backend lists only regex-valid keys), but surface anything
        // non-2xx as an error banner so an out-of-band SSH-side
        // change is visible.
        setActionError(
          `DELETE /admin/runner/workspaces/${issueKey} → HTTP ${res.status}${
            text ? `: ${text.slice(0, 200)}` : ""
          }`,
        );
        setConfirm({ kind: "idle" });
        return;
      }
      // Success - drop the row locally so the table updates without
      // a full refetch round-trip; keep the data fresh by triggering
      // a refresh in the background.
      setState((prev) => {
        if (prev.kind !== "ok") return prev;
        return {
          ...prev,
          rows: prev.rows.filter((row) => row.issue_key !== issueKey),
        };
      });
      setConfirm({ kind: "idle" });
      void fetchOnce();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setActionError(message);
      setConfirm({ kind: "idle" });
    }
  }, [confirm, fetchOnce]);

  const lastRefreshedLabel =
    state.kind === "ok"
      ? formatTimestamp(state.lastRefreshed.toISOString())
      : "-";

  return (
    <section style={{ marginTop: "1rem" }}>
      <header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: "0.75rem",
          gap: "1rem",
        }}
      >
        <div>
          <h2 style={{ margin: 0, fontSize: "1.1rem" }}>Runner Workspaces</h2>
          <p
            style={{
              margin: "0.2rem 0 0",
              color: "#6b7280",
              fontSize: "0.85rem",
            }}
          >
            <code>$RUNNER_BASE_PATH</code> · Last refreshed:{" "}
            {lastRefreshedLabel}
          </p>
        </div>
        <button
          type="button"
          onClick={() => void fetchOnce()}
          style={{
            padding: "0.4rem 0.9rem",
            fontSize: "0.9rem",
            border: "1px solid #2563eb",
            color: "#2563eb",
            background: "#ffffff",
            borderRadius: "0.25rem",
            cursor: "pointer",
          }}
        >
          Refresh
        </button>
      </header>

      {actionError && (
        <div
          role="alert"
          style={{
            background: "#fee2e2",
            color: "#7f1d1d",
            padding: "0.6rem 0.9rem",
            borderRadius: "0.25rem",
            marginBottom: "0.75rem",
            fontSize: "0.85rem",
          }}
        >
          {actionError}
        </div>
      )}

      {state.kind === "loading" && <p>Loading workspaces…</p>}
      {state.kind === "error" && (
        <div
          role="alert"
          style={{
            background: "#fef3c7",
            color: "#78350f",
            padding: "0.75rem",
            borderRadius: "0.25rem",
          }}
        >
          Failed to load workspaces: {state.message}
        </div>
      )}

      {state.kind === "ok" && (
        <table
          style={{
            width: "100%",
            borderCollapse: "collapse",
            background: "#ffffff",
            border: "1px solid #e5e7eb",
            borderRadius: "0.25rem",
            overflow: "hidden",
          }}
        >
          <thead>
            <tr>
              <th style={headerCellStyle}>Issue Key</th>
              <th style={headerCellStyle}>Size</th>
              <th style={headerCellStyle}>Last modified</th>
              <th style={{ ...headerCellStyle, width: "8rem" }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {state.rows.length === 0 ? (
              <tr>
                <td style={cellStyle} colSpan={4}>
                  Hiç workspace bulunamadı.
                </td>
              </tr>
            ) : (
              state.rows.map((row) => {
                const busy =
                  confirm.kind === "deleting" &&
                  confirm.issueKey === row.issue_key;
                return (
                  <tr key={row.issue_key}>
                    <td style={cellStyle}>
                      <code>{row.issue_key}</code>
                    </td>
                    <td style={cellStyle}>{formatSize(row.size_mb)}</td>
                    <td style={cellStyle}>
                      {formatTimestamp(row.last_modified)}
                    </td>
                    <td style={cellStyle}>
                      <button
                        type="button"
                        onClick={() => handleAskConfirm(row.issue_key)}
                        disabled={busy}
                        style={{
                          padding: "0.3rem 0.7rem",
                          border: "1px solid #b91c1c",
                          borderRadius: "0.25rem",
                          background: busy ? "#fca5a5" : "#ffffff",
                          color: "#b91c1c",
                          cursor: busy ? "not-allowed" : "pointer",
                          fontSize: "0.85rem",
                        }}
                      >
                        Sil
                      </button>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      )}

      {(confirm.kind === "asking" || confirm.kind === "deleting") && (
        <ConfirmDialog
          issueKey={confirm.issueKey}
          busy={confirm.kind === "deleting"}
          onCancel={handleCancelConfirm}
          onConfirm={() => void handleConfirmDelete()}
        />
      )}
    </section>
  );
}
