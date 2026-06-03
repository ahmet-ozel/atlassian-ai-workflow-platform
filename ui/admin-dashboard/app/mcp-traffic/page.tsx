"use client";

/**
 * MCP traffic page.
 *
 * Surfaces the `GET /api/v1/mcp/traffic` snapshot from the atlassian-mcp
 * Prometheus exposition: per-client_source / per-tool / per-status
 * request counters. Operators use it to answer "hangi client'tan ne
 * kadar çağrı geldi, hangi tool ne sıklıkta, hangisinde fail oldu".
 *
 * The page offers three independent filters (client_source, tool,
 * status) and auto-refreshes every 60 seconds. The MCP counters are
 * cumulative since the MCP process started (documented snapshot
 * framing — see mcp_traffic.py module docstring).
 */

import {
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { apiFetch } from "@/lib/api-client";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type TrafficRow = {
  client_source: string;
  tool: string;
  status: string;
  count: number;
};

type TrafficTotals = {
  by_client_source: Record<string, number>;
  by_tool: Record<string, number>;
  by_status: Record<string, number>;
  total: number;
};

type TrafficEnvelope = {
  totals: TrafficTotals;
  rows: TrafficRow[];
  fetched_at: string;
  source: string;
};

type Filters = {
  client_source: string;
  tool: string;
  status: string;
};

const AUTO_REFRESH_MS = 60_000;
const EMPTY_FILTERS: Filters = { client_source: "", tool: "", status: "" };

// ---------------------------------------------------------------------------
// Parsing helpers
// ---------------------------------------------------------------------------

function parseTotals(raw: unknown): TrafficTotals {
  const obj = (raw ?? {}) as Record<string, unknown>;
  const numberMap = (v: unknown): Record<string, number> => {
    const out: Record<string, number> = {};
    if (v && typeof v === "object") {
      for (const [k, val] of Object.entries(v as Record<string, unknown>)) {
        const n = Number(val);
        if (Number.isFinite(n)) out[k] = n;
      }
    }
    return out;
  };
  return {
    by_client_source: numberMap(obj.by_client_source),
    by_tool: numberMap(obj.by_tool),
    by_status: numberMap(obj.by_status),
    total: Number.isFinite(Number(obj.total)) ? Number(obj.total) : 0,
  };
}

function parseEnvelope(raw: unknown): TrafficEnvelope {
  const obj = (raw ?? {}) as Record<string, unknown>;
  const rows: TrafficRow[] = Array.isArray(obj.rows)
    ? (obj.rows as unknown[]).map((r) => {
        const row = (r ?? {}) as Record<string, unknown>;
        return {
          client_source: String(row.client_source ?? "unknown"),
          tool: String(row.tool ?? "unknown"),
          status: String(row.status ?? "unknown"),
          count: Number.isFinite(Number(row.count)) ? Number(row.count) : 0,
        };
      })
    : [];
  return {
    totals: parseTotals(obj.totals),
    rows,
    fetched_at: String(obj.fetched_at ?? ""),
    source: String(obj.source ?? "atlassian-mcp"),
  };
}

/**
 * MCP tool labels can carry non-printable bytes when a fuzzing client
 * sends garbage (see test_27). Replace ASCII control codepoints
 * (0x00-0x1F, 0x7F) with a dot and truncate so the table never breaks
 * layout. Implemented as a codepoint scan to avoid embedding literal
 * control bytes in a regex.
 */
function safeLabel(value: string, max = 48): string {
  let cleaned = "";
  for (let i = 0; i < value.length; i += 1) {
    const code = value.charCodeAt(i);
    cleaned += code < 0x20 || code === 0x7f ? "·" : value[i];
  }
  return cleaned.length > max ? `${cleaned.slice(0, max)}…` : cleaned;
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function McpTrafficPage(): JSX.Element {
  const [data, setData] = useState<TrafficEnvelope | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);
  const [optionPool, setOptionPool] = useState<TrafficTotals | null>(null);

  const cancelledRef = useRef(false);

  const load = useCallback(async (active: Filters) => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (active.client_source) params.set("client_source", active.client_source);
      if (active.tool) params.set("tool", active.tool);
      if (active.status) params.set("status", active.status);
      const qs = params.toString();
      const res = await apiFetch(`/api/v1/mcp/traffic${qs ? `?${qs}` : ""}`);
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        if (res.status === 503) {
          throw new Error(
            "MCP metrics henüz hazır değil (atlassian-mcp ayakta mı?).",
          );
        }
        throw new Error(`HTTP ${res.status}${text ? `: ${text.slice(0, 200)}` : ""}`);
      }
      const parsed = parseEnvelope(await res.json());
      if (!cancelledRef.current) {
        setData(parsed);
        setLastRefreshedAt(new Date());
        // Seed the filter option pool from the first unfiltered fetch so
        // the dropdowns never collapse after a selection narrows results.
        if (
          active.client_source === "" &&
          active.tool === "" &&
          active.status === ""
        ) {
          setOptionPool(parsed.totals);
        }
      }
    } catch (err) {
      if (!cancelledRef.current) setError((err as Error).message);
    } finally {
      if (!cancelledRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    cancelledRef.current = false;
    void load(filters);
    const id = setInterval(() => void load(filters), AUTO_REFRESH_MS);
    return () => {
      cancelledRef.current = true;
      clearInterval(id);
    };
  }, [load, filters]);

  const clientOptions = useMemo(
    () => Object.keys(optionPool?.by_client_source ?? data?.totals.by_client_source ?? {}),
    [optionPool, data],
  );
  const toolOptions = useMemo(
    () => Object.keys(optionPool?.by_tool ?? data?.totals.by_tool ?? {}),
    [optionPool, data],
  );

  const hasActiveFilter =
    filters.client_source !== "" || filters.tool !== "" || filters.status !== "";

  return (
    <div className="stack stack--lg">
      <header className="page-header">
        <div className="page-header__title-row">
          <div>
            <h1>MCP trafiği</h1>
            <p className="page-header__lede">
              atlassian-mcp sunucusuna gelen isteklerin kaynak (client_source),
              tool ve sonuç (success/error) kırılımı. Sayaçlar MCP süreci
              başladığından beri kümülatiftir.
            </p>
          </div>
          <div className="page-header__actions">
            <button className="btn" onClick={() => load(filters)} disabled={loading}>
              {loading ? <span className="spinner" /> : "🔄"} Yenile
            </button>
          </div>
        </div>
        {lastRefreshedAt && (
          <div className="muted text-xs" style={{ marginTop: 6 }}>
            Son yenileme: {lastRefreshedAt.toLocaleTimeString()} · 60 saniyede bir otomatik
            {data?.source ? ` · kaynak: ${data.source}` : ""}
          </div>
        )}
      </header>

      {error && (
        <div className="banner banner--danger" role="alert">
          <span className="banner__icon">⚠️</span>
          <div className="banner__body">{error}</div>
        </div>
      )}

      <SummaryCards totals={data?.totals ?? null} />

      <div className="card">
        <div className="card__body">
          <div className="row" style={{ gap: "1rem", flexWrap: "wrap", alignItems: "flex-end" }}>
            <FilterSelect
              label="Client kaynağı"
              value={filters.client_source}
              options={clientOptions}
              onChange={(v) => setFilters((f) => ({ ...f, client_source: v }))}
            />
            <FilterSelect
              label="Tool"
              value={filters.tool}
              options={toolOptions}
              onChange={(v) => setFilters((f) => ({ ...f, tool: v }))}
              renderOption={safeLabel}
            />
            <FilterSelect
              label="Sonuç"
              value={filters.status}
              options={["success", "error"]}
              onChange={(v) => setFilters((f) => ({ ...f, status: v }))}
            />
            {hasActiveFilter && (
              <button
                className="btn btn--sm btn--ghost"
                onClick={() => setFilters(EMPTY_FILTERS)}
              >
                ✕ Filtreleri temizle
              </button>
            )}
          </div>
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "1.25rem" }}>
        <BreakdownCard
          title="Client kaynağına göre"
          counts={data?.totals.by_client_source ?? {}}
          total={data?.totals.total ?? 0}
        />
        <BreakdownCard
          title="Tool'a göre"
          counts={data?.totals.by_tool ?? {}}
          total={data?.totals.total ?? 0}
          labelFn={safeLabel}
        />
      </div>

      <DetailTable rows={data?.rows ?? null} loading={loading && data === null} />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function SummaryCards({ totals }: { totals: TrafficTotals | null }): JSX.Element {
  const total = totals?.total ?? 0;
  const success = totals?.by_status.success ?? 0;
  const errorCount = totals?.by_status.error ?? 0;
  const failRate = total > 0 ? ((errorCount / total) * 100).toFixed(1) : "0.0";

  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: "1rem" }}>
      <StatCard label="Toplam istek" value={total.toLocaleString("tr-TR")} tone="brand" />
      <StatCard label="Başarılı" value={success.toLocaleString("tr-TR")} tone="success" />
      <StatCard label="Hatalı" value={errorCount.toLocaleString("tr-TR")} tone="danger" />
      <StatCard
        label="Hata oranı"
        value={`%${failRate}`}
        tone={Number(failRate) > 5 ? "danger" : "muted"}
      />
    </div>
  );
}

function StatCard({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: "brand" | "success" | "danger" | "muted";
}): JSX.Element {
  const color: Record<typeof tone, string> = {
    brand: "var(--brand-600)",
    success: "var(--success-700)",
    danger: "var(--danger-700)",
    muted: "var(--fg-muted)",
  };
  return (
    <div className="card">
      <div className="card__body">
        <div
          className="muted text-xs"
          style={{ textTransform: "uppercase", letterSpacing: "0.04em" }}
        >
          {label}
        </div>
        <div
          style={{ fontSize: "1.6rem", fontWeight: 700, color: color[tone], marginTop: 4 }}
        >
          {value}
        </div>
      </div>
    </div>
  );
}

function FilterSelect({
  label,
  value,
  options,
  onChange,
  renderOption,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (value: string) => void;
  renderOption?: (raw: string) => string;
}): JSX.Element {
  return (
    <label className="stack" style={{ gap: 4, minWidth: 180 }}>
      <span className="muted text-xs">{label}</span>
      <select
        className="select"
        value={value}
        onChange={(ev) => onChange(ev.target.value)}
      >
        <option value="">Tümü</option>
        {options.map((opt) => (
          <option key={opt} value={opt}>
            {renderOption ? renderOption(opt) : opt}
          </option>
        ))}
      </select>
    </label>
  );
}

function BreakdownCard({
  title,
  counts,
  total,
  labelFn,
}: {
  title: string;
  counts: Record<string, number>;
  total: number;
  labelFn?: (raw: string) => string;
}): JSX.Element {
  const entries = Object.entries(counts).sort((a, b) => b[1] - a[1]);
  const max = entries.length > 0 ? entries[0][1] : 0;

  return (
    <div className="card">
      <div className="card__header">
        <div className="card__title">{title}</div>
      </div>
      <div className="card__body card__body--flush">
        {entries.length === 0 ? (
          <div className="empty" style={{ padding: "1.5rem" }}>
            <div className="empty__title">Veri yok</div>
          </div>
        ) : (
          <table className="table">
            <tbody>
              {entries.map(([key, count]) => {
                const pct = total > 0 ? ((count / total) * 100).toFixed(1) : "0.0";
                const barWidth = max > 0 ? (count / max) * 100 : 0;
                return (
                  <tr key={key}>
                    <td style={{ width: "45%" }}>
                      <code className="text-xs">{labelFn ? labelFn(key) : key}</code>
                    </td>
                    <td style={{ width: "40%" }}>
                      <div
                        style={{
                          height: 8,
                          borderRadius: 4,
                          background: "var(--brand-600)",
                          width: `${barWidth}%`,
                          minWidth: count > 0 ? 4 : 0,
                        }}
                      />
                    </td>
                    <td
                      style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}
                    >
                      <strong>{count.toLocaleString("tr-TR")}</strong>{" "}
                      <span className="muted text-xs">%{pct}</span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

function DetailTable({
  rows,
  loading,
}: {
  rows: TrafficRow[] | null;
  loading: boolean;
}): JSX.Element {
  return (
    <div className="card">
      <div className="card__header">
        <div className="card__title">
          Detaylı sayaçlar{" "}
          {rows ? <span className="muted">({rows.length} satır)</span> : null}
        </div>
      </div>
      <div className="card__body card__body--flush">
        {loading ? (
          <div className="card__body">
            <div className="skeleton" style={{ height: 80 }} />
          </div>
        ) : rows && rows.length > 0 ? (
          <table className="table">
            <thead>
              <tr>
                <th>Client kaynağı</th>
                <th>Tool</th>
                <th>Sonuç</th>
                <th style={{ textAlign: "right" }}>İstek</th>
              </tr>
            </thead>
            <tbody>
              {[...rows]
                .sort((a, b) => b.count - a.count)
                .map((row, idx) => (
                  <tr key={`${row.client_source}|${row.tool}|${row.status}|${idx}`}>
                    <td>
                      <code className="text-xs">{row.client_source}</code>
                    </td>
                    <td>
                      <code className="text-xs">{safeLabel(row.tool)}</code>
                    </td>
                    <td>
                      <StatusBadge status={row.status} />
                    </td>
                    <td
                      style={{
                        textAlign: "right",
                        fontVariantNumeric: "tabular-nums",
                      }}
                    >
                      {row.count.toLocaleString("tr-TR")}
                    </td>
                  </tr>
                ))}
            </tbody>
          </table>
        ) : (
          <div className="empty" style={{ padding: "2rem" }}>
            <div className="empty__icon">📭</div>
            <div className="empty__title">Eşleşen MCP trafiği yok</div>
          </div>
        )}
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: string }): ReactNode {
  const isSuccess = status === "success";
  const isError = status === "error";
  const bg = isSuccess
    ? "var(--success-50)"
    : isError
      ? "var(--danger-50)"
      : "var(--bg-muted)";
  const fg = isSuccess
    ? "var(--success-700)"
    : isError
      ? "var(--danger-700)"
      : "var(--fg-muted)";
  return (
    <span
      style={{
        padding: "0.15rem 0.5rem",
        borderRadius: 4,
        background: bg,
        color: fg,
        fontSize: "0.75rem",
        fontWeight: 600,
      }}
    >
      {status}
    </span>
  );
}
