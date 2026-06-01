"use client";

/**
 * Notifications panel — `platform-mimari-ops` task 12.7 (R4.1 / R5.1).
 *
 * Surfaces dept-level notification configuration (Slack webhook
 * vault refs, email recipients, notify_on_success toggle).
 */

import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api-client";

type DeptNotifyRow = {
  dept_id: string;
  notify_on_success: boolean;
  notify_channels: string[];
  slack_webhook_ref: string | null;
  notify_email: string | null;
};

export default function NotificationsPage(): JSX.Element {
  const [rows, setRows] = useState<DeptNotifyRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch<{ items: DeptNotifyRow[] }>(
        "/admin/notifications/config",
      );
      setRows(res.items ?? []);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <div className="stack stack--lg">
      <header className="page-header">
        <div className="page-header__title-row">
          <div>
            <h1>Bildirimler</h1>
            <p className="page-header__lede">
              Departman bazlı Slack webhook, e-posta alıcı ve başarı bildirim
              tercihleri.
            </p>
          </div>
          <div className="page-header__actions">
            <button className="btn" onClick={refresh} disabled={loading}>
              {loading ? <span className="spinner" /> : "🔄"} Yenile
            </button>
          </div>
        </div>
      </header>

      {error && (
        <div className="banner banner--danger">
          <span className="banner__icon">⚠️</span>
          <div className="banner__body">{error}</div>
        </div>
      )}

      <div className="card">
        <div className="card__header">
          <div className="card__title">Yapılandırma</div>
          <div className="card__sub">{rows.length} departman</div>
        </div>
        <div className="card__body card__body--flush">
          {rows.length === 0 ? (
            <div className="empty">
              <div className="empty__icon">🔔</div>
              <div className="empty__title">Yapılandırılmış bildirim yok</div>
            </div>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Departman</th>
                  <th>Başarıda</th>
                  <th>Kanallar</th>
                  <th>Slack referansı</th>
                  <th>E-posta</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.dept_id}>
                    <td><code>{r.dept_id}</code></td>
                    <td>
                      {r.notify_on_success ? (
                        <span className="badge badge--success">on</span>
                      ) : (
                        <span className="badge">off</span>
                      )}
                    </td>
                    <td className="text-sm">
                      {r.notify_channels.length === 0 ? (
                        <span className="muted">—</span>
                      ) : (
                        r.notify_channels.map((c) => (
                          <span key={c} className="badge badge--info" style={{ marginRight: 4 }}>{c}</span>
                        ))
                      )}
                    </td>
                    <td>
                      {r.slack_webhook_ref ? (
                        <code className="text-sm">{r.slack_webhook_ref}</code>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td className="text-sm">
                      {r.notify_email ?? <span className="muted">—</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
