"use client";

/**
 * Service detail page.
 *
 * Renders the manifest entry, the cached lifecycle state (via
 * :func:`StateBadge`) and a credentials banner reflecting the most
 * recent connectivity probe outcome. The banner exposes a ``Re-probe``
 * action that hits ``POST /admin/services/{name}/probe`` and refreshes
 * the surfaced fields in place.
 *
 * Wire shape mirrors :class:`src.routers._models.ServiceDetail` (with
 * the connectivity probe fields added in this same task) and
 * :class:`src.routers._models.ProbeResponse`.
 *
 * Banner rules:
 * --------------------
 * - ``credentials_status === "ok"``      → green "Credentials OK".
 * - ``credentials_status === "failed"``  → yellow banner with detail
 *                                          + ``[Re-probe]`` button.
 * - ``credentials_status === "unknown"`` → no banner (probe never ran).
 * - ``credentials_status === null``      → no banner (no probe
 *                                          configured for this service).
 */

import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api-client";

import StateBadge, {
  type ServiceState,
} from "../_components/StateBadge";

// ---------------------------------------------------------------------------
// Wire types - kept in sync with src/routers/_models.py (Pydantic v2).
// ---------------------------------------------------------------------------

type ServiceKind = "http_service" | "worker" | "ui" | "infra";

type FormSchemaField = {
  key: string;
  default_value: string;
  comment: string | null;
  is_sensitive: boolean;
};

type HealthSnapshot = {
  ts: string;
  healthz_status: number;
  healthz_body: string;
  readyz_status: number | null;
  readyz_body: string | null;
  state: "healthy" | "unhealthy" | "unknown";
};

type CredentialsStatus = "ok" | "failed" | "unknown" | null;

type ServiceDetail = {
  name: string;
  kind: ServiceKind;
  compose_service_name: string;
  compose_profile: string;
  env_example_path: string;
  health_endpoint: string | null;
  test_command: string | null;
  state: ServiceState;
  last_started_at: string | null;
  last_health_snapshot: HealthSnapshot | null;
  form_schema: { fields: FormSchemaField[] };
  credentials_status: CredentialsStatus;
  credentials_probe_at: string | null;
  credentials_probe_detail: string | null;
};

type ProbeResponse = {
  service_name: string;
  credentials_status: CredentialsStatus;
  credentials_probe_at: string | null;
  credentials_probe_detail: string | null;
};

type ErrorEnvelope = {
  detail: string;
  correlation_id?: string;
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

/**
 * Extract the LLM provider + model a service is configured to use from
 * its manifest form-schema. Returns ``null`` when the service has no
 * LLM fields (e.g. postgres, vault, atlassian-mcp) - those services do
 * not talk to a model, so no "model" row is shown for them.
 *
 * NOTE: there is no standalone "AI model" service - each service that
 * needs an LLM receives the provider/model + key in its own Start
 * modal. This surfaces that per-service configuration so operators can
 * see at a glance which model a given service runs on.
 */
function extractServiceModel(
  fields: FormSchemaField[],
): { provider: string; model: string } | null {
  const byKey = (k: string): string | null => {
    const f = fields.find((x) => x.key === k);
    const v = f?.default_value?.trim();
    return v ? v : null;
  };
  const provider = byKey("LLM_PROVIDER");
  const model = byKey("LLM_MODEL_NAME");
  if (!provider && !model) return null;
  return {
    provider: provider ?? "-",
    model: model ?? "-",
  };
}

async function safeReadDetail(res: Response): Promise<string> {
  try {
    const ct = res.headers.get("content-type") ?? "";
    if (ct.includes("application/json")) {
      const body = (await res.json()) as Partial<ErrorEnvelope> & {
        detail?: unknown;
      };
      if (typeof body.detail === "string") return body.detail;
      if (Array.isArray(body.detail)) return JSON.stringify(body.detail);
      return JSON.stringify(body);
    }
    const text = await res.text();
    return text.slice(0, 400);
  } catch {
    return `HTTP ${res.status}`;
  }
}

// ---------------------------------------------------------------------------
// Credentials banner
// ---------------------------------------------------------------------------

type CredentialsBannerProps = {
  status: CredentialsStatus;
  probeAt: string | null;
  detail: string | null;
  onReprobe: () => void;
  reprobing: boolean;
  reprobeError: string | null;
};

function CredentialsBanner({
  status,
  probeAt,
  detail,
  onReprobe,
  reprobing,
  reprobeError,
}: CredentialsBannerProps) {
  // ``null`` (no probe configured) and ``unknown`` (probe never run)
  // → no banner rendered. The catalog still shows the lifecycle state.
  if (status === null || status === "unknown") {
    return null;
  }

  if (status === "ok") {
    return (
      <div
        role="status"
        aria-live="polite"
        style={{
          background: "#dcfce7",
          border: "1px solid #86efac",
          color: "#166534",
          padding: "0.6rem 0.9rem",
          borderRadius: "0.4rem",
          marginBottom: "1rem",
          display: "flex",
          alignItems: "center",
          gap: "0.5rem",
          fontSize: "0.9rem",
        }}
      >
        <span aria-hidden style={{ fontSize: "1.05rem" }}>✅</span>
        <span style={{ fontWeight: 600 }}>Kimlik bilgileri OK</span>
        <span style={{ color: "#15803d", fontWeight: 400 }}>
          · son kontrol {formatTimestamp(probeAt)}
        </span>
      </div>
    );
  }

  // status === "failed"
  return (
    <div
      role="alert"
      aria-live="assertive"
      style={{
        background: "#fef9c3",
        border: "1px solid #facc15",
        color: "#78350f",
        padding: "0.75rem 0.9rem",
        borderRadius: "0.4rem",
        marginBottom: "1rem",
        fontSize: "0.9rem",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "0.5rem",
          marginBottom: detail ? "0.5rem" : 0,
        }}
      >
        <span aria-hidden style={{ fontSize: "1.05rem" }}>⚠️</span>
        <strong>Kimlik bilgileri başarısız</strong>
        <span style={{ color: "#92400e", fontWeight: 400 }}>
          · son kontrol {formatTimestamp(probeAt)}
        </span>
        <span style={{ flex: 1 }} />
        <button
          type="button"
          onClick={onReprobe}
          disabled={reprobing}
          aria-label="Re-probe service credentials"
          style={{
            padding: "0.3rem 0.75rem",
            fontSize: "0.85rem",
            fontWeight: 600,
            border: "1px solid #92400e",
            borderRadius: "0.25rem",
            background: reprobing ? "#fde68a" : "#ffffff",
            color: "#78350f",
            cursor: reprobing ? "wait" : "pointer",
          }}
        >
          {reprobing ? "Kontrol ediliyor…" : "Yeniden dene"}
        </button>
      </div>
      {detail && (
        <pre
          style={{
            margin: 0,
            padding: "0.5rem 0.75rem",
            background: "#fffbeb",
            border: "1px solid #fde68a",
            borderRadius: "0.25rem",
            fontFamily:
              "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
            fontSize: "0.78rem",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            color: "#78350f",
          }}
        >
          {detail}
        </pre>
      )}
      {reprobeError && (
        <div
          role="alert"
          style={{
            marginTop: "0.5rem",
            padding: "0.4rem 0.6rem",
            background: "#fee2e2",
            border: "1px solid #fecaca",
            borderRadius: "0.25rem",
            color: "#7f1d1d",
            fontSize: "0.8rem",
          }}
        >
          Yeniden deneme başarısız: {reprobeError}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page component
// ---------------------------------------------------------------------------

type LoadState =
  | { kind: "loading" }
  | { kind: "ok"; detail: ServiceDetail }
  | { kind: "error"; message: string };

type PageProps = {
  params: { name: string };
};

export default function ServiceDetailPage({ params }: PageProps) {
  const { name } = params;
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [reprobing, setReprobing] = useState(false);
  const [reprobeError, setReprobeError] = useState<string | null>(null);

  const fetchDetail = useCallback(async () => {
    try {
      const res = await apiFetch(`/admin/services/${encodeURIComponent(name)}`);
      if (!res.ok) {
        const detail = await safeReadDetail(res);
        setState({
          kind: "error",
          message: `GET /admin/services/${name} → HTTP ${res.status}: ${detail}`,
        });
        return;
      }
      const body = (await res.json()) as ServiceDetail;
      setState({ kind: "ok", detail: body });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setState({ kind: "error", message });
    }
  }, [name]);

  useEffect(() => {
    void fetchDetail();
  }, [fetchDetail]);

  const handleReprobe = useCallback(async () => {
    setReprobing(true);
    setReprobeError(null);
    try {
      const res = await apiFetch(
        `/admin/services/${encodeURIComponent(name)}/probe`,
        { method: "POST", body: JSON.stringify({}) },
      );
      if (!res.ok) {
        const detail = await safeReadDetail(res);
        throw new Error(`HTTP ${res.status}: ${detail}`);
      }
      const probe = (await res.json()) as ProbeResponse;
      // Merge the probe result into the cached detail so the banner
      // refreshes without a second GET round-trip.
      setState((prev) => {
        if (prev.kind !== "ok") return prev;
        return {
          kind: "ok",
          detail: {
            ...prev.detail,
            credentials_status: probe.credentials_status,
            credentials_probe_at: probe.credentials_probe_at,
            credentials_probe_detail: probe.credentials_probe_detail,
          },
        };
      });
    } catch (err) {
      setReprobeError(err instanceof Error ? err.message : String(err));
    } finally {
      setReprobing(false);
    }
  }, [name]);

  return (
    <main
      style={{
        padding: "1.5rem",
        fontFamily: "system-ui, sans-serif",
        maxWidth: 960,
        margin: "0 auto",
      }}
    >
      <nav
        style={{
          fontSize: "0.85rem",
          color: "#6b7280",
          marginBottom: "0.75rem",
        }}
      >
        <a href="/services" style={{ color: "#2563eb", textDecoration: "none" }}>
          ← Servisler
        </a>
      </nav>

      {state.kind === "loading" && <p>Servis detayı yükleniyor…</p>}

      {state.kind === "error" && (
        <div
          role="alert"
          style={{
            background: "#fef3c7",
            color: "#78350f",
            padding: "0.75rem",
            borderRadius: "0.25rem",
          }}
        >
          Servis detayı yüklenemedi: {state.message}
        </div>
      )}

      {state.kind === "ok" && (
        <article>
          <header
            style={{
              display: "flex",
              alignItems: "center",
              gap: "0.75rem",
              marginBottom: "1rem",
              flexWrap: "wrap",
            }}
          >
            <h1 style={{ margin: 0, fontSize: "1.5rem" }}>
              <code>{state.detail.name}</code>
            </h1>
            <StateBadge state={state.detail.state} />
            <span
              style={{
                fontSize: "0.85rem",
                color: "#6b7280",
                textTransform: "uppercase",
                letterSpacing: "0.05em",
              }}
            >
              {state.detail.kind}
            </span>
          </header>

          <CredentialsBanner
            status={state.detail.credentials_status}
            probeAt={state.detail.credentials_probe_at}
            detail={state.detail.credentials_probe_detail}
            onReprobe={handleReprobe}
            reprobing={reprobing}
            reprobeError={reprobeError}
          />

          <section
            style={{
              background: "#ffffff",
              border: "1px solid #e5e7eb",
              borderRadius: "0.4rem",
              padding: "1rem 1.25rem",
              marginBottom: "1rem",
            }}
          >
            <h2 style={{ marginTop: 0, fontSize: "1.05rem" }}>Manifest</h2>
            <dl style={dlStyle}>
              <dt style={dtStyle}>Compose servisi</dt>
              <dd style={ddStyle}>
                <code>{state.detail.compose_service_name}</code>
              </dd>
              <dt style={dtStyle}>Compose profili</dt>
              <dd style={ddStyle}>
                <code>{state.detail.compose_profile}</code>
              </dd>
              <dt style={dtStyle}>Env örneği</dt>
              <dd style={ddStyle}>
                <code>{state.detail.env_example_path}</code>
              </dd>
              <dt style={dtStyle}>Sağlık endpoint'i</dt>
              <dd style={ddStyle}>
                {state.detail.health_endpoint ? (
                  <code>{state.detail.health_endpoint}</code>
                ) : (
                  <span style={{ color: "#9ca3af" }}>-</span>
                )}
              </dd>
              <dt style={dtStyle}>Test komutu</dt>
              <dd style={ddStyle}>
                {state.detail.test_command ? (
                  <code>{state.detail.test_command}</code>
                ) : (
                  <span style={{ color: "#9ca3af" }}>-</span>
                )}
              </dd>
              <dt style={dtStyle}>Son başlatma</dt>
              <dd style={ddStyle}>
                {formatTimestamp(state.detail.last_started_at)}
              </dd>
            </dl>
          </section>

          {(() => {
            const model = extractServiceModel(state.detail.form_schema.fields);
            if (!model) return null;
            const running =
              state.detail.state === "running" ||
              state.detail.state === "running_unmonitored";
            return (
              <section
                style={{
                  background: "#ffffff",
                  border: "1px solid #e5e7eb",
                  borderRadius: "0.4rem",
                  padding: "1rem 1.25rem",
                  marginBottom: "1rem",
                }}
              >
                <h2 style={{ marginTop: 0, fontSize: "1.05rem" }}>
                  Yapay zekâ modeli
                </h2>
                <p
                  style={{
                    margin: "0 0 0.75rem",
                    fontSize: "0.82rem",
                    color: "#6b7280",
                  }}
                >
                  Bu servisin kullandığı model, başlatılırken Start
                  ekranından girilir. Aşağıdaki değerler servisin
                  yapılandırmasından okunur.
                </p>
                <dl style={dlStyle}>
                  <dt style={dtStyle}>Sağlayıcı</dt>
                  <dd style={ddStyle}>
                    <code>{model.provider}</code>
                  </dd>
                  <dt style={dtStyle}>Model</dt>
                  <dd style={ddStyle}>
                    <code>{model.model}</code>
                  </dd>
                  <dt style={dtStyle}>Durum</dt>
                  <dd style={ddStyle}>
                    {running ? (
                      <span style={{ color: "#166534", fontWeight: 600 }}>
                        ✓ Servis çalışıyor
                      </span>
                    ) : (
                      <span style={{ color: "#92400e", fontWeight: 600 }}>
                        Servis çalışmıyor
                      </span>
                    )}
                  </dd>
                </dl>
              </section>
            );
          })()}

          {state.detail.last_health_snapshot && (
            <section
              style={{
                background: "#ffffff",
                border: "1px solid #e5e7eb",
                borderRadius: "0.4rem",
                padding: "1rem 1.25rem",
                marginBottom: "1rem",
              }}
            >
              <h2 style={{ marginTop: 0, fontSize: "1.05rem" }}>
                Son sağlık görüntüsü
              </h2>
              <dl style={dlStyle}>
                <dt style={dtStyle}>Durum</dt>
                <dd style={ddStyle}>
                  {state.detail.last_health_snapshot.state}
                </dd>
                <dt style={dtStyle}>Kontrol zamanı</dt>
                <dd style={ddStyle}>
                  {formatTimestamp(state.detail.last_health_snapshot.ts)}
                </dd>
                <dt style={dtStyle}>healthz HTTP</dt>
                <dd style={ddStyle}>
                  {state.detail.last_health_snapshot.healthz_status}
                </dd>
                {state.detail.last_health_snapshot.readyz_status !== null && (
                  <>
                    <dt style={dtStyle}>readyz HTTP</dt>
                    <dd style={ddStyle}>
                      {state.detail.last_health_snapshot.readyz_status}
                    </dd>
                  </>
                )}
              </dl>
            </section>
          )}
        </article>
      )}
    </main>
  );
}

// ---------------------------------------------------------------------------
// Definition list shared styles
// ---------------------------------------------------------------------------

const dlStyle: React.CSSProperties = {
  display: "grid",
  gridTemplateColumns: "max-content 1fr",
  gap: "0.4rem 1rem",
  margin: 0,
  fontSize: "0.9rem",
};

const dtStyle: React.CSSProperties = {
  color: "#6b7280",
  fontWeight: 600,
};

const ddStyle: React.CSSProperties = {
  margin: 0,
  color: "#1f2937",
};
