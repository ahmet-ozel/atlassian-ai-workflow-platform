"use client";

/**
 * RunnerQueueCard — SSH runner queue/quota visibility widget.
 *
 * Displays a summary card: "Aktif: N/quota — Kuyrukta: M — Ortalama bekleme: Tdk"
 * with color thresholds (%80 yellow, %95 red).
 *
 * Below the summary, a workspace table lists each active/queued workspace
 * with issue_key, dept_id, status, queued_at, started_at, path, and
 * Cancel/Force Cleanup action buttons for admins.
 *
 * Connects to the SSE stream at `/admin/runner/queue-status/stream` for
 * real-time updates.
 *
 * Requirements: 15.3, 15.4, 15.6
 */

import { useCallback, useEffect, useState } from "react";

import {
  apiFetch,
  getAdminApiBaseUrl,
  getAdminAuthHeaders,
} from "@/lib/api-client";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type DeptBreakdown = {
  dept_id: string;
  active: number;
  queued: number;
  quota: number;
};

type QueueStatus = {
  active_count: number;
  queued_count: number;
  avg_wait_seconds: number;
  max_concurrent_global: number;
  by_dept: DeptBreakdown[];
};

type WorkspaceEntry = {
  issue_key: string;
  dept_id?: string;
  status: string;
  queued_at?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  path?: string;
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatWaitTime(seconds: number): string {
  if (seconds <= 0) return "0dk";
  if (seconds < 60) return `${Math.round(seconds)}sn`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}dk`;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  return remainingMinutes > 0 ? `${hours}sa ${remainingMinutes}dk` : `${hours}sa`;
}

function getUtilizationRatio(active: number, quota: number): number {
  if (quota <= 0) return 0;
  return active / quota;
}

function getCardColor(ratio: number): { bg: string; border: string; text: string } {
  if (ratio >= 0.95) {
    return { bg: "#fef2f2", border: "#fca5a5", text: "#991b1b" };
  }
  if (ratio >= 0.8) {
    return { bg: "#fefce8", border: "#fde047", text: "#854d0e" };
  }
  return { bg: "#f0fdf4", border: "#86efac", text: "#166534" };
}

function formatDate(dateStr: string | null | undefined): string {
  if (!dateStr) return "—";
  try {
    return new Date(dateStr).toLocaleString("tr-TR");
  } catch {
    return dateStr;
  }
}

// ---------------------------------------------------------------------------
// Inline styles
// ---------------------------------------------------------------------------

const cardContainerStyle: React.CSSProperties = {
  border: "1px solid #e5e7eb",
  borderRadius: 8,
  padding: "1.25rem",
  marginBottom: "1.5rem",
};

const summaryStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "1.5rem",
  flexWrap: "wrap",
};

const metricStyle: React.CSSProperties = {
  fontSize: "1.1rem",
  fontWeight: 600,
};

const tableStyle: React.CSSProperties = {
  width: "100%",
  borderCollapse: "collapse",
  fontSize: "0.9rem",
  marginTop: "1rem",
};

const thStyle: React.CSSProperties = {
  borderBottom: "1px solid #ccc",
  textAlign: "left",
  padding: "0.5rem 0.75rem",
  fontWeight: 600,
};

const tdStyle: React.CSSProperties = {
  borderBottom: "1px solid #eee",
  padding: "0.5rem 0.75rem",
  verticalAlign: "middle",
};

const btnStyle: React.CSSProperties = {
  padding: "0.3rem 0.6rem",
  border: "1px solid #d1d5db",
  background: "#fff",
  borderRadius: 4,
  cursor: "pointer",
  fontSize: "0.75rem",
  marginRight: "0.3rem",
};

const btnDangerStyle: React.CSSProperties = {
  ...btnStyle,
  background: "#dc2626",
  color: "#fff",
  border: "none",
};

const statusBadge = (status: string): React.CSSProperties => {
  const colors: Record<string, { bg: string; color: string }> = {
    running: { bg: "#dbeafe", color: "#1e40af" },
    queued: { bg: "#fef9c3", color: "#854d0e" },
    completed: { bg: "#dcfce7", color: "#166534" },
    failed: { bg: "#fef2f2", color: "#991b1b" },
    cancelled: { bg: "#f3f4f6", color: "#374151" },
  };
  const c = colors[status] ?? { bg: "#f3f4f6", color: "#374151" };
  return {
    display: "inline-block",
    padding: "0.15rem 0.5rem",
    borderRadius: 12,
    fontSize: "0.75rem",
    fontWeight: 600,
    background: c.bg,
    color: c.color,
  };
};

const liveIndicatorStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: "0.35rem",
  fontSize: "0.75rem",
  color: "#6b7280",
};

const liveDotStyle = (connected: boolean): React.CSSProperties => ({
  width: 8,
  height: 8,
  borderRadius: "50%",
  background: connected ? "#22c55e" : "#ef4444",
});

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function RunnerQueueCard(): JSX.Element {
  const [queueStatus, setQueueStatus] = useState<QueueStatus | null>(null);
  const [workspaces, setWorkspaces] = useState<WorkspaceEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sseConnected, setSseConnected] = useState(false);
  const [actionLoading, setActionLoading] = useState<string | null>(null);

  // --- Initial fetch -------------------------------------------------------

  const fetchInitial = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [statusRes, workspacesRes] = await Promise.all([
        apiFetch("/admin/runner/queue-status"),
        apiFetch("/admin/runner/workspaces"),
      ]);

      if (statusRes.ok) {
        const data = (await statusRes.json()) as QueueStatus;
        setQueueStatus(data);
      } else {
        const body = await statusRes.text();
        setError(`Queue status: HTTP ${statusRes.status} — ${body.slice(0, 200)}`);
      }

      if (workspacesRes.ok) {
        const data = (await workspacesRes.json()) as { workspaces: WorkspaceEntry[] };
        setWorkspaces(data.workspaces ?? []);
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  // --- SSE connection ------------------------------------------------------

  useEffect(() => {
    void fetchInitial();

    const controller = new AbortController();
    const decoder = new TextDecoder();

    const connectStream = async () => {
      try {
        const res = await fetch(
          `${getAdminApiBaseUrl()}/admin/runner/queue-status/stream`,
          {
            headers: getAdminAuthHeaders({ Accept: "text/event-stream" }),
            signal: controller.signal,
          },
        );
        if (!res.ok || res.body === null) {
          setSseConnected(false);
          return;
        }

        setSseConnected(true);
        const reader = res.body.getReader();
        let buffer = "";

        while (!controller.signal.aborted) {
          const { done, value } = await reader.read();
          if (done) {
            break;
          }
          buffer += decoder.decode(value, { stream: true });
          const frames = buffer.split("\n\n");
          buffer = frames.pop() ?? "";

          for (const frame of frames) {
            const dataLine = frame
              .split("\n")
              .find((line) => line.startsWith("data: "));
            if (!dataLine) {
              continue;
            }
            try {
              const data = JSON.parse(dataLine.slice(6)) as QueueStatus;
              setQueueStatus(data);
              setError(null);
            } catch {
              // Ignore malformed SSE frames
            }
          }
        }
      } catch (err) {
        if (!controller.signal.aborted) {
          setError((err as Error).message);
        }
      } finally {
        setSseConnected(false);
      }
    };

    void connectStream();

    return () => {
      controller.abort();
      setSseConnected(false);
    };
  }, [fetchInitial]);

  // --- Action handlers -----------------------------------------------------

  const handleCancel = useCallback(async (issueKey: string) => {
    setActionLoading(issueKey);
    try {
      const res = await apiFetch(
        `/admin/runner/workspaces/${encodeURIComponent(issueKey)}`,
        { method: "DELETE" },
      );
      if (!res.ok) {
        const body = await res.text();
        setError(`Cancel failed: HTTP ${res.status} — ${body.slice(0, 200)}`);
      } else {
        // Remove from local list
        setWorkspaces((prev) => prev.filter((w) => w.issue_key !== issueKey));
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setActionLoading(null);
    }
  }, []);

  // --- Render --------------------------------------------------------------

  const ratio = queueStatus
    ? getUtilizationRatio(queueStatus.active_count, queueStatus.max_concurrent_global)
    : 0;
  const cardColor = getCardColor(ratio);

  return (
    <section style={cardContainerStyle}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: "0.75rem",
        }}
      >
        <h2 style={{ margin: 0, fontSize: "1.1rem" }}>SSH Runner Kuyruğu</h2>
        <span style={liveIndicatorStyle}>
          <span style={liveDotStyle(sseConnected)} />
          {sseConnected ? "Canlı" : "Bağlantı kesildi"}
        </span>
      </div>

      {error && (
        <p role="alert" style={{ color: "crimson", fontSize: "0.9rem" }}>
          Hata: {error}
        </p>
      )}

      {loading && !queueStatus ? (
        <p style={{ color: "#6b7280" }}>Yükleniyor…</p>
      ) : queueStatus ? (
        <>
          {/* Summary card */}
          <div
            style={{
              ...summaryStyle,
              background: cardColor.bg,
              border: `1px solid ${cardColor.border}`,
              borderRadius: 6,
              padding: "0.75rem 1rem",
              color: cardColor.text,
            }}
            role="status"
            aria-label={`Aktif: ${queueStatus.active_count}/${queueStatus.max_concurrent_global}, Kuyrukta: ${queueStatus.queued_count}, Ortalama bekleme: ${formatWaitTime(queueStatus.avg_wait_seconds)}`}
          >
            <span style={metricStyle}>
              Aktif: {queueStatus.active_count}/{queueStatus.max_concurrent_global}
            </span>
            <span style={{ color: cardColor.text }}>—</span>
            <span style={metricStyle}>
              Kuyrukta: {queueStatus.queued_count}
            </span>
            <span style={{ color: cardColor.text }}>—</span>
            <span style={metricStyle}>
              Ortalama bekleme: {formatWaitTime(queueStatus.avg_wait_seconds)}
            </span>
          </div>

          {/* Per-department breakdown */}
          {queueStatus.by_dept.length > 0 && (
            <details style={{ marginTop: "0.75rem" }}>
              <summary
                style={{ cursor: "pointer", fontSize: "0.85rem", color: "#6b7280" }}
              >
                Departman bazlı dağılım ({queueStatus.by_dept.length} dept)
              </summary>
              <ul style={{ margin: "0.5rem 0 0 1rem", fontSize: "0.85rem" }}>
                {queueStatus.by_dept.map((d) => (
                  <li key={d.dept_id}>
                    <strong>{d.dept_id}</strong>: Aktif {d.active}, Kuyrukta{" "}
                    {d.queued}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </>
      ) : null}

      {/* Workspace table */}
      {workspaces.length > 0 && (
        <div style={{ overflowX: "auto", marginTop: "1rem" }}>
          <table style={tableStyle}>
            <thead>
              <tr>
                <th style={thStyle}>Issue Key</th>
                <th style={thStyle}>Dept</th>
                <th style={thStyle}>Durum</th>
                <th style={thStyle}>Kuyruğa Giriş</th>
                <th style={thStyle}>Başlangıç</th>
                <th style={thStyle}>İşlemler</th>
              </tr>
            </thead>
            <tbody>
              {workspaces.map((ws) => (
                <tr key={ws.issue_key}>
                  <td style={tdStyle}>
                    <code style={{ fontSize: "0.85rem" }}>{ws.issue_key}</code>
                  </td>
                  <td style={tdStyle}>{ws.dept_id ?? "—"}</td>
                  <td style={tdStyle}>
                    <span style={statusBadge(ws.status)}>
                      {ws.status === "running"
                        ? "Çalışıyor"
                        : ws.status === "queued"
                          ? "Kuyrukta"
                          : ws.status}
                    </span>
                  </td>
                  <td style={tdStyle}>{formatDate(ws.queued_at)}</td>
                  <td style={tdStyle}>{formatDate(ws.started_at)}</td>
                  <td style={tdStyle}>
                    <button
                      type="button"
                      style={btnStyle}
                      disabled={actionLoading === ws.issue_key}
                      onClick={() => void handleCancel(ws.issue_key)}
                    >
                      {actionLoading === ws.issue_key ? "…" : "Cancel"}
                    </button>
                    <button
                      type="button"
                      style={btnDangerStyle}
                      disabled={actionLoading === ws.issue_key}
                      onClick={() => void handleCancel(ws.issue_key)}
                    >
                      {actionLoading === ws.issue_key ? "…" : "Force Cleanup"}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {workspaces.length === 0 && !loading && (
        <p style={{ color: "#6b7280", marginTop: "1rem", fontSize: "0.9rem" }}>
          Şu an aktif veya kuyrukta workspace bulunmuyor.
        </p>
      )}
    </section>
  );
}
