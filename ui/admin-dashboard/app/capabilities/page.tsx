"use client";

/**
 * Capability matrix page.
 *
 * Renders the dept × service connectivity grid for every department.
 * Cells are colour-coded:
 *
 * * 🟢 sağlıklı
 * * 🔴 sağlıksız
 * * ⚪ tanımlanmamış / bilinmiyor
 *
 * Clicking a cell opens a side panel with the last error, latency
 * and timestamp, plus a "Yeniden Test Et" button. The grid
 * auto-refreshes every 10 minutes.
 */

import {
  type CSSProperties,
  type ReactNode,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import { apiFetch } from "@/lib/api-client";

import {
  AUTO_REFRESH_INTERVAL_MS,
  STATUS_LABEL,
  SUPPORTED_SERVICES,
  applyCellUpdate,
  formatLatency,
  parseMatrix,
  parseProbeCell,
  statusColor,
} from "./_lib/matrix.mjs";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type ProbeStatus = "healthy" | "unhealthy" | "not_configured" | "unknown";

type ProbeCell = {
  dept_id: string;
  service: string;
  status: ProbeStatus;
  error: string | null;
  latency_ms: number | null;
  probed_at: string | null;
};

type DeptRow = {
  dept_id: string;
  display_name: string | null | undefined;
  services: Record<string, ProbeCell>;
};

type CapabilityMatrix = {
  departments: DeptRow[];
  supported_services: string[];
};

type Selection = {
  deptId: string;
  service: string;
};

// ---------------------------------------------------------------------------
// Cell colour palette
// ---------------------------------------------------------------------------

const CELL_COLOR: Record<"green" | "red" | "grey", { bg: string; fg: string; border: string }> = {
  green: { bg: "var(--success-50)", fg: "var(--success-700)", border: "var(--success-100)" },
  red:   { bg: "var(--danger-50)",  fg: "var(--danger-700)",  border: "var(--danger-100)" },
  grey:  { bg: "var(--bg-muted)",   fg: "var(--fg-muted)",    border: "var(--border)" },
};

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function CapabilitiesPage(): JSX.Element {
  const [matrix, setMatrix] = useState<CapabilityMatrix | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [reprobing, setReprobing] = useState(false);
  const [reprobeError, setReprobeError] = useState<string | null>(null);
  const [lastRefreshedAt, setLastRefreshedAt] = useState<Date | null>(null);

  const cancelledRef = useRef(false);

  const loadMatrix = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch("/api/v1/departments/capabilities");
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status}${text ? `: ${text.slice(0, 200)}` : ""}`);
      }
      const raw = await res.json();
      const parsed = parseMatrix(raw) as CapabilityMatrix;
      if (!cancelledRef.current) {
        setMatrix(parsed);
        setLastRefreshedAt(new Date());
      }
    } catch (err) {
      if (!cancelledRef.current) setError((err as Error).message);
    } finally {
      if (!cancelledRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    cancelledRef.current = false;
    void loadMatrix();
    const id = setInterval(() => {
      void loadMatrix();
    }, AUTO_REFRESH_INTERVAL_MS);
    return () => {
      cancelledRef.current = true;
      clearInterval(id);
    };
  }, [loadMatrix]);

  const handleCellClick = useCallback((deptId: string, service: string) => {
    setReprobeError(null);
    setSelection({ deptId, service });
  }, []);

  const handleClosePanel = useCallback(() => {
    setSelection(null);
    setReprobeError(null);
  }, []);

  const handleReprobe = useCallback(async (deptId: string, service: string) => {
    setReprobing(true);
    setReprobeError(null);
    try {
      const res = await apiFetch(
        `/api/v1/departments/${encodeURIComponent(deptId)}/probe/${encodeURIComponent(service)}`,
        { method: "POST", body: JSON.stringify({}) },
      );
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(`HTTP ${res.status}${text ? `: ${text.slice(0, 200)}` : ""}`);
      }
      const raw = await res.json();
      const updated = parseProbeCell(raw, deptId, service) as ProbeCell;
      setMatrix((prev) => (prev ? (applyCellUpdate(prev, updated) as CapabilityMatrix) : prev));
    } catch (err) {
      setReprobeError((err as Error).message);
    } finally {
      setReprobing(false);
    }
  }, []);

  const selectedCell: ProbeCell | null = (() => {
    if (!selection || !matrix) return null;
    const dept = matrix.departments.find((d) => d.dept_id === selection.deptId);
    if (!dept) return null;
    return dept.services[selection.service] ?? null;
  })();

  return (
    <div className="stack stack--lg">
      <header className="page-header">
        <div className="page-header__title-row">
          <div>
            <h1>Yetenek matrisi</h1>
            <p className="page-header__lede">
              Her departmanın Jira, Bitbucket, Confluence, LLM, SSH ve
              Docker servislerine bağlanma durumu. Hücreye tıklayarak son
              hatayı görebilir ve yeniden test edebilirsiniz.
            </p>
          </div>
          <div className="page-header__actions">
            <button className="btn" onClick={loadMatrix} disabled={loading}>
              {loading ? <span className="spinner" /> : "🔄"} Yenile
            </button>
          </div>
        </div>
        {lastRefreshedAt && (
          <div className="muted text-xs" style={{ marginTop: 6 }}>
            Son yenileme: {lastRefreshedAt.toLocaleTimeString()} · 10 dakikada bir otomatik
          </div>
        )}
      </header>

      {error && (
        <div className="banner banner--danger" role="alert">
          <span className="banner__icon">⚠️</span>
          <div className="banner__body">{error}</div>
        </div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: selection ? "1fr 320px" : "1fr", gap: "1.25rem" }}>
        <MatrixGrid matrix={matrix} selection={selection} onCellClick={handleCellClick} />

        {selection && (
          <DetailPanel
            cell={selectedCell}
            deptId={selection.deptId}
            service={selection.service}
            reprobing={reprobing}
            reprobeError={reprobeError}
            onReprobe={() => handleReprobe(selection.deptId, selection.service)}
            onClose={handleClosePanel}
          />
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function MatrixGrid({
  matrix,
  selection,
  onCellClick,
}: {
  matrix: CapabilityMatrix | null;
  selection: Selection | null;
  onCellClick: (deptId: string, service: string) => void;
}): JSX.Element {
  if (matrix === null) {
    return (
      <div className="card">
        <div className="card__body"><div className="skeleton" style={{ height: 100 }} /></div>
      </div>
    );
  }
  if (matrix.departments.length === 0) {
    return (
      <div className="card">
        <div className="card__body">
          <div className="empty">
            <div className="empty__icon">🏢</div>
            <div className="empty__title">Yapılandırılmış departman yok</div>
          </div>
        </div>
      </div>
    );
  }

  const services = matrix.supported_services.length > 0
    ? matrix.supported_services
    : [...SUPPORTED_SERVICES];

  return (
    <div className="card">
      <div className="card__body card__body--flush">
        <table className="table">
          <thead>
            <tr>
              <th>Departman</th>
              {services.map((svc) => (
                <th key={svc} style={{ textAlign: "center" }}>{svc}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {matrix.departments.map((dept) => (
              <tr key={dept.dept_id}>
                <td>
                  <div>{dept.display_name ?? dept.dept_id}</div>
                  <code className="text-xs muted">{dept.dept_id}</code>
                </td>
                {services.map((svc) => {
                  const cell = dept.services[svc];
                  const isSelected =
                    selection !== null &&
                    selection.deptId === dept.dept_id &&
                    selection.service === svc;
                  return (
                    <CellButton
                      key={svc}
                      cell={cell}
                      selected={isSelected}
                      onClick={() => onCellClick(dept.dept_id, svc)}
                    />
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function CellButton({
  cell,
  selected,
  onClick,
}: {
  cell: ProbeCell | undefined;
  selected: boolean;
  onClick: () => void;
}): JSX.Element {
  const cellStyle: CSSProperties = { padding: "0.4rem", textAlign: "center" };

  if (!cell) {
    const palette = CELL_COLOR.grey;
    return (
      <td style={cellStyle}>
        <div
          style={{
            padding: "0.35rem 0.4rem",
            background: palette.bg,
            color: palette.fg,
            border: `1px solid ${palette.border}`,
            borderRadius: 6,
            fontSize: "0.78rem",
          }}
          title="hücre verisi yok"
        >
          ?
        </div>
      </td>
    );
  }

  const palette = CELL_COLOR[statusColor(cell.status) as "green" | "red" | "grey"];
  const label = STATUS_LABEL[cell.status];

  return (
    <td style={cellStyle}>
      <button
        type="button"
        onClick={(ev) => {
          ev.stopPropagation();
          onClick();
        }}
        aria-label={`${cell.dept_id} ${cell.service}: ${label}`}
        title={label}
        style={{
          width: "100%",
          padding: "0.4rem 0.3rem",
          background: palette.bg,
          color: palette.fg,
          border: `${selected ? "2px" : "1px"} solid ${selected ? "var(--brand-600)" : palette.border}`,
          borderRadius: 6,
          cursor: "pointer",
          fontWeight: 600,
          fontSize: "0.75rem",
          letterSpacing: "0.02em",
          transition: "transform 120ms",
        }}
      >
        {label}
      </button>
    </td>
  );
}

function DetailPanel({
  cell,
  deptId,
  service,
  reprobing,
  reprobeError,
  onReprobe,
  onClose,
}: {
  cell: ProbeCell | null;
  deptId: string;
  service: string;
  reprobing: boolean;
  reprobeError: string | null;
  onReprobe: () => void;
  onClose: () => void;
}): JSX.Element {
  return (
    <aside className="card" aria-label="Hücre detayı">
      <div className="card__header">
        <div className="card__title">
          {deptId} <span className="muted">/</span> {service}
        </div>
        <button onClick={onClose} aria-label="Kapat" className="btn btn--sm btn--ghost btn--icon">✕</button>
      </div>
      <div className="card__body">
        {cell === null ? (
          <p className="muted">Hücre verisi bulunamadı.</p>
        ) : (
          <DetailRows cell={cell} />
        )}

        <button
          type="button"
          onClick={onReprobe}
          disabled={reprobing}
          className="btn btn--primary"
          style={{ marginTop: "0.75rem", width: "100%" }}
        >
          {reprobing ? <span className="spinner" /> : "🔁"} Yeniden test et
        </button>

        {reprobeError && (
          <p className="text-sm" style={{ color: "var(--danger-700)", marginTop: 8 }} role="alert">
            Hata: {reprobeError}
          </p>
        )}
      </div>
    </aside>
  );
}

function DetailRows({ cell }: { cell: ProbeCell }): JSX.Element {
  return (
    <dl style={{ margin: 0 }}>
      <Row label="Durum">{STATUS_LABEL[cell.status]}</Row>
      <Row label="Son hata">
        {cell.error ? (
          <code style={{ wordBreak: "break-word" }}>{cell.error}</code>
        ) : (
          <span className="faint">—</span>
        )}
      </Row>
      <Row label="Gecikme">{formatLatency(cell.latency_ms)}</Row>
      <Row label="Son test">
        {cell.probed_at ? <code className="text-xs">{cell.probed_at}</code> : <span className="faint">—</span>}
      </Row>
    </dl>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }): JSX.Element {
  return (
    <div className="row" style={{ marginBottom: 8 }}>
      <dt className="muted" style={{ minWidth: 80, fontSize: "0.8rem" }}>{label}</dt>
      <dd style={{ margin: 0, flex: 1, fontSize: "0.85rem" }}>{children}</dd>
    </div>
  );
}
