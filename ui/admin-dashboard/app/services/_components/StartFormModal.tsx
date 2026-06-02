"use client";

/**
 * StartFormModal — Servis Yapılandırma Formu (Requirement 5).
 *
 * Implements task 8.2 of the admin-dashboard-control-plane spec. The
 * component is rendered when the operator clicks "Start" on a row of
 * the Servis Kataloğu (Requirement 4.4) and walks them through three
 * stages:
 *
 *   1. Fetch ``GET /admin/services/{name}`` to retrieve the manifest
 *      entry plus the ``form_schema.fields`` array. The shape mirrors
 *      :class:`src.routers._models.ServiceDetail` exactly
 *      (Requirement 6.2).
 *   2. Render one input per ``FormSchemaField``. Sensitive fields are
 *      rendered as ``<input type="password" required>`` (Requirement
 *      5.3, 5.7); non-sensitive fields use ``<input type="text">``
 *      with the field's ``default_value`` as the HTML placeholder
 *      (Requirement 5.2). Any preceding ``.env.example`` comment is
 *      surfaced as a ``<small id="{key}-help">`` element wired up
 *      with ``aria-describedby`` (Requirement 5.4).
 *   3. On *explicit* submit (button click or Enter — never automatic;
 *      Requirement 5.5), build ``env_overrides``: empty values fall
 *      back to ``default_value`` for *non*-sensitive fields with a
 *      non-empty default; sensitive empty values raise an inline
 *      validation error (Requirement 5.7) and the request is *not*
 *      sent. Non-sensitive empty values without defaults are sent as
 *      empty strings so the client preserves backend form-schema
 *      parity. POST ``/admin/services/{name}/start`` with the body
 *      ``{env_overrides: {...}}`` (Requirement 5.5).
 *
 * Error surface (matching the router envelopes in design §3.3):
 *
 * * ``404`` (unknown service) — inline message; close to retry.
 * * ``422`` (form schema mismatch) — inline message lists the
 *   server-supplied detail (e.g. extra/missing keys) so the operator
 *   can correct without reopening the modal.
 * * ``502`` (Vault / Audit / Compose upstream failure) — inline
 *   message *plus* the ``correlation_id`` UUID copied from the
 *   :class:`~src.routers._models.ErrorEnvelope` body so the operator
 *   can pivot into the audit log / structured logs (Requirement 6.7,
 *   11.8).
 *
 * Defense-in-depth: even though ``is_sensitive`` in the schema is
 * authoritative (the backend computes it via the Python twin of
 * ``isSensitiveEnvKey`` — Property C4), we OR it with the result of
 * :func:`isSensitiveEnvKey` from ``web-shared`` so a stale server
 * cache cannot trick the UI into rendering a token field as plain
 * text (Requirement 5.3, 7.7, 11.3).
 *
 * The component is a pure modal: it only talks to the API on mount
 * (``GET``) and on explicit submit (``POST``). No polling, no auto-
 * retry, no preflight start (Requirement 5.5).
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { isSensitiveEnvKey } from "@yeni-atlassian/web-shared";

import { apiFetch } from "../../../lib/api-client";

// ---------------------------------------------------------------------------
// Wire types — kept in sync with services/admin-dashboard-api/src/routers/_models.py
// ---------------------------------------------------------------------------

/**
 * One row of ``form_schema.fields`` returned by ``GET /admin/services/{name}``.
 * Mirrors :class:`src.routers._models.FormSchemaField` (Requirement 6.2).
 */
type FormSchemaField = {
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
 * 502 :class:`ErrorEnvelope` shape (Requirement 6.7). The router emits
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
 * charge of mounting / unmounting the modal — the component itself
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
   * with the blocking flag name (Requirement 10.3 / Q12).
   */
  onFeatureFlagDisabled?: (blockingFlag: string) => void;
};

// ---------------------------------------------------------------------------
// Inline styles (no design system in this scaffold yet — design.md §3.10
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

const LLM_SECRET_KEYS = new Set([
  "OPENAI_API_KEY",
  "ANTHROPIC_API_KEY",
  "VLLM_API_KEY",
]);

const LLM_PROVIDER_OPTIONS = [
  { value: "openai", label: "OpenAI" },
  { value: "anthropic", label: "Anthropic" },
  { value: "vllm", label: "vLLM" },
];

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

function sensitiveFieldRequired(
  key: string,
  sensitive: boolean,
  provider: string,
): boolean {
  if (!sensitive) return false;
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
  const formRef = useRef<HTMLFormElement | null>(null);

  // -------------------------------------------------------------------------
  // Schema fetch (Requirement 4.4, 5.1) — runs once per mount.
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
          // throwing — operator should still be able to dismiss.
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
  // Submit handler (Requirement 5.5, 5.7) — only fires on explicit user
  // action (form's onSubmit). preventDefault() blocks the implicit GET
  // navigation Next.js would otherwise pick up.
  // -------------------------------------------------------------------------

  /**
   * Resolve the *effective* sensitivity of a field by OR-ing the
   * server-supplied flag with the local regex check
   * (defense-in-depth — Requirement 5.3, 7.7).
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
        // for sensitive values — leading/trailing whitespace can be
        // semantically significant for tokens).
        envOverrides[field.key] = sensitive ? raw : trimmed;
        continue;
      }

      // Empty input.
      if (sensitive) {
        if (!sensitiveFieldRequired(field.key, sensitive, effectiveProvider)) {
          envOverrides[field.key] = "";
          continue;
        }
        // Sensitive_Env_Key default_value is *never* used — the
        // operator must explicitly type one (Requirement 5.7).
        newValidationErrors[field.key] =
          `${field.key} is required for LLM_PROVIDER=${effectiveProvider}.`;
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
        // 202 Accepted (design §3.3). Notify parent and dismiss.
        onStarted?.();
        onClose();
        return;
      }

      // 409 feature_flag_disabled — delegate to parent modal (Requirement 10.3).
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

        {fields != null && (
          <form ref={formRef} onSubmit={handleSubmit} noValidate>
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

            {fields.map((field) => {
              const sensitive = isFieldSensitive(field);
              const isProviderField = field.key === "LLM_PROVIDER";
              const fieldRequired = sensitiveFieldRequired(
                field.key,
                sensitive,
                selectedProvider,
              );
              const helpId = field.comment ? `${field.key}-help` : undefined;
              const errId = validationErrors[field.key]
                ? `${field.key}-err`
                : undefined;
              const describedBy =
                [helpId, errId].filter(Boolean).join(" ") || undefined;

              return (
                <div key={field.key} style={fieldRowStyle}>
                  <label style={labelStyle}>
                    <span>
                      {field.key}
                      {fieldRequired && (
                        <span
                          aria-label="sensitive"
                          title="Sensitive_Env_Key — must be entered explicitly"
                          style={{ marginLeft: "0.4rem", color: "#b00" }}
                        >
                          *
                        </span>
                      )}
                    </span>
                    {field.comment && (
                      <small id={helpId} style={helpStyle}>
                        {field.comment}
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
                      // Sensitive fields never display the .env.example
                      // default — operator must type one explicitly
                      // (Requirement 5.7). Non-sensitive fields show the
                      // default as the placeholder so the operator can
                      // submit blank to accept it (Requirement 5.2).
                        placeholder={
                          sensitive
                            ? fieldRequired
                              ? "(required)"
                              : "(optional)"
                            : field.default_value
                        }
                        autoComplete={sensitive ? "new-password" : "off"}
                      // ``required`` here is for accessibility hints
                      // only; the actual sensitive-empty check lives
                      // in handleSubmit so we can show a proper inline
                      // message instead of the browser's tooltip.
                        required={fieldRequired}
                        aria-describedby={describedBy}
                        aria-invalid={errId != null ? "true" : undefined}
                        style={inputStyle}
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
 * (Requirement 6.7, 11.8) which we surface verbatim so the operator
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
