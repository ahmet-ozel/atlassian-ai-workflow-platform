"use client";

import { useEffect, useState, type ReactNode } from "react";

import { apiFetch } from "@/lib/api-client";
import StateBadge, { type ServiceState } from "./StateBadge";

type ServiceSummary = {
  name: string;
  state: ServiceState;
};

type ServiceQuickStartProps = {
  services: ServiceSummary[];
  busyServices: Set<string>;
  onStart: (name: string) => void;
  onRestart: (name: string) => void;
};

type ExternalProviderStatus =
  | "ok"
  | "unreachable"
  | "unauthorized"
  | "rate_limited";

type ExternalProvider = {
  name: string;
  status: ExternalProviderStatus;
};

type ExternalServicesResponse = {
  services: ExternalProvider[];
};

type AiModelState =
  | { kind: "loading" }
  | { kind: "none" }
  | { kind: "configured"; provider: ExternalProvider }
  | { kind: "error" };

const STREAMLIT_URL = (
  process.env.NEXT_PUBLIC_STREAMLIT_URL ?? "http://localhost:18501"
).replace(/\/$/, "");

const AI_PROVIDER_NAMES = new Set(["openai", "vllm", "anthropic"]);

function findService(services: ServiceSummary[], name: string): ServiceSummary | null {
  return services.find((service) => service.name === name) ?? null;
}

function serviceReady(service: ServiceSummary | null): boolean {
  return service?.state === "running" || service?.state === "running_unmonitored";
}

function serviceStarting(service: ServiceSummary | null): boolean {
  return service?.state === "starting";
}

function canStart(service: ServiceSummary | null, busy: boolean): boolean {
  if (busy || service == null) return false;
  return service.state === "stopped" || service.state === "failed";
}

function canRestart(service: ServiceSummary | null, busy: boolean): boolean {
  if (busy || service == null) return false;
  return serviceReady(service) || service.state === "unhealthy";
}

function QuickStatus({ service }: { service: ServiceSummary | null }) {
  if (service == null) {
    return <span className="badge">bekleniyor</span>;
  }
  return <StateBadge state={service.state} />;
}

function AiStatusBadge({ state }: { state: AiModelState }) {
  if (state.kind === "loading") {
    return <span className="badge">kontrol ediliyor</span>;
  }
  if (state.kind === "none") {
    return <span className="badge">tanımlı değil</span>;
  }
  if (state.kind === "error") {
    return (
      <span className="badge" style={{ background: "#fee2e2", color: "#991b1b" }}>
        okunamadı
      </span>
    );
  }

  if (state.provider.status === "ok") {
    return (
      <span className="badge" style={{ background: "#16a34a", color: "#ffffff" }}>
        OK
      </span>
    );
  }

  return (
    <span className="badge" style={{ background: "#fee2e2", color: "#991b1b" }}>
      hata
    </span>
  );
}

function AiModelName({ state }: { state: AiModelState }) {
  if (state.kind === "configured") {
    return <strong>{state.provider.name}</strong>;
  }
  if (state.kind === "none") {
    return <span>Model tanımlı değil</span>;
  }
  if (state.kind === "error") {
    return <span>Model durumu alınamadı</span>;
  }
  return <span>Model kontrol ediliyor</span>;
}

function StepShell({
  eyebrow,
  title,
  children,
}: {
  eyebrow: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <div
      style={{
        borderRight: "1px solid var(--border)",
        padding: "1rem",
        minWidth: 0,
      }}
    >
      <div
        style={{
          color: "var(--fg-subtle)",
          fontSize: "0.72rem",
          fontWeight: 700,
          letterSpacing: "0.06em",
          textTransform: "uppercase",
        }}
      >
        {eyebrow}
      </div>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "0.75rem",
          marginTop: "0.35rem",
        }}
      >
        <h3 style={{ fontSize: "0.98rem" }}>{title}</h3>
      </div>
      <div style={{ marginTop: "0.8rem" }}>{children}</div>
    </div>
  );
}

function ServiceActions({
  service,
  busy,
  onStart,
  onRestart,
  chat,
}: {
  service: ServiceSummary | null;
  busy: boolean;
  onStart: () => void;
  onRestart: () => void;
  chat?: boolean;
}) {
  if (serviceReady(service) && chat) {
    return (
      <div className="row">
        <a
          className="btn btn--primary btn--sm"
          href={`${STREAMLIT_URL}/chat`}
          target="_blank"
          rel="noopener noreferrer"
        >
          Chat'i aç
        </a>
        <a
          className="btn btn--sm"
          href={`${STREAMLIT_URL}/credentials`}
          target="_blank"
          rel="noopener noreferrer"
        >
          Kimlik bilgileri
        </a>
      </div>
    );
  }

  if (canStart(service, busy)) {
    return (
      <button type="button" className="btn btn--primary btn--sm" onClick={onStart}>
        Başlat
      </button>
    );
  }

  if (canRestart(service, busy)) {
    return (
      <button type="button" className="btn btn--sm" onClick={onRestart}>
        Yeniden başlat
      </button>
    );
  }

  return (
    <button type="button" className="btn btn--sm" disabled>
      {serviceStarting(service) || busy ? "Hazırlanıyor" : "Bekleniyor"}
    </button>
  );
}

export default function ServiceQuickStart({
  services,
  busyServices,
  onStart,
  onRestart,
}: ServiceQuickStartProps) {
  const [aiModel, setAiModel] = useState<AiModelState>({ kind: "loading" });
  const mcp = findService(services, "atlassian-mcp");
  const streamlit = findService(services, "streamlit-ui");
  const mcpBusy = busyServices.has("atlassian-mcp");
  const streamlitBusy = busyServices.has("streamlit-ui");

  useEffect(() => {
    let cancelled = false;

    async function fetchAiModel() {
      try {
        const res = await apiFetch("/api/v1/services/external");
        if (!res.ok) {
          if (!cancelled) setAiModel({ kind: "error" });
          return;
        }
        const data = (await res.json()) as ExternalServicesResponse;
        const provider =
          data.services.find(
            (item) =>
              item.status === "ok" &&
              AI_PROVIDER_NAMES.has(item.name.toLowerCase()),
          ) ??
          data.services.find((item) =>
            AI_PROVIDER_NAMES.has(item.name.toLowerCase()),
          );
        if (cancelled) return;
        setAiModel(provider ? { kind: "configured", provider } : { kind: "none" });
      } catch {
        if (!cancelled) setAiModel({ kind: "error" });
      }
    }

    void fetchAiModel();
    const id = window.setInterval(() => {
      void fetchAiModel();
    }, 30_000);

    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  return (
    <section className="card" aria-labelledby="quick-start-title">
      <div className="card__header">
        <div>
          <h2 id="quick-start-title" className="card__title">
            Hızlı başlangıç
          </h2>
          <div className="card__sub">Model, MCP ve Streamlit sohbet akışı</div>
        </div>
        <a className="btn btn--sm" href="/llm-providers">
          AI modelleri
        </a>
      </div>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
        }}
      >
        <StepShell eyebrow="Model" title="AI modeli">
          <div className="row--between">
            <div style={{ display: "grid", gap: "0.35rem", minWidth: 0 }}>
              <AiModelName state={aiModel} />
              <AiStatusBadge state={aiModel} />
            </div>
            <a className="btn btn--sm" href="/llm-providers">
              Yönet
            </a>
          </div>
        </StepShell>

        <StepShell eyebrow="MCP" title="Atlassian MCP">
          <div className="row--between">
            <QuickStatus service={mcp} />
            <ServiceActions
              service={mcp}
              busy={mcpBusy}
              onStart={() => onStart("atlassian-mcp")}
              onRestart={() => onRestart("atlassian-mcp")}
            />
          </div>
        </StepShell>

        <StepShell eyebrow="Chat" title="Streamlit">
          <div className="row--between">
            <QuickStatus service={streamlit} />
            <ServiceActions
              service={streamlit}
              busy={streamlitBusy}
              onStart={() => onStart("streamlit-ui")}
              onRestart={() => onRestart("streamlit-ui")}
              chat
            />
          </div>
        </StepShell>
      </div>
    </section>
  );
}
