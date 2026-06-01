"use client";

/**
 * Audit panel — `platform-mimari-ops` task 12.5 (R4.5 / R6.5 / R6.9)
 * + `gereksinim.txt` G9 E3 iyileştirmesi.
 *
 * Loki + archive-flag enrichment search surface. Calls
 * /admin/audit/search; displays a unified result list with archive
 * restore links.
 *
 * E3 eklentisi: spec satır 27 "servis/seviye/departman bazında
 * filtrelenir" gereği üç filtre eklendi:
 *   - Servis (client_source) — server-side param
 *   - trace_id — server-side param (tek request'i servisler arası izleme)
 *   - Seviye (level) — sonuç satırlarına client-side uygulanır
 *     (audit-search endpoint'i ``level`` query param'ı tanımıyor).
 */

import { useMemo, useState } from "react";

import { apiFetch } from "@/lib/api-client";

type Hit = {
  id: string;
  archived: boolean;
  archive_uri?: string;
  summary?: string;
  actor_id?: string;
  action?: string;
  dept_id?: string | null;
  at?: string;
  client_source?: string;
  trace_id?: string;
  level?: string;
};

/** Bilinen client_source / servis değerleri — dropdown seçenekleri. */
const SERVICE_OPTIONS = [
  "automation-service",
  "automation-worker",
  "agent-runner-worker",
  "execution-runner-worker",
  "assistant-service",
  "atlassian-mcp",
  "admin-dashboard-api",
  "streamlit-ui",
];

/** Log seviyeleri — sonuçlara client-side uygulanan filtre. */
const LEVEL_OPTIONS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];

export default function AuditPage(): JSX.Element {
  const [actorId, setActorId] = useState("");
  const [deptId, setDeptId] = useState("");
  const [action, setAction] = useState("");
  const [clientSource, setClientSource] = useState("");
  const [traceId, setTraceId] = useState("");
  const [level, setLevel] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [hits, setHits] = useState<Hit[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [searched, setSearched] = useState(false);

  const search = async (e: React.FormEvent) => {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (actorId) params.set("actor_id", actorId);
      if (deptId) params.set("dept_id", deptId);
      if (action) params.set("action", action);
      if (clientSource) params.set("client_source", clientSource);
      if (traceId) params.set("trace_id", traceId);
      if (start) params.set("start", start);
      if (end) params.set("end", end);
      const response = await apiFetch(
        `/admin/audit/search?${params.toString()}`,
      );
      if (!response.ok) {
        throw new Error(`Audit search failed: HTTP ${response.status}`);
      }
      const res = (await response.json()) as { results?: Hit[] };
      setHits(res.results ?? []);
      setSearched(true);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  };

  // Level filtresi server-side desteklenmediği için sonuçlara
  // client-side uygulanır. Boşsa tüm satırlar geçer.
  const visibleHits = useMemo(() => {
    if (!level) return hits;
    const wanted = level.toUpperCase();
    return hits.filter((h) => (h.level ?? "").toUpperCase() === wanted);
  }, [hits, level]);

  return (
    <div className="stack stack--lg">
      <header className="page-header">
        <div className="page-header__title-row">
          <div>
            <h1>Audit log</h1>
            <p className="page-header__lede">
              Loki üzerinden aktör, eylem, departman, servis, seviye ve
              trace_id filtreleri ile arama. Arşivlenmiş kayıtlar geri
              yükleme bağlantısı sunar.
            </p>
          </div>
        </div>
      </header>

      <div className="card">
        <div className="card__header">
          <div className="card__title">Arama</div>
        </div>
        <div className="card__body">
          <form onSubmit={search} className="form-grid">
            <div className="field">
              <label className="field__label">Aktör (actor_id)</label>
              <input
                className="input"
                placeholder="örn. user@org"
                value={actorId}
                onChange={(e) => setActorId(e.target.value)}
              />
            </div>
            <div className="field">
              <label className="field__label">Departman</label>
              <input
                className="input"
                placeholder="örn. payment"
                value={deptId}
                onChange={(e) => setDeptId(e.target.value)}
              />
            </div>
            <div className="field">
              <label className="field__label">Eylem</label>
              <input
                className="input"
                placeholder="örn. workflow_started"
                value={action}
                onChange={(e) => setAction(e.target.value)}
              />
            </div>
            <div className="field">
              <label className="field__label">Servis (client_source)</label>
              <select
                className="select"
                value={clientSource}
                onChange={(e) => setClientSource(e.target.value)}
              >
                <option value="">Tümü</option>
                {SERVICE_OPTIONS.map((svc) => (
                  <option key={svc} value={svc}>
                    {svc}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label className="field__label">Seviye</label>
              <select
                className="select"
                value={level}
                onChange={(e) => setLevel(e.target.value)}
              >
                <option value="">Tümü</option>
                {LEVEL_OPTIONS.map((lvl) => (
                  <option key={lvl} value={lvl}>
                    {lvl}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label className="field__label">trace_id</label>
              <input
                className="input"
                placeholder="örn. 0190a1b2-..."
                value={traceId}
                onChange={(e) => setTraceId(e.target.value)}
              />
            </div>
            <div className="field">
              <label className="field__label">Başlangıç</label>
              <input
                className="input"
                type="datetime-local"
                value={start}
                onChange={(e) => setStart(e.target.value)}
              />
            </div>
            <div className="field">
              <label className="field__label">Bitiş</label>
              <input
                className="input"
                type="datetime-local"
                value={end}
                onChange={(e) => setEnd(e.target.value)}
              />
            </div>
            <div className="field" style={{ alignSelf: "end" }}>
              <button type="submit" className="btn btn--primary" disabled={loading}>
                {loading ? <span className="spinner" /> : "🔎"} Ara
              </button>
            </div>
          </form>
        </div>
      </div>

      {error && (
        <div className="banner banner--danger">
          <span className="banner__icon">⚠️</span>
          <div className="banner__body">{error}</div>
        </div>
      )}

      <div className="card">
        <div className="card__header">
          <div>
            <div className="card__title">Sonuçlar</div>
            <div className="card__sub">
              {visibleHits.length} kayıt
              {level && hits.length !== visibleHits.length
                ? ` (${hits.length} kayıttan ${level} seviyesinde süzüldü)`
                : ""}
            </div>
          </div>
        </div>
        <div className="card__body card__body--flush">
          {!searched ? (
            <div className="empty">
              <div className="empty__icon">📜</div>
              <div className="empty__title">Aramaya hazır</div>
              <div className="muted">
                Filtreleri doldurun ve &quot;Ara&quot; düğmesine basın.
              </div>
            </div>
          ) : visibleHits.length === 0 ? (
            <div className="empty">
              <div className="empty__icon">🔍</div>
              <div className="empty__title">Eşleşme bulunamadı</div>
            </div>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th></th>
                  <th>Eylem</th>
                  <th>Aktör</th>
                  <th>Departman</th>
                  <th>Servis</th>
                  <th>Seviye</th>
                  <th>Zaman</th>
                  <th>Özet</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {visibleHits.map((h) => (
                  <tr key={h.id}>
                    <td>
                      <span
                        className={`badge ${h.archived ? "badge--warn" : "badge--success"}`}
                      >
                        {h.archived ? "📦 archive" : "🟢 live"}
                      </span>
                    </td>
                    <td>
                      <strong>{h.action ?? "(unknown)"}</strong>
                      {h.trace_id && (
                        <div className="mono text-xs muted" title="trace_id">
                          {h.trace_id}
                        </div>
                      )}
                    </td>
                    <td className="mono text-sm">{h.actor_id ?? "—"}</td>
                    <td className="text-sm">{h.dept_id ?? "—"}</td>
                    <td className="text-sm">
                      {h.client_source ? (
                        <code className="text-xs">{h.client_source}</code>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>
                      {h.level ? <LevelBadge level={h.level} /> : <span className="muted">—</span>}
                    </td>
                    <td className="muted text-sm">{h.at ?? ""}</td>
                    <td className="muted text-sm" style={{ maxWidth: 320 }}>
                      {h.summary ?? ""}
                    </td>
                    <td>
                      {h.archive_uri && (
                        <a
                          className="btn btn--sm btn--ghost"
                          href={`/audit/restore?uri=${encodeURIComponent(h.archive_uri)}`}
                        >
                          Geri yükle
                        </a>
                      )}
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

function LevelBadge({ level }: { level: string }): JSX.Element {
  const up = level.toUpperCase();
  const tone =
    up === "ERROR" || up === "CRITICAL"
      ? { bg: "var(--danger-50)", fg: "var(--danger-700)" }
      : up === "WARNING"
        ? { bg: "var(--warn-50, #fff7ed)", fg: "var(--warn-700, #b45309)" }
        : { bg: "var(--bg-muted)", fg: "var(--fg-muted)" };
  return (
    <span
      style={{
        padding: "0.1rem 0.45rem",
        borderRadius: 4,
        background: tone.bg,
        color: tone.fg,
        fontSize: "0.72rem",
        fontWeight: 600,
      }}
    >
      {up}
    </span>
  );
}
