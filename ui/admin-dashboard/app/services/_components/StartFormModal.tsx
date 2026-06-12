"use client";

/**
 * StartFormModal - Servis Yapilandirma Formu.
 * * The component is rendered when the operator clicks "Start" on a service
 * catalog row. It fetches the service manifest, renders one field per
 * form_schema entry, and submits env_overrides only after explicit user input.
 * * Sensitive fields render as required password inputs. Non-sensitive fields use
 * text inputs, display API defaults as placeholders, and fall back to those
 * defaults when submitted blank. Empty sensitive values raise an inline
 * validation error and the start request is not sent.
 * * Error envelopes are surfaced inline. Upstream failures include the
 * correlation_id so the operator can pivot into audit logs or structured logs.
 * * Defense-in-depth: backend is_sensitive is authoritative, but the UI also
 * applies the shared sensitive-key matcher so a stale server cache cannot render
 * a token field as plain text.
 * * The modal only calls the API on mount and explicit submit. There is no
 * polling, automatic retry, or preflight start.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { isSensitiveEnvKey } from "@platform/web-shared";

import { apiFetch } from "../../../lib/api-client";
import { getStreamlitUrl } from "../../../lib/config";
import AtlassianMcpStartForm from "./AtlassianMcpStartForm";

// ---------------------------------------------------------------------------
// Wire types - kept in sync with services/admin-dashboard-api/src/routers/_models.py
// ---------------------------------------------------------------------------

/**
 * One row of ``form_schema.fields`` returned by ``GET /admin/services/{name}``.
 * Mirrors :class:`src.routers._models.FormSchemaField`.
 */
export type FormSchemaField = {
  key: string;
  default_value: string;
  comment: string | null;
  is_sensitive: boolean;
};

/**
 * Subset of :class:`src.routers._models.ServiceDetail` actually consumed
 * by this component. Other fields (state, last_health_snapshot, etc.)
 * are owned by the catalog page and re-fetched after ``onStarted``.
 */
type ServiceDetailSubset = {
  name: string;
  form_schema: { fields: FormSchemaField[] };
};

/**
 * 502 :class:`ErrorEnvelope` shape. The router emits
 * this body for every Vault / Audit / Compose upstream failure.
 */
type ErrorEnvelope = {
  detail: string;
  correlation_id: string;
};

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

/**
 * Public props of {@link StartFormModal}. The catalog page is in
 * charge of mounting / unmounting the modal - the component itself
 * never decides to close.
 */
export type StartFormModalProps = {
  /**
   * Managed_Service ``name`` from the manifest. Used as the ``{name}``
   * path parameter for both the schema fetch and the start POST.
   */
  serviceName: string;

  /**
   * Called on every dismissal path: explicit Cancel button, Escape
   * key, backdrop click, *and* after a successful start (so the parent
   * can unmount the modal).
   */
  onClose: () => void;

  /**
   * Optional callback fired once the start request returns a
   * non-error response. The parent uses this hook to refresh the
   * Servis Kataloğu rows so the new ``starting`` / ``running`` state
   * lands on screen without waiting for the next poll tick.
   */
  onStarted?: () => void;

  /**
   * Called when the start request returns 409 ``feature_flag_disabled``.
   * The parent is responsible for showing the FeatureFlagDisabledModal
   * with the blocking flag name.
   */
  onFeatureFlagDisabled?: (blockingFlag: string) => void;
};

// ---------------------------------------------------------------------------
// Inline styles (no shared design system is available here yet
// renders the catalog with raw <table>; we follow the same minimalism)
// ---------------------------------------------------------------------------

const overlayStyle: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "rgba(0, 0, 0, 0.5)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 1000,
};

const modalStyle: React.CSSProperties = {
  background: "#fff",
  color: "#111",
  borderRadius: 8,
  padding: "1.5rem",
  width: "min(640px, 92vw)",
  maxHeight: "90vh",
  overflowY: "auto",
  boxShadow: "0 10px 30px rgba(0, 0, 0, 0.3)",
};

const fieldRowStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: "0.25rem",
  marginBottom: "0.75rem",
};

const labelStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: "0.25rem",
  fontFamily: "monospace",
  fontWeight: 600,
};

const inputStyle: React.CSSProperties = {
  padding: "0.4rem 0.5rem",
  border: "1px solid #ccc",
  borderRadius: 4,
  fontFamily: "monospace",
};

const helpStyle: React.CSSProperties = {
  color: "#555",
  fontWeight: 400,
  fontFamily: "sans-serif",
  whiteSpace: "pre-wrap",
};

const errorBoxStyle: React.CSSProperties = {
  background: "#fdecea",
  border: "1px solid #f5c2c0",
  color: "#611a15",
  padding: "0.5rem 0.75rem",
  borderRadius: 4,
  marginBottom: "0.75rem",
  fontSize: "0.9rem",
};

const buttonRowStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "flex-end",
  gap: "0.5rem",
  marginTop: "1rem",
};

const infoBoxStyle: React.CSSProperties = {
  background: "#eef6ff",
  border: "1px solid #b8d8ff",
  color: "#12324f",
  padding: "0.65rem 0.75rem",
  borderRadius: 4,
  marginBottom: "0.9rem",
  fontSize: "0.9rem",
  lineHeight: 1.45,
};

const LLM_SECRET_KEYS = new Set([
  "OPENAI_API_KEY",
  "ANTHROPIC_API_KEY",
  "VLLM_API_KEY",
]);

const STREAMLIT_HIDDEN_KEYS = new Set([
  "LOG_LEVEL",
  "CLIENT_SOURCE",
  "OPENAI_BASE_URL",
  "ANTHROPIC_BASE_URL",
  "LLM_REASONING_EFFORT",
  "LLM_VERBOSITY",
]);

const PROVIDER_SPECIFIC_KEYS = new Set([
  "OPENAI_API_KEY",
  "ANTHROPIC_API_KEY",
  "VLLM_BASE_URL",
  "VLLM_API_KEY",
]);

const PROVIDER_VISIBLE_KEYS: Record<string, Set<string>> = {
  openai: new Set(["OPENAI_API_KEY"]),
  anthropic: new Set(["ANTHROPIC_API_KEY"]),
  vllm: new Set(["VLLM_BASE_URL", "VLLM_API_KEY"]),
};

const LLM_PROVIDER_OPTIONS = [
  { value: "openai", label: "OpenAI" },
  { value: "anthropic", label: "Anthropic" },
  { value: "vllm", label: "vLLM" },
];

const STREAMLIT_FIELD_COPY: Record<
  string,
  { label: string; help: string; readOnly?: boolean }
> = {
  PORT: {
    label: "Streamlit container port",
    help: "Container listens on this port. Browser access uses the published host port shown above.",
    readOnly: true,
  },
  ASSISTANT_BASE_URL: {
    label: "Assistant service URL",
    help: "Docker network address used by Streamlit. Leave this default unless the assistant service is external.",
  },
  MCP_BASE_URL: {
    label: "Atlassian MCP URL",
    help: "Docker network address of atlassian-mcp. This is not the browser URL.",
  },
  LLM_PROVIDER: {
    label: "LLM provider",
    help: "Choose the model backend. Credential fields below change with this selection.",
  },
  LLM_MODEL_NAME: {
    label: "Model name",
    help: "Model identifier sent to the selected provider.",
  },
  OPENAI_API_KEY: {
    label: "OpenAI API key",
    help: "Required when provider is OpenAI.",
  },
  ANTHROPIC_API_KEY: {
    label: "Anthropic API key",
    help: "Required when provider is Anthropic.",
  },
  VLLM_BASE_URL: {
    label: "vLLM base URL",
    help: "Required when provider is vLLM. Use the OpenAI-compatible /v1 endpoint.",
  },
  VLLM_API_KEY: {
    label: "vLLM API key",
    help: "Required when provider is vLLM. Use a placeholder only if your vLLM gateway does not enforce auth.",
  },
};

function normalizeProvider(
  value: FormDataEntryValue | string | null,
  fallback = "openai",
): string {
  const normalized = String(value ?? "").trim().toLowerCase();
  return normalized.length > 0 ? normalized : fallback;
}

function llmSecretRequired(key: string, provider: string): boolean {
  if (key === "OPENAI_API_KEY") return provider === "openai";
  if (key === "ANTHROPIC_API_KEY") return provider === "anthropic";
  if (key === "VLLM_API_KEY") return provider === "vllm";
  return false;
}

function providerDefaultFromFields(fields: FormSchemaField[] | null): string {
  return normalizeProvider(
    fields?.find((field) => field.key === "LLM_PROVIDER")?.default_value ?? null,
  );
}

function streamlitFieldVisible(key: string, provider: string): boolean {
  if (STREAMLIT_HIDDEN_KEYS.has(key)) return false;
  if (!PROVIDER_SPECIFIC_KEYS.has(key)) return true;
  return PROVIDER_VISIBLE_KEYS[provider]?.has(key) ?? false;
}

function fieldLabel(field: FormSchemaField, serviceName: string): string {
  if (serviceName === "streamlit-ui") {
    return STREAMLIT_FIELD_COPY[field.key]?.label ?? field.key;
  }
  return field.key;
}

function fieldHelp(field: FormSchemaField, serviceName: string): string | null {
  if (serviceName === "streamlit-ui") {
    return STREAMLIT_FIELD_COPY[field.key]?.help ?? field.comment;
  }
  return field.comment;
}

function fieldReadOnly(field: FormSchemaField, serviceName: string): boolean {
  return Boolean(
    serviceName === "streamlit-ui" &&
      STREAMLIT_FIELD_COPY[field.key]?.readOnly,
  );
}

function sensitiveFieldRequired(
  key: string,
  sensitive: boolean,
  provider: string,
  serviceName?: string,
): boolean {
  if (!sensitive) return false;
  if (serviceName === "atlassian-mcp") return false;
  if (LLM_SECRET_KEYS.has(key)) return llmSecretRequired(key, provider);
  return true;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function StartFormModal({
  serviceName,
  onClose,
  onStarted,
  onFeatureFlagDisabled,
}: StartFormModalProps): JSX.Element {
  const [fields, setFields] = useState<FormSchemaField[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitCorrelationId, setSubmitCorrelationId] = useState<string | null>(
    null,
  );
  const [validationErrors, setValidationErrors] = useState<
    Record<string, string>
  >({});
  const [selectedProviderInput, setSelectedProviderInput] = useState("");
  const [streamlitPublicUrl, setStreamlitPublicUrl] = useState("");
  const formRef = useRef<HTMLFormElement | null>(null);

  // -------------------------------------------------------------------------
  // Schema fetch - runs once per mount.
  // -------------------------------------------------------------------------

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    async function load(): Promise<void> {
      setLoadError(null);
      try {
        const res = await apiFetch(
          `/admin/services/${encodeURIComponent(serviceName)}`,
          { signal: controller.signal },
        );
        if (cancelled) return;
        if (!res.ok) {
          // Surface 4xx (e.g. 404 unknown service) inline instead of
          // throwing - operator should still be able to dismiss.
          const detail = await safeReadDetail(res);
          setLoadError(
            `Failed to load form schema (HTTP ${res.status}): ${detail}`,
          );
          return;
        }
        const body = (await res.json()) as ServiceDetailSubset;
        if (cancelled) return;
        const loadedFields = body.form_schema?.fields ?? [];
        setFields(loadedFields);
        setSelectedProviderInput(providerDefaultFromFields(loadedFields));
      } catch (err: unknown) {
        if (cancelled || (err instanceof DOMException && err.name === "AbortError")) {
          return;
        }
        setLoadError(
          err instanceof Error ? err.message : "Network error loading schema",
        );
      }
    }

    void load();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [serviceName]);

  useEffect(() => {
    if (serviceName === "streamlit-ui") {
      setStreamlitPublicUrl(getStreamlitUrl());
    }
  }, [serviceName]);

  // -------------------------------------------------------------------------
  // Escape-to-close (modal ergonomics; the operator can always also click
  // the Cancel button or the backdrop).
  // -------------------------------------------------------------------------

  useEffect(() => {
    function onKey(ev: KeyboardEvent): void {
      if (ev.key === "Escape" && !submitting) {
        onClose();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, submitting]);

  // -------------------------------------------------------------------------
  // Submit handler - only fires on explicit user
  // action (form's onSubmit). preventDefault() blocks the implicit GET
  // navigation Next.js would otherwise pick up.
  // -------------------------------------------------------------------------

  /**
   * Resolve the *effective* sensitivity of a field by OR-ing the
   * server-supplied flag with the local regex check
   * (defense-in-depth).
   */
  const isFieldSensitive = useMemo(
    () =>
      (field: FormSchemaField): boolean =>
        field.is_sensitive || isSensitiveEnvKey(field.key),
    [],
  );

  const providerDefault = useMemo(
    () => providerDefaultFromFields(fields),
    [fields],
  );
  const selectedProvider = normalizeProvider(
    selectedProviderInput,
    providerDefault,
  );
  const visibleFields = useMemo(() => {
    if (fields == null) return [];
    if (serviceName !== "streamlit-ui") return fields;
    return fields.filter((field) =>
      streamlitFieldVisible(field.key, selectedProvider),
    );
  }, [fields, selectedProvider, serviceName]);

  async function handleSubmit(
    ev: React.FormEvent<HTMLFormElement>,
  ): Promise<void> {
    ev.preventDefault();
    if (submitting || fields == null) return;

    const formEl = ev.currentTarget;
    const fd = new FormData(formEl);
    const effectiveProvider = normalizeProvider(
      fd.get("LLM_PROVIDER"),
      providerDefaultFromFields(fields),
    );

    // Build env_overrides + run client-side validation.
    const envOverrides: Record<string, string> = {};
    const newValidationErrors: Record<string, string> = {};

    for (const field of fields) {
      const raw = String(fd.get(field.key) ?? "");
      const trimmed = raw.trim();
      const sensitive = isFieldSensitive(field);

      if (trimmed.length > 0) {
        // Always preserve the operator-typed value verbatim (no trim
        // for sensitive values - leading/trailing whitespace can be
        // semantically significant for tokens).
        envOverrides[field.key] = sensitive ? raw : trimmed;
        continue;
      }

      // Empty input.
      if (sensitive) {
        if (!sensitiveFieldRequired(field.key, sensitive, effectiveProvider, serviceName)) {
          envOverrides[field.key] = "";
          continue;
        }
        // Sensitive_Env_Key default_value is *never* used - the
        // operator must explicitly type one.
        newValidationErrors[field.key] =
          `${fieldLabel(field, serviceName)} is required for ${effectiveProvider}.`;
        continue;
      }
      if (field.default_value.length > 0) {
        envOverrides[field.key] = field.default_value;
      } else {
        // Preserve exact LHS parity with the backend form schema while
        // still allowing optional settings to remain intentionally unset.
        envOverrides[field.key] = "";
      }
    }

    if (Object.keys(newValidationErrors).length > 0) {
      setValidationErrors(newValidationErrors);
      return;
    }
    setValidationErrors({});

    setSubmitting(true);
    setSubmitError(null);
    setSubmitCorrelationId(null);

    try {
      const res = await apiFetch(
        `/admin/services/${encodeURIComponent(serviceName)}/start`,
        {
          method: "POST",
          body: JSON.stringify({ env_overrides: envOverrides }),
        },
      );

      if (res.ok) {
        // 202 Accepted. Notify parent and dismiss.
        onStarted?.();
        onClose();
        return;
      }

      // 409 feature_flag_disabled - delegate to parent modal.
      if (res.status === 409) {
        try {
          const ct = res.headers.get("content-type") ?? "";
          if (ct.includes("application/json")) {
            const body = (await res.json()) as {
              error?: string;
              blocking_flag?: string;
            };
            if (
              body.error === "feature_flag_disabled" &&
              typeof body.blocking_flag === "string"
            ) {
              onClose(); // close the start form first
              onFeatureFlagDisabled?.(body.blocking_flag);
              return;
            }
          }
        } catch {
          // fall through to generic error handling
        }
      }

      // Error path: surface the message + correlation_id (when 502).
      await renderErrorFromResponse(res, setSubmitError, setSubmitCorrelationId);
    } catch (err: unknown) {
      setSubmitError(
        err instanceof Error
          ? `Network error: ${err.message}`
          : "Network error during submit.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  const titleId = `start-form-title-${serviceName}`;

  return (
    <div
      style={overlayStyle}
      role="presentation"
      onMouseDown={(ev) => {
        // Close only when the *backdrop* itself is clicked, not when
        // a click happens inside the modal and bubbles up.
        if (ev.target === ev.currentTarget && !submitting) {
          onClose();
        }
      }}
    >
      <div
        style={modalStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <h2 id={titleId} style={{ marginTop: 0 }}>
          Start service: <code>{serviceName}</code>
        </h2>

        {loadError != null && <div style={errorBoxStyle}>{loadError}</div>}

        {fields == null && loadError == null && <p>Loading form schema…</p>}

        {fields != null && fields.length === 0 && loadError == null && (
          <p>
            This service has no environment overrides. Click{" "}
            <strong>Start</strong> to launch it with the manifest defaults.
          </p>
        )}

        {fields != null && serviceName === "atlassian-mcp" && (
          <AtlassianMcpStartForm
            fields={fields}
            submitting={submitting}
            setSubmitting={setSubmitting}
            onClose={onClose}
            onStarted={onStarted}
            onFeatureFlagDisabled={onFeatureFlagDisabled}
          />
        )}

        {fields != null && serviceName !== "atlassian-mcp" && (
          <form ref={formRef} onSubmit={handleSubmit} noValidate>
            {serviceName === "streamlit-ui" && (
              <div style={infoBoxStyle}>
                <div>
                  <strong>Streamlit UI</strong>
                  {streamlitPublicUrl ? (
                    <>
                      {" "}
                      opens at <code>{streamlitPublicUrl}</code>.
                    </>
                  ) : null}
                </div>
                <div>
                  The service and MCP URLs below are Docker-internal addresses
                  used between containers.
                </div>
              </div>
            )}

            {submitError != null && (
              <div style={errorBoxStyle} role="alert">
                <div>{submitError}</div>
                {submitCorrelationId != null && (
                  <div style={{ marginTop: "0.25rem", fontFamily: "monospace" }}>
                    correlation_id:{" "}
                    <code data-testid="correlation-id">
                      {submitCorrelationId}
                    </code>
                  </div>
                )}
              </div>
            )}

            {visibleFields.map((field) => {
              const sensitive = isFieldSensitive(field);
              const isProviderField = field.key === "LLM_PROVIDER";
              const labelText = fieldLabel(field, serviceName);
              const helpText = fieldHelp(field, serviceName);
              const readOnly = fieldReadOnly(field, serviceName);
              const fieldRequired = sensitiveFieldRequired(
                field.key,
                sensitive,
                selectedProvider,
                serviceName,
              );
              const helpId = helpText ? `${field.key}-help` : undefined;
              const errId = validationErrors[field.key]
                ? `${field.key}-err`
                : undefined;
              const describedBy =
                [helpId, errId].filter(Boolean).join(" ") || undefined;

              return (
                <div key={field.key} style={fieldRowStyle}>
                  <label style={labelStyle}>
                    <span>
                      {labelText}
                      {fieldRequired && (
                        <span
                          aria-label="sensitive"
                          title="Sensitive_Env_Key - must be entered explicitly"
                          style={{ marginLeft: "0.4rem", color: "#b00" }}
                        >
                          * </span>
                      )}
                    </span>
                    {helpText && (
                      <small id={helpId} style={helpStyle}>
                        {helpText}
                      </small>
                    )}
                    {isProviderField ? (
                      <select
                        name={field.key}
                        value={selectedProvider}
                        onChange={(ev) => {
                          setSelectedProviderInput(ev.currentTarget.value);
                        }}
                        aria-describedby={describedBy}
                        aria-invalid={errId != null ? "true" : undefined}
                        style={inputStyle}
                      >
                        {LLM_PROVIDER_OPTIONS.map((option) => (
                          <option key={option.value} value={option.value}>
                            {option.label}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <input
                        name={field.key}
                        type={sensitive ? "password" : "text"}
                        readOnly={readOnly}
                        defaultValue={
                          serviceName === "streamlit-ui" && !sensitive
                            ? field.default_value
                            : undefined
                        }
                        placeholder={
                          sensitive
                            ? fieldRequired
                              ? "(required)"
                              : "(optional)"
                            : field.default_value
                        }
                        autoComplete={sensitive ? "new-password" : "off"}
                        required={fieldRequired}
                        aria-describedby={describedBy}
                        aria-invalid={errId != null ? "true" : undefined}
                        style={
                          readOnly
                            ? { ...inputStyle, background: "#f6f7f9", color: "#444" }
                            : inputStyle
                        }
                      />
                    )}
                  </label>
                  {validationErrors[field.key] && (
                    <small
                      id={errId}
                      role="alert"
                      style={{ color: "#b00", fontFamily: "sans-serif" }}
                    >
                      {validationErrors[field.key]}
                    </small>
                  )}
                </div>
              );
            })}

            <div style={buttonRowStyle}>
              <button
                type="button"
                onClick={onClose}
                disabled={submitting}
                style={{ padding: "0.4rem 0.9rem" }}
              >
                Cancel
              </button>
              <button
                type="submit"
                disabled={submitting}
                style={{
                  padding: "0.4rem 0.9rem",
                  background: "#0b5",
                  color: "#fff",
                  border: "none",
                  borderRadius: 4,
                  cursor: submitting ? "wait" : "pointer",
                }}
              >
                {submitting ? "Starting…" : "Start"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Extract a useful ``detail`` string from any FastAPI error response.
 * FastAPI wraps validation errors as ``{detail: [...]}`` and our
 * router uses plain strings for 4xx envelopes.
 */
async function safeReadDetail(res: Response): Promise<string> {
  try {
    const ct = res.headers.get("content-type") ?? "";
    if (ct.includes("application/json")) {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === "string") return body.detail;
      if (Array.isArray(body.detail)) return JSON.stringify(body.detail);
      return JSON.stringify(body);
    }
    return await res.text();
  } catch {
    return res.statusText || "unknown error";
  }
}

/**
 * Render an error response from the start endpoint into the two
 * pieces of UI state. 502 envelopes carry a ``correlation_id`` UUID
 * which we surface verbatim so the operator
 * can pivot into ``shared.audit_log``.
 */
async function renderErrorFromResponse(
  res: Response,
  setError: (msg: string) => void,
  setCorrelationId: (id: string | null) => void,
): Promise<void> {
  const ct = res.headers.get("content-type") ?? "";
  if (ct.includes("application/json")) {
    try {
      const body = (await res.json()) as Partial<ErrorEnvelope> & {
        detail?: unknown;
      };
      const detail =
        typeof body.detail === "string"
          ? body.detail
          : Array.isArray(body.detail)
            ? JSON.stringify(body.detail)
            : `HTTP ${res.status}`;
      setError(`Start failed (HTTP ${res.status}): ${detail}`);
      if (typeof body.correlation_id === "string") {
        setCorrelationId(body.correlation_id);
      } else {
        setCorrelationId(null);
      }
      return;
    } catch {
      // fall through to plain-text branch
    }
  }
  const text = await res.text().catch(() => "");
  setError(`Start failed (HTTP ${res.status})${text ? `: ${text}` : ""}`);
  setCorrelationId(null);
}
