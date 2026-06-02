"use client";

/**
 * PO Review panel — admin-only review of bot-opened draft PRs.
 *
 * Moved out of the Streamlit end-user app: PO review is a governance
 * action that must be gated by admin auth, not exposed to every
 * credential-holding chat user. The list + actions are served by the
 * admin-dashboard-api PO-review proxy (`/api/po-review-inbox`), which
 * forwards to automation-service and enforces `require_admin`.
 *
 * Actions map to the documented automation-service endpoints:
 *   - Approve note → POST .../approve-note
 *   - Request changes → POST .../request-changes
 *   - Re-open draft → POST .../open-draft
 */

import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api-client";

type Department = { id: string; display_name?: string };

type InboxRow = {
  id?: number;
  pr_id?: number;
  source_branch?: string;
  title?: string;
  author_account_id?: string;
  is_draft?: boolean;
  pr_url?: string;
  jira_issue_url?: string;
  diff_summary?: string;
  [key: string]: unknown;
};

type InboxResponse = { items?: InboxRow[]; inbox?: InboxRow[]; pull_requests?: InboxRow[] };

function prId(row: InboxRow): number | null {
  if (typeof row.id === "number") return row.id;
  if (typeof row.pr_id === "number") return row.pr_id;
  return null;
}

export default function PoReviewPage(): JSX.Element {
  const [departments, setDepartments] = useState<Department[]>([]);
  const [deptId, setDeptId] = useState<string>("");
  const [rows, setRows] = useState<InboxRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [comments, setComments] = useState<Record<number, string>>({});
  const [busyPr, setBusyPr] = useState<number | null>(null);

  // Load departments once.
  useEffect(() => {
    void (async () => {
      try {
        const res = await apiFetch("/admin/departments");
        if (!res.ok) return;
        const body = (await res.json()) as { departments?: Department[]; items?: Department[] };
        const list = body.departments ?? body.items ?? [];
        setDepartments(list);
        if (list.length > 0) setDeptId((prev) => prev || list[0].id);
      } catch {
        /* ignore — dept selector simply stays empty */
      }
    })();
  }, []);

  const refresh = useCallback(async () => {
    if (!deptId) return;
    setLoading(true);
    setError(null);
    setNotice(null);
    try {
      const res = await apiFetch(`/api/po-review-inbox?dept_id=${encodeURIComponent(deptId)}`);
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status}${text ? `: ${text.slice(0, 200)}` : ""}`);
      }
      const body = (await res.json()) as InboxResponse | InboxRow[];
      const items = Array.isArray(body)
        ? body
        : body.items ?? body.inbox ?? body.pull_requests ?? [];
      setRows(items);
    } catch (err) {
      setError((err as Error).message);
      setRows([]);
    } finally {
      setLoading(false);
    }
  }, [deptId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const act = useCallback(
    async (id: number, action: "approve-note" | "request-changes" | "open-draft") => {
      setBusyPr(id);
      setError(null);
      setNotice(null);
      try {
        const comment = comments[id] ?? "";
        if (action === "request-changes" && !comment.trim()) {
          throw new Error("Değişiklik talebi için yorum gerekli.");
        }
        const res = await apiFetch(
          `/api/po-review-inbox/${id}/${action}?dept_id=${encodeURIComponent(deptId)}`,
          { method: "POST", body: JSON.stringify({ comment }) },
        );
        if (!res.ok) {
          const text = await res.text().catch(() => "");
          throw new Error(`HTTP ${res.status}${text ? `: ${text.slice(0, 200)}` : ""}`);
        }
        setNotice(`PR #${id}: işlem gönderildi (${action}).`);
        await refresh();
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setBusyPr(null);
      }
    },
    [comments, deptId, refresh],
  );

  return (
    <div className="stack stack--lg">
      <header className="page-header">
        <div className="page-header__title-row">
          <div>
            <h1>PO Review</h1>
            <p className="page-header__lede">
              Bot tarafından açılan draft PR&apos;lar burada incelenir. Onay
              notu, değişiklik talebi ve yeniden açma işlemleri Bitbucket&apos;a
              gerçek yorum/aksiyon olarak gider; tüm aksiyonlar denetim kaydına
              yazılır. Bu sayfa yalnızca yöneticiler içindir.
            </p>
          </div>
          <div className="page-header__actions">
            <button className="btn" onClick={refresh} disabled={loading || !deptId}>
              {loading ? <span className="spinner" /> : "🔄"} Yenile
            </button>
          </div>
        </div>
      </header>

      <div className="card">
        <div className="card__header">
          <div className="card__title">Departman</div>
          <div className="row">
            <select
              className="select"
              style={{ width: 240 }}
              value={deptId}
              onChange={(e) => setDeptId(e.target.value)}
              disabled={loading}
            >
              {departments.length === 0 && <option value="">— departman yok —</option>}
              {departments.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.display_name ? `${d.display_name} (${d.id})` : d.id}
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
          {notice && (
            <div className="banner banner--success" style={{ margin: "1rem" }}>
              <span className="banner__icon">✅</span>
              <div className="banner__body">{notice}</div>
            </div>
          )}

          {!error && rows.length === 0 ? (
            <div className="empty">
              <div className="empty__icon">📥</div>
              <div className="empty__title">Bekleyen PO inceleme talebi yok</div>
              <div className="muted">Botun açtığı draft PR olduğunda burada listelenir.</div>
            </div>
          ) : (
            <div className="stack" style={{ padding: "1rem", gap: "1rem" }}>
              {rows.map((row) => {
                const id = prId(row);
                if (id === null) return null;
                const busy = busyPr === id;
                return (
                  <div key={id} className="card" style={{ margin: 0 }}>
                    <div className="card__header">
                      <div className="card__title">
                        <span className="mono">PR #{id}</span> {row.title ?? ""}
                      </div>
                      <div className="row" style={{ gap: "0.75rem" }}>
                        {row.is_draft && <span className="badge badge--warn">draft</span>}
                        {row.source_branch && (
                          <code className="text-sm">{row.source_branch}</code>
                        )}
                      </div>
                    </div>
                    <div className="card__body stack" style={{ gap: "0.6rem" }}>
                      <div className="row" style={{ gap: "1rem" }}>
                        {row.pr_url && (
                          <a href={row.pr_url} target="_blank" rel="noopener noreferrer">
                            Bitbucket PR ↗
                          </a>
                        )}
                        {row.jira_issue_url && (
                          <a href={row.jira_issue_url} target="_blank" rel="noopener noreferrer">
                            Jira issue ↗
                          </a>
                        )}
                      </div>
                      {row.diff_summary && (
                        <p className="muted text-sm" style={{ margin: 0 }}>
                          {row.diff_summary}
                        </p>
                      )}
                      <textarea
                        className="input"
                        rows={2}
                        placeholder="Yorum / değişiklik talebi"
                        value={comments[id] ?? ""}
                        onChange={(e) =>
                          setComments((prev) => ({ ...prev, [id]: e.target.value }))
                        }
                        disabled={busy}
                      />
                      <div className="row" style={{ gap: "0.5rem" }}>
                        <button
                          className="btn btn--primary btn--sm"
                          onClick={() => act(id, "approve-note")}
                          disabled={busy}
                        >
                          ✅ Onay notu
                        </button>
                        <button
                          className="btn btn--sm"
                          onClick={() => act(id, "request-changes")}
                          disabled={busy}
                        >
                          ✏️ Değişiklik iste
                        </button>
                        <button
                          className="btn btn--ghost btn--sm"
                          onClick={() => act(id, "open-draft")}
                          disabled={busy}
                        >
                          ↩️ Draft&apos;ı yeniden aç
                        </button>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
