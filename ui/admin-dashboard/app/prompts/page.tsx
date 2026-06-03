"use client";

/**
 * Prompts catalogue.
 *
 * Lists every `.md` under `platform/prompts/`. Each row links to the
 * editor at `/prompts/{name}` (catch-all route) where operators run
 * sandbox calls and commit changes as draft PRs.
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { apiFetch } from "@/lib/api-client";

type PromptListItem = {
  name: string;
  last_modified: string;
  content_hash: string;
  size_bytes: number;
};

type PromptListResponse = {
  items: PromptListItem[];
};

function editorHref(name: string): string {
  const encoded = name.split("/").map(encodeURIComponent).join("/");
  return `/prompts/${encoded}`;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MiB`;
}

function formatTimestamp(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString();
}

function truncateHash(hash: string): string {
  return hash.length <= 12 ? hash : `${hash.slice(0, 12)}…`;
}

export default function PromptsPage(): JSX.Element {
  const [rows, setRows] = useState<PromptListItem[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch("/api/v1/prompts");
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(
          `GET /api/v1/prompts → HTTP ${res.status}${
            text ? `: ${text.slice(0, 200)}` : ""
          }`,
        );
      }
      const data = (await res.json()) as PromptListResponse;
      setRows(Array.isArray(data.items) ? data.items : []);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = q
      ? rows.filter((r) => r.name.toLowerCase().includes(q))
      : rows;
    return [...list].sort((a, b) => a.name.localeCompare(b.name));
  }, [rows, query]);

  return (
    <div className="stack stack--lg">
      <header className="page-header">
        <div className="page-header__title-row">
          <div>
            <h1>Promptlar</h1>
            <p className="page-header__lede">
              <code>platform/prompts/</code> altındaki tüm dosyalar.{" "}
              <strong>Düzenle</strong> ile diff + sandbox + commit
              editörünü açın.
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
        <div className="banner banner--danger" role="alert">
          <span className="banner__icon">⚠️</span>
          <div className="banner__body">{error}</div>
        </div>
      )}

      <div className="card">
        <div className="card__header">
          <div className="card__title">Katalog</div>
          <input
            className="input"
            style={{ maxWidth: 280 }}
            type="search"
            placeholder="Prompt adı ara…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        <div className="card__body card__body--flush">
          {filtered.length === 0 && !loading ? (
            <div className="empty">
              <div className="empty__icon">🧠</div>
              <div className="empty__title">
                {rows.length === 0 ? "Prompt bulunamadı" : "Eşleşme yok"}
              </div>
            </div>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Ad</th>
                  <th>Son değişiklik</th>
                  <th className="right">Boyut</th>
                  <th>Hash</th>
                  <th className="right">İşlem</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((row) => (
                  <tr key={row.name}>
                    <td><code>{row.name}</code></td>
                    <td className="muted text-sm">{formatTimestamp(row.last_modified)}</td>
                    <td className="right num">{formatBytes(row.size_bytes)}</td>
                    <td>
                      <code title={row.content_hash} className="muted text-xs">
                        {truncateHash(row.content_hash)}
                      </code>
                    </td>
                    <td className="right">
                      <a className="btn btn--sm btn--primary" href={editorHref(row.name)}>
                        Düzenle
                      </a>
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
