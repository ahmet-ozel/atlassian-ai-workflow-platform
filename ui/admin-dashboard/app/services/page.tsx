"use client";

/**
 * Servis Kataloğu sayfası — admin-dashboard-control-plane spec, design §3.10.
 *
 * Tablo halinde Service_Manifest'ten dönen tüm Managed_Service satırlarını
 * gösterir. Polling intervalinde state sütununu otomatik tazeler; manuel
 * "Refresh" düğmesi de sunar (Requirement 4.6, 12.1, 12.2, 12.3).
 *
 * Eylem düğmeleri (`Start`, `Stop`, `Restart`, `View Logs`, `Run Tests`)
 * mevcut state'e göre `disabled` durumunda render edilir; gizlenmez
 * (Requirement 4.3). `Start` modal `StartFormModal` ile,
 * `View Logs` `LogsViewer` ile, `Run Tests` `TestRunnerPanel` ile
 * eşleşir.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { apiFetch } from "@/lib/api-client";

import ExternalProvidersSection from "./_components/ExternalProvidersSection";
import LogsViewer from "./_components/LogsViewer";
import McpSetupTab from "./_components/McpSetupTab";
import StartFormModal from "./_components/StartFormModal";
import StateBadge, { type ServiceState } from "./_components/StateBadge";
import StopConfirmationModal from "./_components/StopConfirmationModal";
import TestRunnerPanel from "./_components/TestRunnerPanel";
import WorkspacesTab from "./_components/WorkspacesTab";

// --------------------------------------------------------------------------
// Types
// --------------------------------------------------------------------------

type ServiceKind = "http_service" | "worker" | "ui" | "infra";

type HealthSnapshot = {
  ts: string;
  healthz_status: number;
  healthz_body: string;
  readyz_status: number | null;
  readyz_body: string | null;
  state: "healthy" | "unhealthy" | "unknown";
};

type ServiceSummary = {
  name: string;
  kind: ServiceKind;
  state: ServiceState;
  last_started_at: string | null;
  last_health_snapshot: HealthSnapshot | null;
  feature_flag_dependency?: string[];
};

type ActionKind = "start" | "stop" | "restart";

// --------------------------------------------------------------------------
// Polling interval
// --------------------------------------------------------------------------

const DEFAULT_POLL_INTERVAL = 10;
const MIN_POLL_INTERVAL = 5;
const MAX_POLL_INTERVAL = 30;

function resolvePollInterval(): number {
  const raw = process.env.NEXT_PUBLIC_HEALTH_POLL_INTERVAL_SECONDS ?? "10";
  const parsed = Number(raw);
  if (
    Number.isFinite(parsed) &&
    parsed >= MIN_POLL_INTERVAL &&
    parsed <= MAX_POLL_INTERVAL
  ) {
    return parsed;
  }
  // eslint-disable-next-line no-console
  console.warn(
    `NEXT_PUBLIC_HEALTH_POLL_INTERVAL_SECONDS=${raw} out of [${MIN_POLL_INTERVAL},${MAX_POLL_INTERVAL}]; using ${DEFAULT_POLL_INTERVAL}`,
  );
  return DEFAULT_POLL_INTERVAL;
}

// --------------------------------------------------------------------------
// Action availability
// --------------------------------------------------------------------------

function actionEnabled(state: ServiceState, action: ActionKind): boolean {
  switch (action) {
    case "start":
      return state === "stopped" || state === "failed";
    case "stop":
      return (
        state === "running" ||
        state === "running_unmonitored" ||
        state === "unhealthy" ||
        state === "starting"
      );
    case "restart":
      return state === "running" || state === "running_unmonitored" || state === "unhealthy";
  }
}

function disabledReason(state: ServiceState, action: ActionKind): string {
  if (actionEnabled(state, action)) return "";
  switch (action) {
    case "start":
      return `Cannot start while state=${state}; only stopped or failed services can start.`;
    case "stop":
      return `Cannot stop while state=${state}; only running, unhealthy, or starting services can stop.`;
    case "restart":
      return `Cannot restart while state=${state}; only running or unhealthy services can restart.`;
  }
}

// --------------------------------------------------------------------------
// Feature flag badges/modal
// --------------------------------------------------------------------------

function FlagGatedBadge({ flags }: { flags: string[] }) {
  if (flags.length === 0) return null;
  return (
    <span
      className="badge"
      title={`Bu servis aşağıdaki feature flag'lere bağlıdır: ${flags.join(", ")}`}
      style={{ marginLeft: 6 }}
    >
      🏳️ {flags.join(", ")}
    </span>
  );
}

type FeatureFlagDisabledModalProps = {
  blockingFlag: string;
  onClose: () => void;
};

function FeatureFlagDisabledModal({ blockingFlag, onClose }: FeatureFlagDisabledModalProps) {
  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      if (ev.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="modal-backdrop"
      role="presentation"
      onMouseDown={(ev) => {
        if (ev.target === ev.currentTarget) onClose();
      }}
    >
      <div
        className="modal"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="ff-disabled-title"
        aria-describedby="ff-disabled-desc"
        style={{ padding: "1.5rem", width: "min(480px, 92vw)" }}
      >
        <h2 id="ff-disabled-title" style={{ color: "var(--warn-700)" }}>Feature flag kapalı</h2>
        <p id="ff-disabled-desc" style={{ marginTop: 8 }}>
          Önce <a href="/feature-flags">Feature Flags</a> sayfasından{" "}
          <code>{blockingFlag}</code> bayrağını açın.
        </p>
        <div className="row--between" style={{ marginTop: "1rem" }}>
          <a className="btn btn--primary btn--sm" href="/feature-flags">Feature Flags&apos;e git</a>
          <button type="button" className="btn btn--sm" onClick={onClose}>Kapat</button>
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function unhealthyExcerpt(snapshot: HealthSnapshot | null): string | null {
  if (!snapshot) return null;
  const source =
    snapshot.healthz_body && snapshot.healthz_body.length > 0
      ? snapshot.healthz_body
      : snapshot.readyz_body ?? "";
  const trimmed = source.slice(0, 200);
  return trimmed.length > 0 ? trimmed : null;
}

// --------------------------------------------------------------------------
// Data hook
// --------------------------------------------------------------------------

type ListState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ok"; rows: ServiceSummary[]; lastRefreshed: Date }
  | { kind: "error"; message: string };

function useServiceCatalog(pollIntervalSec: number) {
  const [state, setState] = useState<ListState>({ kind: "idle" });
  const cancelledRef = useRef(false);

  const fetchOnce = useCallback(async () => {
    try {
      const res = await apiFetch("/admin/services");
      if (cancelledRef.current) return;
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        setState({
          kind: "error",
          message: `GET /admin/services → HTTP ${res.status}${
            text ? `: ${text.slice(0, 200)}` : ""
          }`,
        });
        return;
      }
      const rows = (await res.json()) as ServiceSummary[];
      if (cancelledRef.current) return;
      setState({ kind: "ok", rows, lastRefreshed: new Date() });
    } catch (err) {
      if (cancelledRef.current) return;
      const message = err instanceof Error ? err.message : String(err);
      setState({ kind: "error", message });
    }
  }, []);

  useEffect(() => {
    cancelledRef.current = false;
    setState({ kind: "loading" });
    void fetchOnce();
    const id = window.setInterval(() => {
      void fetchOnce();
    }, pollIntervalSec * 1000);
    return () => {
      cancelledRef.current = true;
      window.clearInterval(id);
    };
  }, [pollIntervalSec, fetchOnce]);

  const refresh = useCallback(() => {
    void fetchOnce();
  }, [fetchOnce]);

  return { state, refresh };
}

// --------------------------------------------------------------------------
// Restart action
// --------------------------------------------------------------------------

async function invokeRestart(serviceName: string): Promise<void> {
  const res = await apiFetch(`/admin/services/${serviceName}/restart`, {
    method: "POST",
    body: JSON.stringify({}),
  });
  if (!res.ok && res.status !== 202) {
    const text = await res.text().catch(() => "");
    throw new Error(
      `restart ${serviceName} → HTTP ${res.status}${
        text ? `: ${text.slice(0, 200)}` : ""
      }`,
    );
  }
}

// --------------------------------------------------------------------------
// Modal coordination
// --------------------------------------------------------------------------

type ModalState =
  | { kind: "none" }
  | { kind: "start"; serviceName: string }
  | { kind: "stop"; serviceName: string }
  | { kind: "logs"; serviceName: string }
  | { kind: "tests"; serviceName: string }
  | { kind: "feature_flag_disabled"; blockingFlag: string };

type MainTab = "services" | "workspaces" | "mcp";

const TAB_DEFINITIONS: ReadonlyArray<{ id: MainTab; label: string; icon: string }> = [
  { id: "services", label: "Servisler", icon: "SV" },
  { id: "workspaces", label: "Workspaces", icon: "WS" },
  { id: "mcp", label: "MCP Kurulum", icon: "MC" },
];

// --------------------------------------------------------------------------
// Service row
// --------------------------------------------------------------------------

type ServiceRowProps = {
  svc: ServiceSummary;
  onStart: (name: string) => void;
  onStop: (name: string) => void;
  onRestart: (name: string) => void;
  onViewLogs: (name: string) => void;
  onRunTests: (name: string) => void;
  busy: boolean;
};

function ServiceRow({
  svc,
  onStart,
  onStop,
  onRestart,
  onViewLogs,
  onRunTests,
  busy,
}: ServiceRowProps) {
  const [expanded, setExpanded] = useState(false);
  const excerpt = unhealthyExcerpt(svc.last_health_snapshot);
  const showUnhealthyDetail = svc.state === "unhealthy" && excerpt !== null;

  const startEnabled = actionEnabled(svc.state, "start") && !busy;
  const stopEnabled = actionEnabled(svc.state, "stop") && !busy;
  const restartEnabled = actionEnabled(svc.state, "restart") && !busy;
  const runTestsEnabled =
    (svc.state === "running" || svc.state === "running_unmonitored") && !busy;

  return (
    <>
      <tr>
        <td>
          <a
            href={`/services/${encodeURIComponent(svc.name)}`}
            className="mono text-sm"
            title={`Detay: ${svc.name}`}
          >
            {svc.name}
          </a>
          {svc.feature_flag_dependency && svc.feature_flag_dependency.length > 0 && (
            <FlagGatedBadge flags={svc.feature_flag_dependency} />
          )}
        </td>
        <td className="text-sm">{svc.kind}</td>
        <td>
          <StateBadge state={svc.state} />
          {showUnhealthyDetail && (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="btn btn--sm btn--ghost"
              style={{ marginLeft: 6 }}
              aria-expanded={expanded}
              aria-controls={`unhealthy-detail-${svc.name}`}
            >
              {expanded ? "Gizle" : "Neden?"}
            </button>
          )}
        </td>
        <td className="muted text-sm">
          {formatTimestamp(svc.last_health_snapshot?.ts ?? null)}
        </td>
        <td className="muted text-sm">{formatTimestamp(svc.last_started_at)}</td>
        <td>
          <div className="row" style={{ gap: 6 }}>
            <ActionButton label="Start" enabled={startEnabled} disabledReason={disabledReason(svc.state, "start")} onClick={() => onStart(svc.name)} />
            <ActionButton label="Stop" enabled={stopEnabled} disabledReason={disabledReason(svc.state, "stop")} onClick={() => onStop(svc.name)} />
            <ActionButton label="Restart" enabled={restartEnabled} disabledReason={disabledReason(svc.state, "restart")} onClick={() => onRestart(svc.name)} />
            <ActionButton label="Loglar" enabled={!busy} disabledReason="" onClick={() => onViewLogs(svc.name)} />
            <ActionButton
              label="Test"
              enabled={runTestsEnabled}
              disabledReason={runTestsEnabled ? "" : `Tests require state=running; current state=${svc.state}.`}
              onClick={() => onRunTests(svc.name)}
            />
          </div>
        </td>
      </tr>
      {showUnhealthyDetail && expanded && (
        <tr>
          <td
            colSpan={6}
            id={`unhealthy-detail-${svc.name}`}
            style={{
              background: "var(--warn-50)",
              fontFamily: '"JetBrains Mono", ui-monospace, monospace',
              fontSize: "0.8rem",
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
            }}
          >
            <strong>Son sağlık görüntüsü (ilk 200 karakter):</strong>
            {"\n"}
            {excerpt}
          </td>
        </tr>
      )}
    </>
  );
}

// --------------------------------------------------------------------------
// Action button
// --------------------------------------------------------------------------

type ActionButtonProps = {
  label: string;
  enabled: boolean;
  disabledReason: string;
  onClick: () => void;
};

function ActionButton({ label, enabled, disabledReason, onClick }: ActionButtonProps) {
  const isDanger = label === "Stop";
  const className = `btn btn--sm${isDanger && enabled ? " btn--danger" : ""}`;
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={!enabled}
      title={enabled ? label : disabledReason}
      aria-disabled={!enabled}
      className={className}
    >
      {label}
    </button>
  );
}

// --------------------------------------------------------------------------
// Page
// --------------------------------------------------------------------------

export default function ServicesPage() {
  const [pollInterval, setPollInterval] = useState<number>(DEFAULT_POLL_INTERVAL);
  useEffect(() => {
    setPollInterval(resolvePollInterval());
  }, []);

  const { state, refresh } = useServiceCatalog(pollInterval);
  const [modal, setModal] = useState<ModalState>({ kind: "none" });
  const [busyServices, setBusyServices] = useState<Set<string>>(new Set());
  const [actionError, setActionError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<MainTab>("services");

  const setBusy = useCallback((name: string, busy: boolean) => {
    setBusyServices((prev) => {
      const next = new Set(prev);
      if (busy) next.add(name);
      else next.delete(name);
      return next;
    });
  }, []);

  const handleStart = useCallback((name: string) => {
    setActionError(null);
    setModal({ kind: "start", serviceName: name });
  }, []);

  const handleStop = useCallback((name: string) => {
    setActionError(null);
    setModal({ kind: "stop", serviceName: name });
  }, []);

  const handleStopConfirmed = useCallback(
    (name: string) => {
      setModal({ kind: "none" });
      setBusy(name, true);
      refresh();
      window.setTimeout(() => setBusy(name, false), 1500);
    },
    [refresh, setBusy],
  );

  const handleRestart = useCallback(
    async (name: string) => {
      setActionError(null);
      setBusy(name, true);
      try {
        await invokeRestart(name);
        refresh();
      } catch (err) {
        setActionError(err instanceof Error ? err.message : String(err));
      } finally {
        setBusy(name, false);
      }
    },
    [refresh, setBusy],
  );

  const handleViewLogs = useCallback((name: string) => {
    setActionError(null);
    setModal({ kind: "logs", serviceName: name });
  }, []);

  const handleRunTests = useCallback((name: string) => {
    setActionError(null);
    setModal({ kind: "tests", serviceName: name });
  }, []);

  const handleCloseModal = useCallback(() => setModal({ kind: "none" }), []);

  const handleFeatureFlagDisabled = useCallback((blockingFlag: string) => {
    setModal({ kind: "feature_flag_disabled", blockingFlag });
  }, []);

  const handleStartSubmitted = useCallback(
    (name: string) => {
      setModal({ kind: "none" });
      setBusy(name, true);
      refresh();
      window.setTimeout(() => setBusy(name, false), 1500);
    },
    [refresh, setBusy],
  );

  const lastRefreshedLabel = useMemo(() => {
    if (state.kind === "ok") return formatTimestamp(state.lastRefreshed.toISOString());
    return "—";
  }, [state]);

  const stats = useMemo(() => {
    if (state.kind !== "ok") return { total: 0, running: 0, failed: 0, unhealthy: 0 };
    const rows = state.rows;
    return {
      total: rows.length,
      running: rows.filter((r) => r.state === "running" || r.state === "running_unmonitored").length,
      failed: rows.filter((r) => r.state === "failed").length,
      unhealthy: rows.filter((r) => r.state === "unhealthy").length,
    };
  }, [state]);

  return (
    <div className="stack stack--lg">
      <header className="page-header">
        <div className="page-header__title-row">
          <div>
            <h1>Servisler</h1>
            <p className="page-header__lede">
              Manifest&apos;te tanımlı tüm yönetilen servisler. Health
              durumu her {pollInterval} saniyede bir tazelenir; son
              yenileme: {lastRefreshedLabel}.
            </p>
          </div>
          <div className="page-header__actions">
            <button className="btn" onClick={refresh}>
              Yenile
            </button>
          </div>
        </div>
      </header>

      <div className="stat-grid">
        <div className="stat-card">
          <div className="stat-card__label">Toplam servis</div>
          <div className="stat-card__value num">{stats.total}</div>
          <div className="stat-card__delta">Manifest kayıtları</div>
        </div>
        <div className="stat-card">
          <div className="stat-card__label">Çalışan</div>
          <div className="stat-card__value num" style={{ color: "var(--success-600)" }}>{stats.running}</div>
          <div className="stat-card__delta">Sağlıklı veya monitor dışı</div>
        </div>
        <div className="stat-card">
          <div className="stat-card__label">Sağlıksız</div>
          <div className="stat-card__value num" style={{ color: stats.unhealthy > 0 ? "var(--warn-600)" : undefined }}>{stats.unhealthy}</div>
          <div className="stat-card__delta">Health beklenenden düşük</div>
        </div>
        <div className="stat-card">
          <div className="stat-card__label">Hatalı</div>
          <div className="stat-card__value num" style={{ color: stats.failed > 0 ? "var(--danger-600)" : undefined }}>{stats.failed}</div>
          <div className="stat-card__delta">Müdahale gerekli</div>
        </div>
      </div>

      <div className="tabs" role="tablist" aria-label="Services panel sections">
        {TAB_DEFINITIONS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            role="tab"
            id={`services-tab-${tab.id}`}
            aria-selected={activeTab === tab.id}
            aria-controls={`services-tabpanel-${tab.id}`}
            tabIndex={activeTab === tab.id ? 0 : -1}
            onClick={() => setActiveTab(tab.id)}
            className={`tab${activeTab === tab.id ? " is-active" : ""}`}
          >
            <span aria-hidden style={{ marginRight: 6 }}>{tab.icon}</span>
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === "services" && (
        <div role="tabpanel" id="services-tabpanel-services" aria-labelledby="services-tab-services" className="stack stack--lg">
          <ExternalProvidersSection />

          {actionError && (
            <div className="banner banner--danger" role="alert">
              <span className="banner__icon">!</span>
              <div className="banner__body">{actionError}</div>
            </div>
          )}

          {state.kind === "loading" && (
            <div className="card">
              <div className="card__body"><div className="skeleton" style={{ height: 80 }} /></div>
            </div>
          )}
          {state.kind === "error" && (
            <div className="banner banner--warn" role="alert">
              <span className="banner__icon">!</span>
              <div className="banner__body">Servisler yüklenemedi: {state.message}</div>
            </div>
          )}

          {state.kind === "ok" && (
            <div className="card">
              <div className="card__body card__body--flush">
                {state.rows.length === 0 ? (
                  <div className="empty">
                    <div className="empty__icon">SV</div>
                    <div className="empty__title">Manifest&apos;te servis yok</div>
                  </div>
                ) : (
                  <table className="table">
                    <thead>
                      <tr>
                        <th>Ad</th>
                        <th>Tip</th>
                        <th>Durum</th>
                        <th>Son sağlık</th>
                        <th>Son başlatma</th>
                        <th>İşlemler</th>
                      </tr>
                    </thead>
                    <tbody>
                      {state.rows.map((svc) => (
                        <ServiceRow
                          key={svc.name}
                          svc={svc}
                          busy={busyServices.has(svc.name)}
                          onStart={handleStart}
                          onStop={handleStop}
                          onRestart={handleRestart}
                          onViewLogs={handleViewLogs}
                          onRunTests={handleRunTests}
                        />
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            </div>
          )}
        </div>
      )}

      {activeTab === "workspaces" && (
        <div role="tabpanel" id="services-tabpanel-workspaces" aria-labelledby="services-tab-workspaces">
          <WorkspacesTab />
        </div>
      )}

      {activeTab === "mcp" && (
        <div role="tabpanel" id="services-tabpanel-mcp" aria-labelledby="services-tab-mcp">
          <McpSetupTab />
        </div>
      )}

      {modal.kind === "start" && (
        <StartFormModal
          serviceName={modal.serviceName}
          onClose={handleCloseModal}
          onStarted={() => handleStartSubmitted(modal.serviceName)}
          onFeatureFlagDisabled={handleFeatureFlagDisabled}
        />
      )}
      {modal.kind === "stop" && (
        <StopConfirmationModal
          serviceName={modal.serviceName}
          onClose={handleCloseModal}
          onConfirmed={() => handleStopConfirmed(modal.serviceName)}
        />
      )}
      {modal.kind === "logs" && (
        <LogsViewer serviceName={modal.serviceName} onClose={handleCloseModal} />
      )}
      {modal.kind === "tests" && (
        <TestRunnerPanel serviceName={modal.serviceName} onClose={handleCloseModal} />
      )}
      {modal.kind === "feature_flag_disabled" && (
        <FeatureFlagDisabledModal
          blockingFlag={modal.blockingFlag}
          onClose={handleCloseModal}
        />
      )}
    </div>
  );
}
