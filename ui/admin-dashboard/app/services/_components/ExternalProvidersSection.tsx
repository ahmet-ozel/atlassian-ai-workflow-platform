"use client";

/**
 * ExternalProvidersSection - AI model provider status widget.
 * * Renders a compact section above the managed services table showing the
 * live status of explicitly configured AI model providers.
 * * Each provider displays:
 * - Name
 * - Status badge (green/yellow/red/grey)
 * - Last probe timestamp
 * - Latency (ms)
 * - "Şimdi Test Et" (Test Now) button for on-demand re-probe
 * * When no AI provider is configured, the section renders a neutral grey
 * "AI model tanimli degil" state instead of red provider failures.
 * */

import { useCallback, useEffect, useRef, useState } from "react";

import { apiFetch } from "@/lib/api-client";

// --------------------------------------------------------------------------
// Types - mirrors ExternalServiceResponse from external_providers.py
// --------------------------------------------------------------------------

type ExternalProviderStatus =
  | "ok"
  | "unreachable"
  | "unauthorized"
  | "rate_limited";

type ExternalProvider = {
  name: string;
  kind: "external";
  base_url: string;
  status: ExternalProviderStatus;
  last_probed_at: number;
  latency_ms: number | null;
  error: string | null;
};

type ExternalServicesResponse = {
  services: ExternalProvider[];
};

const AI_PROVIDER_NAMES = new Set(["openai", "vllm", "anthropic"]);

// --------------------------------------------------------------------------
// Status badge configuration
// --------------------------------------------------------------------------

type BadgeConfig = {
  label: string;
  background: string;
  color: string;
};

const STATUS_BADGE_MAP: Record<ExternalProviderStatus, BadgeConfig> = {
  ok: { label: "OK", background: "#16a34a", color: "#ffffff" },
  rate_limited: { label: "Rate Limited", background: "#facc15", color: "#1f2937" },
  unreachable: { label: "Unreachable", background: "#dc2626", color: "#ffffff" },
  unauthorized: { label: "Unauthorized", background: "#9ca3af", color: "#1f2937" },
};

// --------------------------------------------------------------------------
// Contextual error messages for unreachable providers
// --------------------------------------------------------------------------

const UNREACHABLE_HINTS: Record<string, string> = {
  vllm: "vLLM host may be down. Check VLLM_BASE_URL and the vLLM process on the host.",
  openai:
    "OpenAI API is unreachable. Check internet access and OPENAI_API_KEY configuration.",
  anthropic:
    "Anthropic API is unreachable. Check internet access and ANTHROPIC_API_KEY configuration.",
  "firecrawl-cloud":
    "Firecrawl Cloud is unreachable. Check FIRECRAWL_CLOUD_BASE_URL and FIRECRAWL_CLOUD_API_KEY configuration.",
};

function getUnreachableHint(providerName: string, baseUrl: string): string {
  const hint = UNREACHABLE_HINTS[providerName.toLowerCase()];
  if (hint) {
    // For vLLM, include the actual base_url in the message
    if (providerName.toLowerCase() === "vllm" && baseUrl) {
      return `vLLM host may be down. ${baseUrl} is unreachable. Check the vLLM process on the host.`;
    }
    return hint;
  }
  return `${providerName} is unreachable. Check connection settings.`;
}

// --------------------------------------------------------------------------
// Helpers
// --------------------------------------------------------------------------

function formatProbeTime(epochSeconds: number): string {
  if (!epochSeconds) return "-";
  const date = new Date(epochSeconds * 1000);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleTimeString();
}

function formatLatency(latencyMs: number | null): string {
  if (latencyMs === null || latencyMs === undefined) return "-";
  return `${Math.round(latencyMs)} ms`;
}

// --------------------------------------------------------------------------
// StatusBadge sub-component
// --------------------------------------------------------------------------

function ExternalStatusBadge({ status }: { status: ExternalProviderStatus }) {
  const config = STATUS_BADGE_MAP[status] ?? STATUS_BADGE_MAP.unreachable;
  return (
    <span
      style={{
        display: "inline-block",
        padding: "0.15rem 0.5rem",
        borderRadius: "0.75rem",
        fontSize: "0.8rem",
        fontWeight: 600,
        letterSpacing: "0.03em",
        whiteSpace: "nowrap",
        background: config.background,
        color: config.color,
      }}
      aria-label={`Status: ${config.label}`}
      data-status={status}
    >
      {config.label}
    </span>
  );
}

// --------------------------------------------------------------------------
// Provider card sub-component
// --------------------------------------------------------------------------

type ProviderCardProps = {
  provider: ExternalProvider;
  onTestNow: (name: string) => void;
  testing: boolean;
};

function ProviderCard({ provider, onTestNow, testing }: ProviderCardProps) {
  const isUnreachable = provider.status === "unreachable";

  return (
    <div
      style={{
        border: isUnreachable ? "1px solid #fca5a5" : "1px solid #e5e7eb",
        borderRadius: "0.5rem",
        padding: "1rem",
        background: isUnreachable ? "#fef2f2" : "#ffffff",
        minWidth: "220px",
        flex: "1 1 220px",
      }}
    >
      {/* Header: name + badge */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: "0.5rem",
        }}
      >
        <span
          style={{
            fontWeight: 600,
            fontSize: "0.95rem",
            fontFamily:
              "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
          }}
        >
          {provider.name}
        </span>
        <ExternalStatusBadge status={provider.status} />
      </div>

      {/* Metrics row */}
      <div
        style={{
          display: "flex",
          gap: "1rem",
          fontSize: "0.82rem",
          color: "#6b7280",
          marginBottom: "0.5rem",
        }}
      >
        <span title="Son probe zamanı">
          Time {formatProbeTime(provider.last_probed_at)}
        </span>
        <span title="Latency">
          Latency {formatLatency(provider.latency_ms)}
        </span>
      </div>

      {/* Unreachable hint */}
      {isUnreachable && (
        <div
          role="alert"
          style={{
            background: "#fee2e2",
            color: "#7f1d1d",
            padding: "0.4rem 0.6rem",
            borderRadius: "0.25rem",
            fontSize: "0.78rem",
            marginBottom: "0.5rem",
            lineHeight: 1.4,
          }}
        >
          {getUnreachableHint(provider.name, provider.base_url)}
        </div>
      )}

      {/* Unauthorized hint */}
      {provider.status === "unauthorized" && (
        <div
          style={{
            background: "#f3f4f6",
            color: "#374151",
            padding: "0.4rem 0.6rem",
            borderRadius: "0.25rem",
            fontSize: "0.78rem",
            marginBottom: "0.5rem",
            lineHeight: 1.4,
          }}
        >
          API key is missing or invalid. Check the related env variable or Vault path.
        </div>
      )}

      {/* Rate limited hint */}
      {provider.status === "rate_limited" && (
        <div
          style={{
            background: "#fef9c3",
            color: "#78350f",
            padding: "0.4rem 0.6rem",
            borderRadius: "0.25rem",
            fontSize: "0.78rem",
            marginBottom: "0.5rem",
            lineHeight: 1.4,
          }}
        >
          Rate limit exceeded. It should recover after a short wait.
        </div>
      )}

      {/* Test Now button */}
      <button
        type="button"
        onClick={() => onTestNow(provider.name)}
        disabled={testing}
        style={{
          padding: "0.3rem 0.7rem",
          fontSize: "0.82rem",
          border: "1px solid #2563eb",
          color: testing ? "#9ca3af" : "#2563eb",
          background: "#ffffff",
          borderRadius: "0.25rem",
          cursor: testing ? "not-allowed" : "pointer",
          opacity: testing ? 0.6 : 1,
        }}
        aria-label={`Run connection test for ${provider.name}`}
      >
        {testing ? "Testing..." : "Test Now"}
      </button>
    </div>
  );
}

function NoModelCard() {
  return (
    <div
      style={{
        border: "1px solid #d1d5db",
        borderRadius: "0.5rem",
        padding: "1rem",
        background: "#f9fafb",
        minWidth: "220px",
        flex: "1 1 220px",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: "0.5rem",
        }}
      >
        <span
          style={{
            fontWeight: 600,
            fontSize: "0.95rem",
            fontFamily:
              "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
          }}
        >
          ai-model
        </span>
        <span
          style={{
            display: "inline-block",
            padding: "0.15rem 0.5rem",
            borderRadius: "0.75rem",
            fontSize: "0.8rem",
            fontWeight: 600,
            background: "#e5e7eb",
            color: "#374151",
            whiteSpace: "nowrap",
          }}
          data-status="not-configured"
        >
          Tanimli degil
        </span>
      </div>
      <div style={{ color: "#6b7280", fontSize: "0.82rem", lineHeight: 1.4 }}>
        AI model tanımlı değil. OpenAI, vLLM veya Anthropic bilgisi girilince
        bu alan test sonucuna göre yeşil ya da kırmızı olur.
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// Main section component
// --------------------------------------------------------------------------

export default function ExternalProvidersSection() {
  const [providers, setProviders] = useState<ExternalProvider[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [testingProvider, setTestingProvider] = useState<string | null>(null);
  const cancelledRef = useRef(false);

  const fetchProviders = useCallback(async (bypassCache = false) => {
    try {
      const query = bypassCache ? "?bypass_cache=true" : "";
      const res = await apiFetch(`/api/v1/services/external${query}`);
      if (cancelledRef.current) return;
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        setError(
          `GET /api/v1/services/external  HTTP ${res.status}${text ? `: ${text.slice(0, 200)}` : ""}`,
        );
        return;
      }
      const data = (await res.json()) as ExternalServicesResponse;
      if (cancelledRef.current) return;
      setProviders(data.services);
      setError(null);
    } catch (err) {
      if (cancelledRef.current) return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (!cancelledRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    cancelledRef.current = false;
    void fetchProviders();
    // Refresh every 30s to match the API cache TTL
    const id = window.setInterval(() => {
      void fetchProviders();
    }, 30_000);
    return () => {
      cancelledRef.current = true;
      window.clearInterval(id);
    };
  }, [fetchProviders]);

  const handleTestNow = useCallback(
    async (providerName: string) => {
      setTestingProvider(providerName);
      await fetchProviders(true);
      setTestingProvider(null);
    },
    [fetchProviders],
  );

  const aiProviders = providers.filter((provider) =>
    AI_PROVIDER_NAMES.has(provider.name.toLowerCase()),
  );

  return (
    <section
      aria-labelledby="external-providers-heading"
      style={{ marginBottom: "1.5rem" }}
    >
      <h2
        id="external-providers-heading"
        style={{
          fontSize: "1.1rem",
          fontWeight: 600,
          margin: "0 0 0.75rem",
          color: "#111827",
        }}
      >
        AI Model
      </h2>

      {loading && (
        <p style={{ color: "#6b7280", fontSize: "0.9rem" }}>
          AI model status is loading...
        </p>
      )}

      {error && (
        <div
          role="alert"
          style={{
            background: "#fef3c7",
            color: "#78350f",
            padding: "0.75rem",
            borderRadius: "0.25rem",
            fontSize: "0.9rem",
            marginBottom: "0.75rem",
          }}
        >
          AI model status could not be loaded: {error}
        </div>
      )}

      {!loading && !error && aiProviders.length === 0 && (
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: "0.75rem",
          }}
        >
          <NoModelCard />
        </div>
      )}

      {!loading && !error && aiProviders.length > 0 && (
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: "0.75rem",
          }}
        >
          {aiProviders.map((provider) => (
            <ProviderCard
              key={provider.name}
              provider={provider}
              onTestNow={handleTestNow}
              testing={testingProvider === provider.name}
            />
          ))}
        </div>
      )}
    </section>
  );
}
