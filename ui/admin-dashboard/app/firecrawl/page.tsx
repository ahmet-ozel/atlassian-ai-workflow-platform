"use client";

/**
 * Firecrawl egress allowlist page — `gereksinim.txt` G9 (E2 iyileştirme).
 *
 * CRUD surface over `GET/POST/DELETE /api/v1/firecrawl/allowlist`. The
 * firecrawl service only fetches external URLs whose domain is on this
 * allowlist (Requirement 10.3 — FIRECRAWL_EGRESS_ALLOWLIST contract).
 *
 * Operators add/remove domains here instead of hand-editing env vars.
 * The backend store is currently in-memory (see firecrawl_allowlist.py
 * `_allowlist`); the page surfaces that as an informational note.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { apiFetch } from "@/lib/api-client";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type DomainEntry = {
  id: string;
  domain: string;
  added_by: string;
  added_at: string;
};

// A permissive DNS check mirroring the server-side ``_DNS_PATTERN`` so
// the user gets instant feedback before the request round-trips.
const DNS_RE =
  /^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,}$/;

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function FirecrawlAllowlistPage(): JSX.Element {
  const [domains, setDomains] = useState<DomainEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [draft, setDraft] = useState("");
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const cancelledRef = useRef(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch("/api/v1/firecrawl/allowlist/?page=1&page_size=50");
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status}${text ? `: ${text.slice(0, 200)}` : ""}`);
      }
      const body = (await res.json()) as { domains?: unknown };
      const list: DomainEntry[] = Array.isArray(body.domains)
        ? (body.domains as unknown[]).map((d) => {
            const e = (d ?? {}) as Record<string, unknown>;
            return {
              id: String(e.id ?? ""),
              domain: String(e.domain ?? ""),
              added_by: String(e.added_by ?? "system"),
              added_at: String(e.added_at ?? ""),
            };
          })
        : [];
      if (!cancelledRef.current) setDomains(list);
    } catch (err) {
      if (!cancelledRef.current) setError((err as Error).message);
    } finally {
      if (!cancelledRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    cancelledRef.current = false;
    void load();
    return () => {
      cancelledRef.current = true;
    };
  }, [load]);

  const trimmed = draft.trim().toLowerCase();
  const draftValid = trimmed.length > 0 && trimmed.length <= 253 && DNS_RE.test(trimmed);

  const handleAdd = useCallback(async () => {
    if (!draftValid) return;
    setAdding(true);
    setAddError(null);
    setNotice(null);
    try {
      const res = await apiFetch("/api/v1/firecrawl/allowlist/", {
        method: "POST",
        body: JSON.stringify({ domain: trimmed }),
      });
      if (res.status === 409) {
        throw new Error(`'${trimmed}' zaten allowlist'te.`);
      }
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status}${text ? `: ${text.slice(0, 200)}` : ""}`);
      }
      setDraft("");
      setNotice(`'${trimmed}' allowlist'e eklendi.`);
      await load();
    } catch (err) {
      setAddError((err as Error).message);
    } finally {
      setAdding(false);
    }
  }, [draftValid, trimmed, load]);

  const handleDelete = useCallback(
    async (entry: DomainEntry) => {
      setDeletingId(entry.id);
      setError(null);
      setNotice(null);
      try {
        const res = await apiFetch(
          `/api/v1/firecrawl/allowlist/${encodeURIComponent(entry.id)}`,
          { method: "DELETE" },
        );
        if (!res.ok) {
          const text = await res.text().catch(() => "");
          throw new Error(`HTTP ${res.status}${text ? `: ${text.slice(0, 200)}` : ""}`);
        }
        setNotice(`'${entry.domain}' allowlist'ten kaldırıldı.`);
        await load();
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setDeletingId(null);
      }
    },
    [load],
  );

  return (
    <div className="stack stack--lg">
      <header className="page-header">
        <div className="page-header__title-row">
          <div>
            <h1>Firecrawl egress allowlist</h1>
            <p className="page-header__lede">
              Firecrawl yalnızca burada listelenen domain'lere dış HTTP isteği
              atabilir (Requirement 10.3 — egress allowlist sözleşmesi).
              Domain ekleyip kaldırarak izinli alanları yönetin.
            </p>
          </div>
          <div className="page-header__actions">
            <button className="btn" onClick={load} disabled={loading}>
              {loading ? <span className="spinner" /> : "🔄"} Yenile
            </button>
          </div>
        </div>
      </header>

      <div className="banner banner--info" role="note">
        <span className="banner__icon">ℹ️</span>
        <div className="banner__body">
          Allowlist şu an in-memory tutuluyor — firecrawl servisi yeniden
          başlarsa liste sıfırlanır. Kalıcı saklama için bir sonraki sürümde
          PostgreSQL <code>firecrawl_allowlist</code> tablosu devreye girecek.
        </div>
      </div>

      {error && (
        <div className="banner banner--danger" role="alert">
          <span className="banner__icon">⚠️</span>
          <div className="banner__body">{error}</div>
        </div>
      )}
      {notice && (
        <div className="banner banner--success" role="status">
          <span className="banner__icon">✅</span>
          <div className="banner__body">{notice}</div>
        </div>
      )}

      {/* Add domain form */}
      <div className="card">
        <div className="card__header">
          <div className="card__title">Domain ekle</div>
        </div>
        <div className="card__body">
          <div className="row" style={{ gap: "0.75rem", alignItems: "flex-start" }}>
            <div className="stack" style={{ gap: 4, flex: 1 }}>
              <input
                className="input"
                placeholder="örn. docs.example.com"
                value={draft}
                onChange={(ev) => {
                  setDraft(ev.target.value);
                  setAddError(null);
                }}
                onKeyDown={(ev) => {
                  if (ev.key === "Enter" && draftValid && !adding) void handleAdd();
                }}
                aria-label="Eklenecek domain"
              />
              {draft.trim() !== "" && !draftValid && (
                <span className="text-xs" style={{ color: "var(--danger-700)" }}>
                  Geçerli bir DNS domaini girin (örn. <code>example.com</code>).
                </span>
              )}
              {addError && (
                <span className="text-xs" style={{ color: "var(--danger-700)" }}>
                  {addError}
                </span>
              )}
            </div>
            <button
              className="btn btn--primary"
              onClick={handleAdd}
              disabled={!draftValid || adding}
            >
              {adding ? <span className="spinner" /> : "➕"} Ekle
            </button>
          </div>
        </div>
      </div>

      {/* Allowlist table */}
      <div className="card">
        <div className="card__header">
          <div className="card__title">
            İzinli domain'ler{" "}
            {domains ? <span className="muted">({domains.length})</span> : null}
          </div>
        </div>
        <div className="card__body card__body--flush">
          {loading && domains === null ? (
            <div className="card__body">
              <div className="skeleton" style={{ height: 80 }} />
            </div>
          ) : domains && domains.length > 0 ? (
            <table className="table">
              <thead>
                <tr>
                  <th>Domain</th>
                  <th>Ekleyen</th>
                  <th>Eklenme</th>
                  <th style={{ textAlign: "right" }}>İşlem</th>
                </tr>
              </thead>
              <tbody>
                {domains.map((entry) => (
                  <tr key={entry.id}>
                    <td>
                      <code>{entry.domain}</code>
                    </td>
                    <td className="muted text-sm">{entry.added_by}</td>
                    <td className="muted text-xs">
                      {entry.added_at
                        ? new Date(entry.added_at).toLocaleString("tr-TR")
                        : "—"}
                    </td>
                    <td style={{ textAlign: "right" }}>
                      <button
                        className="btn btn--sm btn--ghost"
                        onClick={() => handleDelete(entry)}
                        disabled={deletingId === entry.id}
                        aria-label={`${entry.domain} domainini kaldır`}
                      >
                        {deletingId === entry.id ? (
                          <span className="spinner" />
                        ) : (
                          "🗑️ Kaldır"
                        )}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="empty" style={{ padding: "2rem" }}>
              <div className="empty__icon">🌐</div>
              <div className="empty__title">Allowlist boş</div>
              <p className="muted text-sm" style={{ marginTop: 4 }}>
                Yukarıdaki formdan ilk domain&apos;i ekleyin.
              </p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
