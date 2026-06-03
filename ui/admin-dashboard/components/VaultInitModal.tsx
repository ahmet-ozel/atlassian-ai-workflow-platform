"use client";

/**
 * VaultInitModal — Production Vault Init step for the Setup Wizard.
 *
 * Calls `POST /admin/vault/init` and
 * displays the 5 unseal keys in a one-time modal dialog. The modal
 * cannot be closed without the operator confirming they have saved the
 * keys. Once closed, keys are cleared from the UI and cannot be
 * retrieved again.
 *
 */

import { useCallback, useRef, useState } from "react";

import { getAdminApiBaseUrl } from "@/lib/config";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type VaultInitModalProps = {
  /** Called after the modal is closed and keys are cleared. */
  onComplete?: () => void;
};

type VaultInitResponse = {
  unseal_keys: string[];
  unseal_keys_base64: string[];
  root_token: string;
  message: string;
};

type ModalState =
  | { phase: "idle" }
  | { phase: "loading" }
  | { phase: "display"; data: VaultInitResponse }
  | { phase: "error"; message: string }
  | { phase: "closed" };

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function VaultInitModal({ onComplete }: VaultInitModalProps) {
  const [state, setState] = useState<ModalState>({ phase: "idle" });
  const [confirmed, setConfirmed] = useState(false);
  const keysRef = useRef<VaultInitResponse | null>(null);

  // ---- Trigger Vault init ----
  const handleInit = useCallback(async () => {
    setState({ phase: "loading" });
    setConfirmed(false);

    try {
      const baseUrl = getAdminApiBaseUrl();
      const response = await fetch(`${baseUrl}/admin/vault/init`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });

      if (response.status === 409) {
        setState({
          phase: "error",
          message: "Vault zaten initialize edilmiş. Tekrar init yapılamaz.",
        });
        return;
      }

      if (!response.ok) {
        const errorBody = await response.json().catch(() => null);
        const errorMsg =
          errorBody?.detail?.message ??
          errorBody?.error ??
          `HTTP ${response.status}`;
        setState({ phase: "error", message: `Vault init başarısız: ${errorMsg}` });
        return;
      }

      const data: VaultInitResponse = await response.json();
      keysRef.current = data;
      setState({ phase: "display", data });
    } catch (err) {
      setState({
        phase: "error",
        message: `Ağ hatası: ${err instanceof Error ? err.message : "Bilinmeyen hata"}`,
      });
    }
  }, []);

  // ---- Close modal and clear keys ----
  const handleClose = useCallback(() => {
    // Clear keys from memory
    keysRef.current = null;
    setState({ phase: "closed" });
    setConfirmed(false);
    onComplete?.();
  }, [onComplete]);

  // ---- Render: Idle state — show init button ----
  if (state.phase === "idle") {
    return (
      <div style={stepContainerStyle}>
        <h3 style={headingStyle}>Production Vault Init</h3>
        <p style={descriptionStyle}>
          Vault&apos;u production modda initialize edin. Bu işlem 5 unseal key ve
          1 root token üretecektir. Key&apos;ler yalnızca bir kez gösterilir.
        </p>
        <button
          type="button"
          onClick={handleInit}
          style={primaryButtonStyle}
          aria-label="Vault'u initialize et"
        >
          Vault&apos;u Initialize Et
        </button>
      </div>
    );
  }

  // ---- Render: Loading state ----
  if (state.phase === "loading") {
    return (
      <div style={stepContainerStyle}>
        <h3 style={headingStyle}>Production Vault Init</h3>
        <div style={loadingStyle} role="status" aria-live="polite">
          <span aria-hidden="true">⏳</span> Vault initialize ediliyor...
        </div>
      </div>
    );
  }

  // ---- Render: Error state ----
  if (state.phase === "error") {
    return (
      <div style={stepContainerStyle}>
        <h3 style={headingStyle}>Production Vault Init</h3>
        <div role="alert" style={errorBannerStyle}>
          <span aria-hidden="true">❌</span> {state.message}
        </div>
        <button
          type="button"
          onClick={() => setState({ phase: "idle" })}
          style={secondaryButtonStyle}
        >
          Tekrar Dene
        </button>
      </div>
    );
  }

  // ---- Render: Closed state (keys already cleared) ----
  if (state.phase === "closed") {
    return (
      <div style={stepContainerStyle}>
        <h3 style={headingStyle}>Production Vault Init</h3>
        <div style={successBannerStyle} role="status">
          <span aria-hidden="true">✅</span> Vault başarıyla initialize edildi.
          Unseal key&apos;ler güvenli bir şekilde saklandığınızdan emin olun.
        </div>
      </div>
    );
  }

  // ---- Render: Display keys modal ----
  const { data } = state;

  return (
    <div style={overlayStyle} role="dialog" aria-modal="true" aria-labelledby="vault-init-modal-title">
      <div style={modalStyle}>
        <h3 id="vault-init-modal-title" style={modalHeadingStyle}>
          🔐 Vault Unseal Keys
        </h3>

        <div style={warningBannerStyle} role="alert">
          <strong>DİKKAT:</strong> Bu key&apos;ler yalnızca bir kez gösterilir.
          Güvenli bir yere kaydedin. Modal kapatıldıktan sonra tekrar
          erişilemez.
        </div>

        {/* Unseal Keys */}
        <div style={keySectionStyle}>
          <h4 style={keySectionHeadingStyle}>Unseal Keys (5 adet)</h4>
          <ol style={keyListStyle}>
            {data.unseal_keys.map((key, index) => (
              <li key={index} style={keyItemStyle}>
                <code style={keyCodeStyle}>{key}</code>
              </li>
            ))}
          </ol>
        </div>

        {/* Root Token */}
        <div style={keySectionStyle}>
          <h4 style={keySectionHeadingStyle}>Root Token</h4>
          <code style={keyCodeStyle}>{data.root_token}</code>
        </div>

        {/* Confirmation checkbox */}
        <label style={checkboxLabelStyle}>
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(e) => setConfirmed(e.target.checked)}
            style={checkboxStyle}
            aria-describedby="vault-confirm-description"
          />
          <span id="vault-confirm-description">
            Unseal key&apos;leri güvenli bir yere kaydettim. Bu key&apos;lerin
            tekrar gösterilmeyeceğini anlıyorum.
          </span>
        </label>

        {/* Close button — disabled until confirmed */}
        <button
          type="button"
          onClick={handleClose}
          disabled={!confirmed}
          style={confirmed ? primaryButtonStyle : disabledButtonStyle}
          aria-disabled={!confirmed}
        >
          Onayla ve Kapat
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Styles
// ---------------------------------------------------------------------------

const stepContainerStyle: React.CSSProperties = {
  padding: "1.5rem",
  border: "1px solid #e5e7eb",
  borderRadius: "0.5rem",
  background: "#ffffff",
  maxWidth: "640px",
};

const headingStyle: React.CSSProperties = {
  margin: "0 0 0.75rem 0",
  fontSize: "1.25rem",
  fontWeight: 600,
  color: "#111827",
};

const descriptionStyle: React.CSSProperties = {
  margin: "0 0 1rem 0",
  color: "#4b5563",
  lineHeight: 1.5,
};

const primaryButtonStyle: React.CSSProperties = {
  padding: "0.625rem 1.25rem",
  background: "#2563eb",
  color: "#ffffff",
  border: "none",
  borderRadius: "0.375rem",
  fontSize: "0.9rem",
  fontWeight: 500,
  cursor: "pointer",
};

const secondaryButtonStyle: React.CSSProperties = {
  padding: "0.625rem 1.25rem",
  background: "#f3f4f6",
  color: "#374151",
  border: "1px solid #d1d5db",
  borderRadius: "0.375rem",
  fontSize: "0.9rem",
  fontWeight: 500,
  cursor: "pointer",
};

const disabledButtonStyle: React.CSSProperties = {
  ...primaryButtonStyle,
  background: "#9ca3af",
  cursor: "not-allowed",
  opacity: 0.7,
};

const loadingStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "0.5rem",
  color: "#6b7280",
  fontSize: "0.9rem",
};

const errorBannerStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "0.5rem",
  padding: "0.75rem 1rem",
  background: "#fef2f2",
  color: "#991b1b",
  borderRadius: "0.375rem",
  border: "1px solid #fecaca",
  fontSize: "0.9rem",
  marginBottom: "1rem",
};

const successBannerStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "0.5rem",
  padding: "0.75rem 1rem",
  background: "#f0fdf4",
  color: "#166534",
  borderRadius: "0.375rem",
  border: "1px solid #bbf7d0",
  fontSize: "0.9rem",
};

const overlayStyle: React.CSSProperties = {
  position: "fixed",
  inset: 0,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  background: "rgba(0, 0, 0, 0.6)",
  zIndex: 9999,
  padding: "1rem",
};

const modalStyle: React.CSSProperties = {
  background: "#ffffff",
  borderRadius: "0.75rem",
  padding: "2rem",
  maxWidth: "640px",
  width: "100%",
  maxHeight: "90vh",
  overflowY: "auto",
  boxShadow: "0 25px 50px -12px rgba(0, 0, 0, 0.25)",
};

const modalHeadingStyle: React.CSSProperties = {
  margin: "0 0 1rem 0",
  fontSize: "1.25rem",
  fontWeight: 600,
  color: "#111827",
};

const warningBannerStyle: React.CSSProperties = {
  padding: "0.75rem 1rem",
  background: "#fffbeb",
  color: "#92400e",
  borderRadius: "0.375rem",
  border: "1px solid #fcd34d",
  fontSize: "0.85rem",
  lineHeight: 1.5,
  marginBottom: "1.25rem",
};

const keySectionStyle: React.CSSProperties = {
  marginBottom: "1.25rem",
};

const keySectionHeadingStyle: React.CSSProperties = {
  margin: "0 0 0.5rem 0",
  fontSize: "0.9rem",
  fontWeight: 600,
  color: "#374151",
};

const keyListStyle: React.CSSProperties = {
  margin: 0,
  padding: "0 0 0 1.5rem",
  listStyleType: "decimal",
};

const keyItemStyle: React.CSSProperties = {
  marginBottom: "0.375rem",
};

const keyCodeStyle: React.CSSProperties = {
  display: "inline-block",
  padding: "0.25rem 0.5rem",
  background: "#f3f4f6",
  borderRadius: "0.25rem",
  fontFamily: "monospace",
  fontSize: "0.8rem",
  wordBreak: "break-all",
  color: "#1f2937",
};

const checkboxLabelStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "flex-start",
  gap: "0.5rem",
  padding: "1rem",
  background: "#f9fafb",
  borderRadius: "0.375rem",
  border: "1px solid #e5e7eb",
  marginBottom: "1.25rem",
  cursor: "pointer",
  fontSize: "0.85rem",
  lineHeight: 1.5,
  color: "#374151",
};

const checkboxStyle: React.CSSProperties = {
  marginTop: "0.2rem",
  flexShrink: 0,
  width: "1rem",
  height: "1rem",
};
