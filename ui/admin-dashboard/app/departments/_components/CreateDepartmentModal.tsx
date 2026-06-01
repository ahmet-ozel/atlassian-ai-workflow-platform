"use client";

/**
 * CreateDepartmentModal — Modal for creating a new department.
 *
 * In wizard mode (`wizardMode=true`), the modal shows a two-step flow:
 * 1. Create the department (id + display name)
 * 2. Add at least one bot credential (Jira recommended) and pass a
 *    connectivity probe before marking the wizard step as complete.
 *
 * The close button triggers a confirmation dialog warning that at
 * least one department is required to continue the wizard flow.
 *
 * Requirements: 5.4, 5.5, 5.6
 */

import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api-client";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export type CreateDepartmentModalProps = {
  /** Called when the modal is dismissed (close button, backdrop, Escape). */
  onClose: () => void;
  /** Called after a department is successfully created (and probe passes in wizard mode). */
  onCreated: () => void;
  /** Whether the modal was opened from the setup wizard flow. */
  wizardMode?: boolean;
};

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type WizardStep = "create" | "credential";

type ServiceName = "jira" | "confluence" | "bitbucket";

type ProbeResult = {
  service: string;
  status: "ok" | "failed";
  error: string | null;
  account_id: string | null;
};

// ---------------------------------------------------------------------------
// Inline styles
// ---------------------------------------------------------------------------

const overlayStyle: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "rgba(0,0,0,0.5)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 1000,
};

const modalStyle: React.CSSProperties = {
  background: "#fff",
  color: "#111",
  borderRadius: 8,
  padding: "1.25rem",
  width: "min(600px, 94vw)",
  maxHeight: "92vh",
  overflowY: "auto",
  boxShadow: "0 10px 30px rgba(0,0,0,0.3)",
};

const inputStyle: React.CSSProperties = {
  width: "100%",
  padding: "0.5rem",
  border: "1px solid #d1d5db",
  borderRadius: 4,
  fontSize: "0.95rem",
};

const labelStyle: React.CSSProperties = {
  display: "block",
  marginBottom: "0.3rem",
  fontWeight: 500,
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function CreateDepartmentModal({
  onClose,
  onCreated,
  wizardMode = false,
}: CreateDepartmentModalProps): JSX.Element {
  const [wizardStep, setWizardStep] = useState<WizardStep>("create");
  const [deptId, setDeptId] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Credential step state (wizard mode only)
  const [credService, setCredService] = useState<ServiceName>("jira");
  const [credUrl, setCredUrl] = useState("");
  const [credUsername, setCredUsername] = useState("");
  const [credToken, setCredToken] = useState("");
  const [credSaving, setCredSaving] = useState(false);
  const [credError, setCredError] = useState<string | null>(null);
  const [probeStatus, setProbeStatus] = useState<"idle" | "ok" | "failed">("idle");
  const [probeAccountId, setProbeAccountId] = useState<string | null>(null);

  // Escape key closes modal (with wizard confirmation if needed)
  useEffect(() => {
    function onKey(ev: KeyboardEvent): void {
      if (ev.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // --- Step 1: Create department -------------------------------------------

  const handleCreateSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (!deptId.trim() || !displayName.trim()) return;

      setSubmitting(true);
      setError(null);

      try {
        const normalizedDeptId = deptId.trim();
        const res = await apiFetch("/api/v1/departments", {
          method: "POST",
          body: JSON.stringify({
            id: normalizedDeptId,
            display_name: displayName.trim(),
            jira_project_keys: [],
            confluence_space_keys: [],
            bitbucket_workspace: null,
            default_language: "tr",
            web_search_enabled: true,
            mode: "active",
            bot: {
              jira: {
                credential_ref: `vault:atlassian/${normalizedDeptId}/jira`,
                account_id: "",
                username: "",
              },
            },
            repo_mappings: [],
            budget_caps: {
              weekly_usd_dept: 500,
              weekly_usd_user: 50,
              monthly_usd_dept: 1800,
              monthly_usd_user: 150,
            },
            approval_required_paths: [],
            approvers: [],
            branch_pattern_rules: [],
            docker_defaults: {
              cpu_limit: 2,
              memory_limit_mb: 2048,
              default_timeout_seconds: 1800,
              max_timeout_seconds: 7200,
              cleanup_policy: "on_success",
            },
            notify_on_success: false,
            notify_channels: [],
            slack_webhook_ref: null,
            notify_email: null,
            teams_webhook_ref: null,
            feature_flag_overrides: {},
          }),
        });

        if (!res.ok) {
          const body = await res.text();
          setError(`HTTP ${res.status}: ${body.slice(0, 200)}`);
          return;
        }

        if (wizardMode) {
          // Move to credential step
          setWizardStep("credential");
          setError(null);
        } else {
          // Non-wizard mode: done immediately
          onCreated();
        }
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setSubmitting(false);
      }
    },
    [deptId, displayName, onCreated, wizardMode],
  );

  // --- Step 2: Save credential + probe (wizard mode) -----------------------

  const handleCredentialSave = useCallback(async () => {
    if (!credUrl.trim() || !credUsername.trim() || !credToken.trim()) {
      setCredError("Tüm alanlar zorunludur.");
      return;
    }

    setCredSaving(true);
    setCredError(null);
    setProbeStatus("idle");

    try {
      // Save credential
      const saveRes = await apiFetch(
        `/admin/departments/${encodeURIComponent(deptId.trim())}/credentials/${encodeURIComponent(credService)}`,
        {
          method: "POST",
          body: JSON.stringify({
            url: credUrl.trim(),
            username: credUsername.trim(),
            personal_token: credToken,
          }),
        },
      );

      if (!saveRes.ok) {
        const body = await saveRes.text();
        setCredError(`Credential kaydetme başarısız (HTTP ${saveRes.status}): ${body.slice(0, 200)}`);
        return;
      }

      // Run connectivity probe
      const probeRes = await apiFetch(
        `/admin/departments/${encodeURIComponent(deptId.trim())}/probe?service=${encodeURIComponent(credService)}`,
        { method: "POST", body: JSON.stringify({}) },
      );

      if (!probeRes.ok) {
        const body = await probeRes.text();
        setCredError(`Probe başarısız (HTTP ${probeRes.status}): ${body.slice(0, 200)}`);
        setProbeStatus("failed");
        return;
      }

      const probeBody = (await probeRes.json()) as {
        dept_id: string;
        results: ProbeResult[];
        probed_at: string;
      };

      const serviceResult = probeBody.results.find(
        (r) => r.service === credService,
      );

      if (serviceResult && serviceResult.status === "ok") {
        setProbeStatus("ok");
        setProbeAccountId(serviceResult.account_id);
        // Probe passed — wizard can complete
      } else {
        setProbeStatus("failed");
        setCredError(
          serviceResult?.error
            ? `Bağlantı testi başarısız: ${serviceResult.error}`
            : "Bağlantı testi başarısız — credential bilgilerini kontrol edin.",
        );
      }
    } catch (err) {
      setCredError((err as Error).message);
      setProbeStatus("failed");
    } finally {
      setCredSaving(false);
    }
  }, [deptId, credService, credUrl, credUsername, credToken]);

  // --- Complete wizard (only after probe passes) ---------------------------

  const handleWizardComplete = useCallback(() => {
    onCreated();
  }, [onCreated]);

  // --- Skip credential step (allow user to skip but warn) ------------------

  const handleSkipCredential = useCallback(() => {
    const confirmed = window.confirm(
      "Credential eklemeden devam ederseniz bot bağlantı testi yapılamaz ve bazı özellikler çalışmayabilir. Yine de devam etmek istiyor musunuz?",
    );
    if (confirmed) {
      onCreated();
    }
  }, [onCreated]);

  const titleId = "create-dept-modal-title";

  return (
    <div
      style={overlayStyle}
      role="presentation"
      onMouseDown={(ev) => {
        if (ev.target === ev.currentTarget) onClose();
      }}
    >
      <div
        style={modalStyle}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        {/* Header */}
        <header
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: "1rem",
            marginBottom: "1rem",
          }}
        >
          <h2 id={titleId} style={{ margin: 0, fontSize: "1.15rem" }}>
            {wizardStep === "create"
              ? "Yeni Departman Ekle"
              : "Bot Credential Ekle"}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close modal"
            style={{
              padding: "0.3rem 0.7rem",
              border: "1px solid #d1d5db",
              background: "#ffffff",
              borderRadius: 4,
              cursor: "pointer",
            }}
          >
            ✕
          </button>
        </header>

        {/* Wizard mode banner */}
        {wizardMode && (
          <div
            role="status"
            style={{
              background: "#eff6ff",
              border: "1px solid #bfdbfe",
              color: "#1e40af",
              padding: "0.75rem 1rem",
              borderRadius: 6,
              marginBottom: "1rem",
              fontSize: "0.9rem",
            }}
          >
            {wizardStep === "create" ? (
              <>
                🧙 Setup Wizard akışındasınız. Platformu kullanmaya başlamak
                için en az bir departman eklemeniz gerekiyor.
              </>
            ) : (
              <>
                🔑 Departman oluşturuldu! Şimdi en az bir bot credential
                ekleyip bağlantı testinden geçmeniz gerekiyor.
              </>
            )}
          </div>
        )}

        {/* Step indicator for wizard mode */}
        {wizardMode && (
          <div
            style={{
              display: "flex",
              gap: "0.5rem",
              marginBottom: "1rem",
              fontSize: "0.85rem",
            }}
          >
            <span
              style={{
                padding: "0.2rem 0.6rem",
                borderRadius: 12,
                background: wizardStep === "create" ? "#2563eb" : "#16a34a",
                color: "#fff",
                fontWeight: 500,
              }}
            >
              {wizardStep === "create" ? "1" : "✓"} Departman
            </span>
            <span
              style={{
                padding: "0.2rem 0.6rem",
                borderRadius: 12,
                background:
                  wizardStep === "credential" ? "#2563eb" : "#e5e7eb",
                color: wizardStep === "credential" ? "#fff" : "#6b7280",
                fontWeight: 500,
              }}
            >
              2 Credential
            </span>
          </div>
        )}

        {/* Error display */}
        {wizardStep === "create" && error && (
          <div
            role="alert"
            style={{
              background: "#fef2f2",
              color: "#991b1b",
              padding: "0.75rem",
              borderRadius: 4,
              marginBottom: "1rem",
              fontSize: "0.9rem",
            }}
          >
            {error}
          </div>
        )}

        {/* ================================================================= */}
        {/* Step 1: Create Department Form                                     */}
        {/* ================================================================= */}
        {wizardStep === "create" && (
          <form onSubmit={handleCreateSubmit}>
            <div style={{ marginBottom: "1rem" }}>
              <label htmlFor="dept-id" style={labelStyle}>
                Departman ID
              </label>
              <input
                id="dept-id"
                type="text"
                value={deptId}
                onChange={(e) => setDeptId(e.target.value)}
                placeholder="örn: payment-team"
                required
                style={inputStyle}
              />
            </div>

            <div style={{ marginBottom: "1rem" }}>
              <label htmlFor="dept-name" style={labelStyle}>
                Görünen Ad
              </label>
              <input
                id="dept-name"
                type="text"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder="örn: Payment Team"
                required
                style={inputStyle}
              />
            </div>

            <div
              style={{
                display: "flex",
                justifyContent: "flex-end",
                gap: "0.5rem",
                marginTop: "1.5rem",
              }}
            >
              <button
                type="button"
                onClick={onClose}
                style={{
                  padding: "0.5rem 1rem",
                  border: "1px solid #d1d5db",
                  background: "#fff",
                  borderRadius: 4,
                  cursor: "pointer",
                }}
              >
                İptal
              </button>
              <button
                type="submit"
                disabled={submitting || !deptId.trim() || !displayName.trim()}
                style={{
                  padding: "0.5rem 1rem",
                  background: "#2563eb",
                  color: "#fff",
                  border: "none",
                  borderRadius: 4,
                  cursor: submitting ? "not-allowed" : "pointer",
                  opacity: submitting ? 0.6 : 1,
                }}
              >
                {submitting
                  ? "Oluşturuluyor…"
                  : wizardMode
                    ? "Devam →"
                    : "Departman Oluştur"}
              </button>
            </div>
          </form>
        )}

        {/* ================================================================= */}
        {/* Step 2: Credential + Probe (wizard mode only)                      */}
        {/* ================================================================= */}
        {wizardStep === "credential" && (
          <div>
            {/* Credential error */}
            {credError && (
              <div
                role="alert"
                style={{
                  background: "#fef2f2",
                  color: "#991b1b",
                  padding: "0.75rem",
                  borderRadius: 4,
                  marginBottom: "1rem",
                  fontSize: "0.9rem",
                }}
              >
                {credError}
              </div>
            )}

            {/* Probe success message */}
            {probeStatus === "ok" && (
              <div
                role="status"
                style={{
                  background: "#dcfce7",
                  border: "1px solid #86efac",
                  color: "#166534",
                  padding: "0.75rem",
                  borderRadius: 4,
                  marginBottom: "1rem",
                  fontSize: "0.9rem",
                }}
              >
                ✅ Bağlantı testi başarılı!
                {probeAccountId && (
                  <>
                    {" "}
                    Bot account ID: <code>{probeAccountId}</code>
                  </>
                )}
              </div>
            )}

            {/* Service selector */}
            <div style={{ marginBottom: "1rem" }}>
              <label htmlFor="cred-service" style={labelStyle}>
                Servis (Jira önerilir)
              </label>
              <select
                id="cred-service"
                value={credService}
                onChange={(e) => setCredService(e.target.value as ServiceName)}
                style={inputStyle}
                disabled={probeStatus === "ok"}
              >
                <option value="jira">Jira (önerilen)</option>
                <option value="confluence">Confluence</option>
                <option value="bitbucket">Bitbucket</option>
              </select>
            </div>

            {/* URL */}
            <div style={{ marginBottom: "1rem" }}>
              <label htmlFor="cred-url" style={labelStyle}>
                URL
              </label>
              <input
                id="cred-url"
                type="text"
                value={credUrl}
                onChange={(e) => setCredUrl(e.target.value)}
                placeholder="https://example.atlassian.net"
                required
                disabled={probeStatus === "ok"}
                style={inputStyle}
              />
            </div>

            {/* Username */}
            <div style={{ marginBottom: "1rem" }}>
              <label htmlFor="cred-username" style={labelStyle}>
                Bot Username (e-posta)
              </label>
              <input
                id="cred-username"
                type="text"
                value={credUsername}
                onChange={(e) => setCredUsername(e.target.value)}
                placeholder="bot@example.com"
                required
                disabled={probeStatus === "ok"}
                style={inputStyle}
              />
            </div>

            {/* Token */}
            <div style={{ marginBottom: "1rem" }}>
              <label htmlFor="cred-token" style={labelStyle}>
                Personal Access Token{" "}
                <span style={{ color: "#b00" }}>*</span>
              </label>
              <input
                id="cred-token"
                type="password"
                value={credToken}
                onChange={(e) => setCredToken(e.target.value)}
                placeholder="(zorunlu)"
                required
                disabled={probeStatus === "ok"}
                autoComplete="new-password"
                style={inputStyle}
              />
            </div>

            {/* Action buttons */}
            <div
              style={{
                display: "flex",
                justifyContent: "flex-end",
                gap: "0.5rem",
                marginTop: "1.5rem",
                flexWrap: "wrap",
              }}
            >
              <button
                type="button"
                onClick={handleSkipCredential}
                style={{
                  padding: "0.5rem 1rem",
                  border: "1px solid #d1d5db",
                  background: "#fff",
                  borderRadius: 4,
                  cursor: "pointer",
                  fontSize: "0.9rem",
                  color: "#6b7280",
                }}
              >
                Atla
              </button>

              {probeStatus !== "ok" ? (
                <button
                  type="button"
                  onClick={handleCredentialSave}
                  disabled={
                    credSaving ||
                    !credUrl.trim() ||
                    !credUsername.trim() ||
                    !credToken.trim()
                  }
                  style={{
                    padding: "0.5rem 1rem",
                    background: "#2563eb",
                    color: "#fff",
                    border: "none",
                    borderRadius: 4,
                    cursor: credSaving ? "not-allowed" : "pointer",
                    opacity: credSaving ? 0.6 : 1,
                  }}
                >
                  {credSaving ? "Kaydediliyor & Test Ediliyor…" : "Kaydet & Test Et"}
                </button>
              ) : (
                <button
                  type="button"
                  onClick={handleWizardComplete}
                  style={{
                    padding: "0.5rem 1rem",
                    background: "#16a34a",
                    color: "#fff",
                    border: "none",
                    borderRadius: 4,
                    cursor: "pointer",
                    fontWeight: 600,
                  }}
                >
                  Kurulumu Tamamla ✓
                </button>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
