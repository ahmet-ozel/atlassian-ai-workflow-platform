"use client";

/**
 * StopConfirmationModal - Service Stop confirmation with optional advanced
 * tear-down toggles.
 *
 * Rendered when the operator clicks "Stop" in the Servis Kataloğu. The
 * modal walks the operator through a single explicit confirmation step
 * before any side-effect lands on the orchestrator:
 *
 *   1. The default body is always ``{remove_volumes: false, purge_vault:
 *      false}`` - equivalent to the legacy click-to-stop behaviour the
 *      Servis Kataloğu had before this task.
 *   2. The ``Advanced ▼`` toggle (collapsed by default) reveals two
 *      independent checkboxes:
 *        - ``Volume'ları sil`` (``remove_volumes``) - removes Compose
 *          named volumes alongside the container.
 *        - ``Vault override'ları sil`` (``purge_vault``) - deletes
 *          ``secret/services/{name}/*`` after Compose down. Disabled in
 *          production with an explanatory tooltip so the lifecycle
 *          guard's 403 cannot accidentally fire.
 *   3. On confirmation, ``POST /admin/services/{name}/stop`` is invoked
 *      with the resolved body. A 403 ``purge_vault_forbidden_in_production``
 *      is surfaced inline in the modal so the operator can untick the
 *      Vault checkbox and retry without losing their other selections.
 *
 * The component owns no polling or auto-retry. It calls back to the
 * parent on three paths: explicit Cancel, Escape / backdrop dismissal,
 * and successful stop (``onConfirmed``); the parent decides what to do
 * (close, refresh the catalog, etc.).
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { apiFetch } from "@/lib/api-client";

// ---------------------------------------------------------------------------
// Deployment profile detection.
// ---------------------------------------------------------------------------
//
// The lifecycle stop endpoint refuses ``purge_vault=true`` when
// ``settings.deployment_profile == "production"``. Surfacing the
// same value to the browser via ``NEXT_PUBLIC_DEPLOYMENT_PROFILE`` lets
// the UI disable the Vault checkbox proactively so the 403 is never
// triggered on the happy path. When the env is unset (most local-dev
// deployments) we fall back to ``"dev"`` - matching the backend default
// in :class:`src.config.Settings`.

function resolveDeploymentProfile(): string {
  return (process.env.NEXT_PUBLIC_DEPLOYMENT_PROFILE ?? "dev").trim();
}

function isProductionProfile(profile: string): boolean {
  return profile.toLowerCase() === "production";
}

// ---------------------------------------------------------------------------
// Wire types
// ---------------------------------------------------------------------------

/**
 * 403 ``purge_vault_forbidden_in_production`` envelope shape emitted by
 * :func:`stop_service`. Mirrors the FastAPI ``HTTPException`` body -
 * the FastAPI default wraps the ``detail`` dict, so the on-the-wire
 * payload is ``{detail: {error, detail}}``.
 */
type ProductionGuardEnvelope = {
  detail: {
    error: string;
    detail: string;
  };
};

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export type StopConfirmationModalProps = {
  /** Managed_Service ``name`` (path segment for the stop POST). */
  serviceName: string;
  /** Called when the modal is dismissed without a successful stop. */
  onClose: () => void;
  /**
   * Called *after* a successful 200 response from ``POST
   * /admin/services/{name}/stop``. The parent uses this to refresh the
   * catalog and clear any per-row busy spinner.
   */
  onConfirmed: () => void;
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function StopConfirmationModal({
  serviceName,
  onClose,
  onConfirmed,
}: StopConfirmationModalProps) {
  const profile = useMemo(resolveDeploymentProfile, []);
  const productionGuardActive = useMemo(
    () => isProductionProfile(profile),
    [profile],
  );

  const [advancedOpen, setAdvancedOpen] = useState<boolean>(false);
  const [removeVolumes, setRemoveVolumes] = useState<boolean>(false);
  const [purgeVault, setPurgeVault] = useState<boolean>(false);
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  // Surfaced separately so we can render a coloured callout for the
  // production guard rejection - operators routinely tick the wrong
  // box and expect actionable feedback rather than a generic toast.
  const [productionGuardError, setProductionGuardError] = useState<
    string | null
  >(null);

  // Close on Escape, mirroring StartFormModal / FeatureFlagDisabledModal.
  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      if (ev.key === "Escape" && !submitting) {
        onClose();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, submitting]);

  const handleConfirm = useCallback(async () => {
    setSubmitting(true);
    setError(null);
    setProductionGuardError(null);
    try {
      const res = await apiFetch(
        `/admin/services/${encodeURIComponent(serviceName)}/stop`,
        {
          method: "POST",
          body: JSON.stringify({
            remove_volumes: removeVolumes,
            purge_vault: purgeVault,
          }),
        },
      );
      if (res.ok) {
        onConfirmed();
        return;
      }

      // 403 + purge_vault_forbidden_in_production - render an inline,
      // operator-friendly explanation so the modal can be reused with
      // the Vault checkbox unticked.
      if (res.status === 403) {
        const body = (await res
          .json()
          .catch(() => null)) as ProductionGuardEnvelope | null;
        if (body?.detail?.error === "purge_vault_forbidden_in_production") {
          setProductionGuardError(
            body.detail.detail ??
              "purge_vault=true is forbidden when DEPLOYMENT_PROFILE resolves to 'production'.",
          );
          return;
        }
      }

      const text = await res.text().catch(() => "");
      setError(
        `stop ${serviceName} → HTTP ${res.status}${
          text ? `: ${text.slice(0, 200)}` : ""
        }`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }, [serviceName, removeVolumes, purgeVault, onConfirmed]);

  const summary = useMemo(() => {
    const parts: string[] = [];
    if (removeVolumes) parts.push("Volume'lar silinecek");
    if (purgeVault) parts.push("Vault override'ları silinecek");
    return parts;
  }, [removeVolumes, purgeVault]);

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.5)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 1100,
      }}
      role="presentation"
      onMouseDown={(ev) => {
        if (ev.target === ev.currentTarget && !submitting) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="stop-modal-title"
        aria-describedby="stop-modal-desc"
        style={{
          background: "#ffffff",
          borderRadius: 8,
          padding: "1.25rem 1.5rem 1.5rem",
          width: "min(520px, 92vw)",
          boxShadow: "0 10px 30px rgba(0,0,0,0.3)",
        }}
      >
        <h2
          id="stop-modal-title"
          style={{ marginTop: 0, fontSize: "1.1rem", color: "#111827" }}
        >
          Stop service?
        </h2>
        <p
          id="stop-modal-desc"
          style={{ margin: "0.4rem 0 1rem", color: "#374151" }}
        >
          <code
            style={{
              background: "#f3f4f6",
              padding: "0.1rem 0.35rem",
              borderRadius: 4,
              fontWeight: 700,
            }}
          >
            {serviceName}
          </code>{" "}
          servisini durdurmak üzeresin. Varsayılan olarak yalnızca container
          kapatılır; volume'lar ve Vault override'ları korunur.
        </p>

        {/* ----- Advanced toggle ----- */}
        <button
          type="button"
          aria-expanded={advancedOpen}
          aria-controls="stop-modal-advanced"
          onClick={() => setAdvancedOpen((v) => !v)}
          disabled={submitting}
          style={{
            background: "transparent",
            border: "none",
            padding: "0.25rem 0",
            color: "#1d4ed8",
            cursor: submitting ? "not-allowed" : "pointer",
            fontWeight: 600,
            fontSize: "0.9rem",
            display: "inline-flex",
            alignItems: "center",
            gap: "0.25rem",
          }}
        >
          <span aria-hidden>{advancedOpen ? "▼" : "▶"}</span>
          Advanced
        </button>

        {advancedOpen && (
          <fieldset
            id="stop-modal-advanced"
            disabled={submitting}
            style={{
              border: "1px solid #e5e7eb",
              borderRadius: 6,
              padding: "0.75rem 0.9rem",
              margin: "0.5rem 0 1rem",
            }}
          >
            <legend
              style={{
                padding: "0 0.4rem",
                fontSize: "0.8rem",
                color: "#6b7280",
                fontWeight: 600,
              }}
            >
              Tear-down options
            </legend>

            <label
              htmlFor="stop-modal-remove-volumes"
              style={checkboxRowStyle}
            >
              <input
                id="stop-modal-remove-volumes"
                type="checkbox"
                checked={removeVolumes}
                onChange={(ev) => setRemoveVolumes(ev.target.checked)}
              />
              <span style={{ display: "flex", flexDirection: "column" }}>
                <span style={{ fontWeight: 500 }}>Volume&apos;ları sil</span>
                <small style={{ color: "#6b7280" }}>
                  Compose named volume&apos;ları konteyner ile birlikte siler
                  (<code>docker compose down --volumes</code>).
                </small>
              </span>
            </label>

            <label
              htmlFor="stop-modal-purge-vault"
              style={{
                ...checkboxRowStyle,
                opacity: productionGuardActive ? 0.55 : 1,
                cursor: productionGuardActive ? "not-allowed" : "pointer",
              }}
              title={
                productionGuardActive
                  ? "Production'da Vault purge yasak"
                  : undefined
              }
            >
              <input
                id="stop-modal-purge-vault"
                type="checkbox"
                checked={purgeVault && !productionGuardActive}
                disabled={productionGuardActive}
                onChange={(ev) => setPurgeVault(ev.target.checked)}
              />
              <span style={{ display: "flex", flexDirection: "column" }}>
                <span style={{ fontWeight: 500 }}>
                  Vault override&apos;ları sil
                </span>
                <small style={{ color: "#6b7280" }}>
                  <code>secret/services/{serviceName}/*</code> altındaki
                  override&apos;ları durdurma sonrası siler.
                  {productionGuardActive && (
                    <>
                      {" "}
                      <strong style={{ color: "#92400e" }}>
                        Production&apos;da Vault purge yasak.
                      </strong>
                    </>
                  )}
                </small>
              </span>
            </label>
          </fieldset>
        )}

        {summary.length > 0 && (
          <div
            role="status"
            style={{
              fontSize: "0.85rem",
              color: "#92400e",
              background: "#fffbeb",
              border: "1px solid #fde68a",
              borderRadius: 6,
              padding: "0.5rem 0.75rem",
              marginBottom: "0.75rem",
            }}
          >
            <strong>Uyarı:</strong> {summary.join(" + ")}.
          </div>
        )}

        {productionGuardError && (
          <div
            role="alert"
            style={{
              fontSize: "0.85rem",
              color: "#7f1d1d",
              background: "#fee2e2",
              border: "1px solid #fecaca",
              borderRadius: 6,
              padding: "0.6rem 0.75rem",
              marginBottom: "0.75rem",
            }}
          >
            <strong>Vault purge reddedildi.</strong>{" "}
            {productionGuardError} Vault checkbox&apos;ını kaldırıp tekrar
            deneyin.
          </div>
        )}

        {error && (
          <div
            role="alert"
            style={{
              fontSize: "0.85rem",
              color: "#7f1d1d",
              background: "#fee2e2",
              border: "1px solid #fecaca",
              borderRadius: 6,
              padding: "0.6rem 0.75rem",
              marginBottom: "0.75rem",
            }}
          >
            {error}
          </div>
        )}

        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            gap: "0.5rem",
          }}
        >
          <button
            type="button"
            onClick={onClose}
            disabled={submitting}
            style={{
              padding: "0.45rem 0.95rem",
              border: "1px solid #d1d5db",
              borderRadius: 4,
              background: "#ffffff",
              cursor: submitting ? "not-allowed" : "pointer",
              fontSize: "0.9rem",
            }}
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={submitting}
            style={{
              padding: "0.45rem 0.95rem",
              border: "1px solid #b91c1c",
              borderRadius: 4,
              background: submitting ? "#fca5a5" : "#dc2626",
              color: "#ffffff",
              cursor: submitting ? "wait" : "pointer",
              fontSize: "0.9rem",
              fontWeight: 600,
            }}
          >
            {submitting ? "Stopping…" : "Stop"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Shared styles
// ---------------------------------------------------------------------------

const checkboxRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "flex-start",
  gap: "0.6rem",
  padding: "0.4rem 0",
  cursor: "pointer",
};
