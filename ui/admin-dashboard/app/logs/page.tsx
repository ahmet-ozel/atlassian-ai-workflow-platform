"use client";

import {
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useState,
  type CSSProperties,
} from "react";
import { useSearchParams } from "next/navigation";

import { apiFetch } from "@/lib/api-client";

type ServiceSummary = {
  name: string;
  kind: string;
  state: string;
};

type LogsResponse = {
  lines: string[];
};

const TAIL_OPTIONS = [50, 200, 500, 1000] as const;

function LogsPageInner(): JSX.Element {
  const searchParams = useSearchParams();
  const requestedService = searchParams.get("service") ?? "";
  const [services, setServices] = useState<ServiceSummary[]>([]);
  const [selectedService, setSelectedService] = useState(requestedService);
  const [tail, setTail] = useState<number>(200);
  const [lines, setLines] = useState<string[]>([]);
  const [status, setStatus] = useState<"idle" | "loading" | "ready" | "error">(
    "idle",
  );
  const [error, setError] = useState<string | null>(null);

  const sortedServices = useMemo(
    () => [...services].sort((a, b) => a.name.localeCompare(b.name)),
    [services],
  );

  const loadServices = useCallback(async () => {
    try {
      const response = await apiFetch("/admin/services");
      if (!response.ok) {
        const text = await response.text().catch(() => "");
        throw new Error(
          `GET /admin/services -> HTTP ${response.status}${
            text ? `: ${text.slice(0, 200)}` : ""
          }`,
        );
      }
      const payload = (await response.json()) as ServiceSummary[];
      setServices(Array.isArray(payload) ? payload : []);
      if (!selectedService && payload.length > 0) {
        setSelectedService(payload[0].name);
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
      setStatus("error");
    }
  }, [selectedService]);

  const loadLogs = useCallback(async () => {
    if (!selectedService) return;
    setStatus("loading");
    setError(null);
    try {
      const response = await apiFetch(
        `/admin/services/${encodeURIComponent(selectedService)}/logs?tail=${tail}`,
      );
      if (!response.ok) {
        const text = await response.text().catch(() => "");
        throw new Error(
          `GET logs -> HTTP ${response.status}${text ? `: ${text.slice(0, 300)}` : ""}`,
        );
      }
      const payload = (await response.json()) as LogsResponse;
      setLines(Array.isArray(payload.lines) ? payload.lines : []);
      setStatus("ready");
    } catch (exc) {
      setLines([]);
      setError(exc instanceof Error ? exc.message : String(exc));
      setStatus("error");
    }
  }, [selectedService, tail]);

  useEffect(() => {
    void loadServices();
  }, [loadServices]);

  useEffect(() => {
    if (requestedService) {
      setSelectedService(requestedService);
    }
  }, [requestedService]);

  useEffect(() => {
    void loadLogs();
  }, [loadLogs]);

  const selectedMeta = sortedServices.find((svc) => svc.name === selectedService);

  return (
    <div>
      <header className="page-header">
        <div className="page-header__title-row">
          <div>
            <h1>Loglar</h1>
            <p className="page-header__lede">
              Servis loglarini tek yerden secip izleyin. Hassas degerler API
              tarafinda maskelenerek dondurulur.
            </p>
          </div>
          <button type="button" className="btn btn--primary" onClick={loadLogs}>
            Refresh
          </button>
        </div>
      </header>

      <section className="card" aria-label="Log filtreleri">
        <div className="card__body">
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "minmax(220px, 1fr) 160px",
              gap: "1rem",
              alignItems: "end",
            }}
          >
            <label className="field" style={{ marginBottom: 0 }}>
              <span className="field__label">Servis</span>
              <select
                className="select"
                value={selectedService}
                onChange={(event) => setSelectedService(event.target.value)}
              >
                {sortedServices.length === 0 ? (
                  <option value="">Servis yok</option>
                ) : (
                  sortedServices.map((service) => (
                    <option key={service.name} value={service.name}>
                      {service.name}
                    </option>
                  ))
                )}
              </select>
            </label>
            <label className="field" style={{ marginBottom: 0 }}>
              <span className="field__label">Satir</span>
              <select
                className="select"
                value={tail}
                onChange={(event) => setTail(Number(event.target.value))}
              >
                {TAIL_OPTIONS.map((option) => (
                  <option key={option} value={option}>
                    {option}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {selectedMeta ? (
            <p className="field__hint" style={{ marginTop: "0.8rem" }}>
              Secili servis: {selectedMeta.name} / {selectedMeta.kind} /
              state={selectedMeta.state}
            </p>
          ) : null}
        </div>
      </section>

      <section className="card" style={{ marginTop: "1rem" }} aria-label="Log ciktilari">
        <div className="card__header">
          <div>
            <div className="card__title">{selectedService || "Servis secilmedi"}</div>
            <div className="card__sub">docker compose logs --tail {tail}</div>
          </div>
          <span className={`badge ${status === "error" ? "badge--danger" : "badge--info"}`}>
            {status}
          </span>
        </div>
        <div className="card__body">
          {error ? (
            <div role="alert" style={errorStyle}>
              {error}
            </div>
          ) : null}
          <div role="log" aria-live="polite" style={terminalStyle}>
            {status === "loading" ? (
              <span style={{ color: "#9ca3af" }}>Loading logs...</span>
            ) : lines.length === 0 ? (
              <span style={{ color: "#9ca3af" }}>No log lines returned.</span>
            ) : (
              lines.map((line, index) => (
                <div key={`${index}-${line.slice(0, 24)}`} style={lineStyle}>
                  {cleanLogLine(line)}
                </div>
              ))
            )}
          </div>
        </div>
      </section>
    </div>
  );
}

function cleanLogLine(line: string): string {
  return line.replace(/\x1B\[[0-?]*[ -/]*[@-~]/g, "");
}

export default function LogsPage(): JSX.Element {
  return (
    <Suspense
      fallback={
        <div className="stack">
          <div className="skeleton" style={{ height: 120 }} />
          <div className="skeleton" style={{ height: 420 }} />
        </div>
      }
    >
      <LogsPageInner />
    </Suspense>
  );
}

const terminalStyle: CSSProperties = {
  minHeight: "420px",
  maxHeight: "66vh",
  overflowY: "auto",
  background: "#111827",
  color: "#e5e7eb",
  fontFamily:
    "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', monospace",
  fontSize: "0.82rem",
  lineHeight: 1.5,
  padding: "0.9rem 1rem",
  borderRadius: "0.375rem",
  border: "1px solid #374151",
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
};

const lineStyle: CSSProperties = {
  minHeight: "1.3em",
};

const errorStyle: CSSProperties = {
  marginBottom: "0.75rem",
  padding: "0.75rem",
  border: "1px solid #fca5a5",
  borderRadius: "0.375rem",
  background: "#fee2e2",
  color: "#991b1b",
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
};
