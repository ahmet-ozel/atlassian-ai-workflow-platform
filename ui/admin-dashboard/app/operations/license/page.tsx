"use client";

/**
 * Operations > License sayfası.
 *
 * `GET /admin/operations/license` endpoint'inden bot license cap verilerini çeker;
 * her license için:
 *   - Bar chart: concurrent / daily / monthly kullanım vs. maksimum.
 *   - 30 günlük trend line chart (GET /admin/operations/license/{id}/trend).
 *
 */

import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api-client";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type LicenseEntry = {
  license_id: string;
  max_concurrent: number;
  current_concurrent: number;
  daily_used: number;
  daily_max: number;
  monthly_token_usd_used: number;
  monthly_token_usd_max: number;
  percent_used: number;
};

type TrendPoint = {
  day: string;       // ISO date string "YYYY-MM-DD"
  workflows: number; // daily workflow count
};

type TrendData = {
  license_id: string;
  trend: TrendPoint[];
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

function pct(used: number, max: number): number {
  if (max <= 0) return 0;
  return clamp((used / max) * 100, 0, 100);
}

function barColor(percent: number): string {
  if (percent >= 90) return "#dc2626"; // kırmızı
  if (percent >= 70) return "#f59e0b"; // sarı
  return "#16a34a";                    // yeşil
}

function formatUsd(value: number): string {
  return value.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

// ---------------------------------------------------------------------------
// SVG Bar Chart - tek bir metrik için yatay bar
// ---------------------------------------------------------------------------

type BarProps = {
  label: string;
  used: number;
  max: number;
  formatValue?: (v: number) => string;
};

function MetricBar({ label, used, max, formatValue }: BarProps) {
  const percent = pct(used, max);
  const color = barColor(percent);
  const fmt = formatValue ?? ((v: number) => String(v));

  return (
    <div style={{ marginBottom: "0.75rem" }}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: "0.85rem",
          marginBottom: "0.25rem",
          color: "#374151",
        }}
      >
        <span>{label}</span>
        <span>
          {fmt(used)} / {fmt(max)}{" "}
          <span style={{ color: "#6b7280" }}>({percent.toFixed(1)}%)</span>
        </span>
      </div>
      <div
        role="progressbar"
        aria-valuenow={percent}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`${label}: ${percent.toFixed(1)}%`}
        style={{
          height: "1rem",
          background: "#e5e7eb",
          borderRadius: "0.25rem",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            width: `${percent}%`,
            height: "100%",
            background: color,
            borderRadius: "0.25rem",
            transition: "width 0.3s ease",
          }}
        />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// SVG Line Chart - 30 günlük trend
// ---------------------------------------------------------------------------

const CHART_W = 480;
const CHART_H = 120;
const CHART_PAD = { top: 10, right: 10, bottom: 30, left: 40 };

type LineChartProps = {
  points: TrendPoint[];
};

function TrendLineChart({ points }: LineChartProps) {
  if (points.length === 0) {
    return (
      <p style={{ color: "#9ca3af", fontSize: "0.85rem" }}>
        Trend verisi yok.
      </p>
    );
  }

  const innerW = CHART_W - CHART_PAD.left - CHART_PAD.right;
  const innerH = CHART_H - CHART_PAD.top - CHART_PAD.bottom;

  const maxVal = Math.max(...points.map((p) => p.workflows), 1);
  const n = points.length;

  const xScale = (i: number) =>
    CHART_PAD.left + (i / Math.max(n - 1, 1)) * innerW;
  const yScale = (v: number) =>
    CHART_PAD.top + innerH - (v / maxVal) * innerH;

  const polylinePoints = points
    .map((p, i) => `${xScale(i)},${yScale(p.workflows)}`)
    .join(" ");

  // X-axis labels: show first, middle, last
  const labelIndices = [0, Math.floor((n - 1) / 2), n - 1].filter(
    (v, i, arr) => arr.indexOf(v) === i,
  );

  // Y-axis ticks: 0, half, max
  const yTicks = [0, Math.round(maxVal / 2), maxVal];

  return (
    <svg
      width={CHART_W}
      height={CHART_H}
      viewBox={`0 0 ${CHART_W} ${CHART_H}`}
      aria-label="30 günlük workflow trend grafiği"
      role="img"
      style={{ maxWidth: "100%", display: "block" }}
    >
      {/* Y-axis grid lines + labels */}
      {yTicks.map((tick) => {
        const y = yScale(tick);
        return (
          <g key={tick}>
            <line
              x1={CHART_PAD.left}
              y1={y}
              x2={CHART_PAD.left + innerW}
              y2={y}
              stroke="#e5e7eb"
              strokeWidth={1}
            />
            <text
              x={CHART_PAD.left - 4}
              y={y + 4}
              textAnchor="end"
              fontSize={10}
              fill="#6b7280"
            >
              {tick}
            </text>
          </g>
        );
      })}

      {/* X-axis labels */}
      {labelIndices.map((i) => {
        const p = points[i];
        const x = xScale(i);
        const label = p.day.slice(5); // "MM-DD"
        return (
          <text
            key={i}
            x={x}
            y={CHART_H - 6}
            textAnchor="middle"
            fontSize={10}
            fill="#6b7280"
          >
            {label}
          </text>
        );
      })}

      {/* Area fill */}
      <polygon
        points={[
          `${xScale(0)},${CHART_PAD.top + innerH}`,
          ...points.map((p, i) => `${xScale(i)},${yScale(p.workflows)}`),
          `${xScale(n - 1)},${CHART_PAD.top + innerH}`,
        ].join(" ")}
        fill="#dbeafe"
        opacity={0.6}
      />

      {/* Line */}
      <polyline
        points={polylinePoints}
        fill="none"
        stroke="#2563eb"
        strokeWidth={2}
        strokeLinejoin="round"
        strokeLinecap="round"
      />

      {/* Data points */}
      {points.map((p, i) => (
        <circle
          key={i}
          cx={xScale(i)}
          cy={yScale(p.workflows)}
          r={3}
          fill="#2563eb"
        >
          <title>
            {p.day}: {p.workflows} workflow
          </title>
        </circle>
      ))}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// License Card
// ---------------------------------------------------------------------------

type LicenseCardProps = {
  entry: LicenseEntry;
};

function LicenseCard({ entry }: LicenseCardProps) {
  const [trend, setTrend] = useState<TrendPoint[] | null>(null);
  const [trendError, setTrendError] = useState<string | null>(null);
  const [trendLoading, setTrendLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setTrendLoading(true);
    setTrendError(null);

    apiFetch(
      `/admin/operations/license/${encodeURIComponent(entry.license_id)}/trend`,
    )
      .then(async (res) => {
        if (cancelled) return;
        if (!res.ok) {
          const text = await res.text().catch(() => "");
          setTrendError(
            `Trend yüklenemedi: HTTP ${res.status}${text ? ` - ${text.slice(0, 120)}` : ""}`,
          );
          return;
        }
        const data = (await res.json()) as TrendData;
        if (!cancelled) setTrend(data.trend ?? []);
      })
      .catch((err: unknown) => {
        if (!cancelled)
          setTrendError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setTrendLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [entry.license_id]);

  const overallPct = entry.percent_used;
  const overallColor = barColor(overallPct);

  return (
    <article
      aria-label={`License: ${entry.license_id}`}
      style={{
        border: "1px solid #e5e7eb",
        borderRadius: "0.5rem",
        padding: "1.25rem",
        background: "#ffffff",
        marginBottom: "1.5rem",
        boxShadow: "0 1px 3px rgba(0,0,0,0.06)",
      }}
    >
      {/* Header */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: "1rem",
          flexWrap: "wrap",
          gap: "0.5rem",
        }}
      >
        <h2 style={{ margin: 0, fontSize: "1.1rem", color: "#111827" }}>
          <code>{entry.license_id}</code>
        </h2>
        <span
          style={{
            display: "inline-block",
            padding: "0.2rem 0.6rem",
            borderRadius: "9999px",
            fontSize: "0.8rem",
            fontWeight: 600,
            background: overallColor,
            color: "#ffffff",
          }}
          aria-label={`Genel kullanım: ${overallPct.toFixed(1)}%`}
        >
          {overallPct.toFixed(1)}% kullanımda
        </span>
      </div>

      {/* Bar charts */}
      <section aria-label="Kullanım metrikleri">
        <MetricBar
          label="Eş zamanlı workflow"
          used={entry.current_concurrent}
          max={entry.max_concurrent}
        />
        <MetricBar
          label="Günlük workflow"
          used={entry.daily_used}
          max={entry.daily_max}
        />
        <MetricBar
          label="Aylık token maliyeti"
          used={entry.monthly_token_usd_used}
          max={entry.monthly_token_usd_max}
          formatValue={formatUsd}
        />
      </section>

      {/* Trend chart */}
      <section
        aria-label="Son 30 gün trend"
        style={{ marginTop: "1.25rem" }}
      >
        <h3
          style={{
            margin: "0 0 0.5rem",
            fontSize: "0.9rem",
            color: "#374151",
            fontWeight: 600,
          }}
        >
          Son 30 gün - günlük workflow sayısı
        </h3>
        {trendLoading && (
          <p style={{ color: "#9ca3af", fontSize: "0.85rem" }}>
            Trend yükleniyor…
          </p>
        )}
        {trendError && (
          <p
            role="alert"
            style={{ color: "#b91c1c", fontSize: "0.85rem" }}
          >
            {trendError}
          </p>
        )}
        {!trendLoading && !trendError && trend !== null && (
          <TrendLineChart points={trend} />
        )}
      </section>
    </article>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function LicensePage(): JSX.Element {
  const [licenses, setLicenses] = useState<LicenseEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch("/admin/operations/license");
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(
          `GET /admin/operations/license → HTTP ${res.status}${
            text ? `: ${text.slice(0, 200)}` : ""
          }`,
        );
      }
      const data = (await res.json()) as LicenseEntry[];
      setLicenses(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return (
    <main style={{ padding: "1.5rem", fontFamily: "system-ui, sans-serif" }}>
      <header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: "1.5rem",
          gap: "1rem",
          flexWrap: "wrap",
        }}
      >
        <div>
          <h1 style={{ margin: 0, fontSize: "1.5rem", color: "#111827" }}>
            Operations &rsaquo; License
          </h1>
          <p
            style={{
              margin: "0.25rem 0 0",
              color: "#6b7280",
              fontSize: "0.9rem",
            }}
          >
            Bot lisans kapasitesi ve kullanım durumu
          </p>
        </div>
        <button
          type="button"
          onClick={refresh}
          disabled={loading}
          style={{
            padding: "0.4rem 0.9rem",
            fontSize: "0.9rem",
            border: "1px solid #2563eb",
            color: "#2563eb",
            background: "#ffffff",
            borderRadius: "0.25rem",
            cursor: loading ? "not-allowed" : "pointer",
            opacity: loading ? 0.6 : 1,
          }}
        >
          {loading ? "Yükleniyor…" : "Yenile"}
        </button>
      </header>

      {error && (
        <div
          role="alert"
          style={{
            background: "#fee2e2",
            color: "#7f1d1d",
            padding: "0.75rem 1rem",
            borderRadius: "0.375rem",
            marginBottom: "1.5rem",
            fontSize: "0.9rem",
          }}
        >
          {error}
        </div>
      )}

      {loading && licenses === null && (
        <p style={{ color: "#6b7280" }}>Lisans verileri yükleniyor…</p>
      )}

      {!loading && licenses !== null && licenses.length === 0 && (
        <p style={{ color: "#6b7280" }}>
          Henüz tanımlı bot lisansı yok.
        </p>
      )}

      {licenses !== null &&
        licenses.map((entry) => (
          <LicenseCard key={entry.license_id} entry={entry} />
        ))}
    </main>
  );
}
