"use client";

/**
 * Workflows panel.
 *
 * Lists active Temporal workflows with a status filter, pagination and
 * a drill-down link. Data is served by the admin-dashboard-api
 * `WorkflowsDrillDownRouter` which proxies to
 * automation-service.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { apiFetch } from "@/lib/api-client";

type WorkflowRow = {
  workflow_id: string;
  workflow_type: string;
  dept_id: string | null;
  status: string;
  iteration_count?: number;
  summary?: string;
  jira_issue_url?: string | null;
};

type ListResponse = {
  items?: WorkflowRow[];
  workflows?: WorkflowRow[];
};

const STATUS_OPTIONS = [
  "all",
  "running",
  "completed",
  "failed",
  "partial",
] as const;

const STATUS_BADGE: Record<string, string> = {
  running: "badge--info",
  completed: "badge--success",
  failed: "badge--danger",
  partial: "badge--warn",
};

export default function WorkflowsPage(): JSX.Element {
  const [status, setStatus] = useState<string>("all");
  const [rows, setRows] = useState<WorkflowRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ page_size: "50" });
      if (status !== "all") {
        params.set("status", status);
      }
      const res = await apiFetch(`/api/v1/workflows?${params.toString()}`);
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(`GET /api/v1/workflows → HTTP ${res.status}${text ? `: ${text.slice(0, 200)}` : ""}`);
      }
      const body = (await res.json()) as ListResponse;
      const items = body.items ?? body.workflows ?? [];
      setRows(items);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [status]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const summary = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const r of rows) counts[r.status] = (counts[r.status] ?? 0) + 1;
    return counts;
  }, [rows]);

  return (
    <div className="stack stack--lg">
      <header className="page-header">
        <div className="page-header__title-row">
          <div>
            <h1>İş akışları</h1>
            <p className="page-header__lede">
              Temporal üzerinde çalışan workflow&apos;ları izleyin. iter≥3
              yüksek tekrarı işaretler - yeniden kapsam belirleme önerilir.
            </p>
          </div>
          <div className="page-header__actions">
            <button className="btn" onClick={refresh} disabled={loading}>
              {loading ? <span className="spinner" /> : "🔄"} Yenile
            </button>
          </div>
        </div>
      </header>

      <div className="stat-grid">
        <div className="stat-card">
          <div className="stat-card__label">Toplam</div>
          <div className="stat-card__value num">{rows.length}</div>
          <div className="stat-card__delta">Filtre: {status}</div>
        </div>
        <div className="stat-card">
          <div className="stat-card__label">Çalışan</div>
          <div className="stat-card__value num">{summary.running ?? 0}</div>
          <div className="stat-card__delta">Aktif workflow</div>
        </div>
        <div className="stat-card">
          <div className="stat-card__label">Hatalı</div>
          <div className="stat-card__value num">{summary.failed ?? 0}</div>
          <div className="stat-card__delta">Müdahale gerektirenler</div>
        </div>
        <div className="stat-card">
          <div className="stat-card__label">Tamamlanan</div>
          <div className="stat-card__value num">{summary.completed ?? 0}</div>
          <div className="stat-card__delta">Son çalışmalar</div>
        </div>
      </div>

      <div className="card">
        <div className="card__header">
          <div className="card__title">Filtre</div>
          <div className="row">
            <label className="muted text-sm">Durum</label>
            <select
              className="select"
              style={{ width: 200 }}
              value={status}
              onChange={(e) => setStatus(e.target.value)}
              disabled={loading}
            >
              {STATUS_OPTIONS.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
        </div>

        <div className="card__body card__body--flush">
          {error && (
            <div className="banner banner--danger" style={{ margin: "1rem" }}>
              <span className="banner__icon">⚠️</span>
              <div className="banner__body">{error}</div>
            </div>
          )}

          {!error && rows.length === 0 ? (
            <div className="empty">
              <div className="empty__icon">🔁</div>
              <div className="empty__title">Workflow bulunamadı</div>
              <div className="muted">Filtre değiştirip yeniden deneyin.</div>
            </div>
          ) : (
            <div className="table-wrap" style={{ borderRadius: 0, border: 0, boxShadow: "none" }}>
              <table className="table">
                <thead>
                  <tr>
                    <th>Workflow</th>
                    <th>Tip</th>
                    <th>Departman</th>
                    <th>Durum</th>
                    <th className="right">Iter</th>
                    <th>Özet</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => {
                    const badgeClass = STATUS_BADGE[r.status] ?? "";
                    const iter = r.iteration_count ?? 0;
                    return (
                      <tr key={r.workflow_id}>
                        <td>
                          <a
                            href={`/workflows/${encodeURIComponent(r.workflow_id)}`}
                            className="mono text-sm"
                          >
                            {r.workflow_id}
                          </a>
                        </td>
                        <td className="text-sm">{r.workflow_type}</td>
                        <td className="text-sm">
                          {r.dept_id ? <code>{r.dept_id}</code> : <span className="muted">-</span>}
                        </td>
                        <td>
                          <span className={`badge ${badgeClass}`}>
                            <span className="badge__dot" /> {r.status}
                          </span>
                        </td>
                        <td className="right num">
                          {iter >= 3 ? (
                            <span
                              className="badge badge--warn"
                              title="iter≥3 - yeniden kapsam belirlensin mi?"
                            >
                              {iter}
                            </span>
                          ) : (
                            iter
                          )}
                        </td>
                        <td className="muted text-sm">{r.summary ?? ""}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
