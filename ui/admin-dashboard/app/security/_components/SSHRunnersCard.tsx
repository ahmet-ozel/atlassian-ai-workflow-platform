"use client";

/**
 * SSHRunnersCard — Displays SSH runners with key rotation controls.
 *
 * Fetches runner data from `GET /admin/ssh-runners` (list endpoint, EK4
 * fix; the rotation endpoints below live under `/admin/security/...`)
 * and renders
 * each runner with host info, fingerprint, last rotation date, and action
 * buttons for key rotation, known_hosts refresh, and rotation finalization.
 *
 * Requirements: 8.5, 8.6
 */

import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api-client";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type SSHRunner = {
  runner_id: string;
  host: string;
  port?: number;
  username?: string;
  base_path?: string;
  vault_path?: string;
  created_at?: string;
  updated_at?: string;
  last_rotated_at?: string | null;
  active_key_fingerprint?: string | null;
  previous_key_fingerprint?: string | null;
  known_hosts_fingerprint?: string | null;
  status: "ok" | "key_expired" | "unreachable" | "active" | "disabled" | "quarantine" | string;
};

type RotateKeyResponse = {
  public_key: string;
};

// ---------------------------------------------------------------------------
// Inline styles
// ---------------------------------------------------------------------------

const cardStyle: React.CSSProperties = {
  border: "1px solid #e5e7eb",
  borderRadius: 8,
  padding: "1.25rem",
  marginTop: "1.5rem",
};

const tableStyle: React.CSSProperties = {
  width: "100%",
  borderCollapse: "collapse",
  fontSize: "0.9rem",
};

const thStyle: React.CSSProperties = {
  borderBottom: "1px solid #ccc",
  textAlign: "left",
  padding: "0.5rem 0.75rem",
  fontWeight: 600,
};

const tdStyle: React.CSSProperties = {
  borderBottom: "1px solid #eee",
  padding: "0.5rem 0.75rem",
  verticalAlign: "middle",
};

const btnStyle: React.CSSProperties = {
  padding: "0.35rem 0.7rem",
  border: "1px solid #d1d5db",
  background: "#fff",
  borderRadius: 4,
  cursor: "pointer",
  fontSize: "0.8rem",
  marginRight: "0.4rem",
};

const btnPrimaryStyle: React.CSSProperties = {
  ...btnStyle,
  background: "#2563eb",
  color: "#fff",
  border: "none",
};

const btnDangerStyle: React.CSSProperties = {
  ...btnStyle,
  background: "#dc2626",
  color: "#fff",
  border: "none",
};

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
  padding: "1.5rem",
  width: "min(560px, 94vw)",
  maxHeight: "80vh",
  overflowY: "auto",
  boxShadow: "0 10px 30px rgba(0,0,0,0.3)",
};

const codeBlockStyle: React.CSSProperties = {
  background: "#f3f4f6",
  border: "1px solid #e5e7eb",
  borderRadius: 4,
  padding: "0.75rem 1rem",
  fontFamily: "monospace",
  fontSize: "0.85rem",
  wordBreak: "break-all",
  whiteSpace: "pre-wrap",
  marginTop: "0.75rem",
};

const statusBadge = (status: SSHRunner["status"]): React.CSSProperties => {
  const colors: Record<string, { bg: string; color: string }> = {
    ok: { bg: "#dcfce7", color: "#166534" },
    active: { bg: "#dcfce7", color: "#166534" },
    key_expired: { bg: "#fef9c3", color: "#854d0e" },
    quarantine: { bg: "#fef9c3", color: "#854d0e" },
    unreachable: { bg: "#fef2f2", color: "#991b1b" },
    disabled: { bg: "#f3f4f6", color: "#374151" },
  };
  const c = colors[status] ?? colors.disabled;
  return {
    display: "inline-block",
    padding: "0.15rem 0.5rem",
    borderRadius: 12,
    fontSize: "0.75rem",
    fontWeight: 600,
    background: c.bg,
    color: c.color,
  };
};

const statusLabel = (status: SSHRunner["status"]): string => {
  switch (status) {
    case "ok":
    case "active":
      return "OK";
    case "key_expired":
      return "Süresi Dolmuş";
    case "unreachable":
      return "Erişilemez";
    case "disabled":
      return "Devre Dışı";
    case "quarantine":
      return "Karantina";
    default:
      return status || "Bilinmiyor";
  }
};

const fingerprintPreview = (runner: SSHRunner): string => {
  const value =
    runner.active_key_fingerprint ??
    runner.known_hosts_fingerprint ??
    runner.vault_path ??
    "";
  return value ? `${value.slice(0, 16)}…` : "—";
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function SSHRunnersCard(): JSX.Element {
  const [runners, setRunners] = useState<SSHRunner[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Confirmation dialog state
  const [confirmAction, setConfirmAction] = useState<{
    type: "rotate-key" | "rotate-known-hosts" | "finalize";
    runnerId: string;
    host: string;
  } | null>(null);

  // Modal state for showing new public key after rotation
  const [rotateResult, setRotateResult] = useState<{
    publicKey: string;
    host: string;
  } | null>(null);

  const [actionLoading, setActionLoading] = useState(false);

  // --- Fetch runners -------------------------------------------------------

  const fetchRunners = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // EK4 fix: backend router is mounted at /admin/ssh-runners (see
      // services/admin-dashboard-api/src/routers/ssh_runners.py — prefix
      // "/admin/ssh-runners"). The /admin/security/ prefix returned 404.
      const res = await apiFetch("/admin/ssh-runners");
      if (!res.ok) {
        const body = await res.text();
        setError(`HTTP ${res.status}: ${body.slice(0, 200)}`);
        return;
      }
      const data = (await res.json()) as { runners: SSHRunner[] };
      setRunners(data.runners ?? []);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchRunners();
  }, [fetchRunners]);

  // --- Action handlers -----------------------------------------------------

  const handleConfirm = useCallback(async () => {
    if (!confirmAction) return;

    setActionLoading(true);
    setError(null);

    try {
      const { type, runnerId, host } = confirmAction;
      let endpoint = "";

      switch (type) {
        case "rotate-key":
          endpoint = `/admin/security/ssh-runners/${encodeURIComponent(runnerId)}/rotate-key`;
          break;
        case "rotate-known-hosts":
          endpoint = `/admin/security/ssh-runners/${encodeURIComponent(runnerId)}/rotate-known-hosts`;
          break;
        case "finalize":
          endpoint = `/admin/security/ssh-runners/${encodeURIComponent(runnerId)}/finalize-rotation`;
          break;
      }

      const res = await apiFetch(endpoint, { method: "POST" });

      if (!res.ok) {
        const body = await res.text();
        setError(`İşlem başarısız (HTTP ${res.status}): ${body.slice(0, 200)}`);
        setConfirmAction(null);
        return;
      }

      if (type === "rotate-key") {
        const data = (await res.json()) as RotateKeyResponse;
        setRotateResult({ publicKey: data.public_key, host });
      }

      setConfirmAction(null);
      // Refresh runner list
      await fetchRunners();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setActionLoading(false);
    }
  }, [confirmAction, fetchRunners]);

  // --- Helpers -------------------------------------------------------------

  const formatDate = (dateStr?: string | null): string => {
    if (!dateStr) return "—";
    try {
      return new Date(dateStr).toLocaleString("tr-TR");
    } catch {
      return dateStr;
    }
  };

  const confirmMessages: Record<string, string> = {
    "rotate-key":
      "SSH anahtarını döndürmek istediğinizden emin misiniz? Yeni anahtar üretilecek ve mevcut anahtar yedek slot'a taşınacak.",
    "rotate-known-hosts":
      "Known hosts fingerprint'ini yenilemek istediğinizden emin misiniz? Hedef sunucuya bağlanılarak yeni fingerprint alınacak.",
    finalize:
      "Rotation'ı sonlandırmak istediğinizden emin misiniz? Yedek (previous) slot silinecek — yeni anahtarı sunucuya eklediğinizden emin olun.",
  };

  // --- Render --------------------------------------------------------------

  return (
    <section style={cardStyle}>
      <h2 style={{ margin: "0 0 0.75rem 0", fontSize: "1.1rem" }}>
        SSH Runners
      </h2>

      {error && (
        <p role="alert" style={{ color: "crimson", fontSize: "0.9rem" }}>
          Hata: {error}
        </p>
      )}

      {loading && runners.length === 0 ? (
        <p style={{ color: "#6b7280" }}>Yükleniyor…</p>
      ) : runners.length === 0 ? (
        <p style={{ color: "#6b7280" }}>Kayıtlı SSH runner bulunamadı.</p>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={tableStyle}>
            <thead>
              <tr>
                <th style={thStyle}>Host</th>
                <th style={thStyle}>Son Rotation</th>
                <th style={thStyle}>Fingerprint</th>
                <th style={thStyle}>Durum</th>
                <th style={thStyle}>İşlemler</th>
              </tr>
            </thead>
            <tbody>
              {runners.map((runner) => (
                <tr key={runner.runner_id}>
                  <td style={tdStyle}>
                    <strong>{runner.host}</strong>
                    {runner.port && runner.port !== 22 && (
                      <span style={{ color: "#6b7280" }}>:{runner.port}</span>
                    )}
                  </td>
                  <td style={tdStyle}>{formatDate(runner.last_rotated_at)}</td>
                  <td style={tdStyle}>
                    <code style={{ fontSize: "0.8rem" }}>
                      {fingerprintPreview(runner)}
                    </code>
                  </td>
                  <td style={tdStyle}>
                    <span style={statusBadge(runner.status)}>
                      {statusLabel(runner.status)}
                    </span>
                  </td>
                  <td style={tdStyle}>
                    <button
                      type="button"
                      style={btnPrimaryStyle}
                      onClick={() =>
                        setConfirmAction({
                          type: "rotate-key",
                          runnerId: runner.runner_id,
                          host: runner.host,
                        })
                      }
                    >
                      Anahtarı Döndür
                    </button>
                    <button
                      type="button"
                      style={btnStyle}
                      onClick={() =>
                        setConfirmAction({
                          type: "rotate-known-hosts",
                          runnerId: runner.runner_id,
                          host: runner.host,
                        })
                      }
                    >
                      Known Hosts Yenile
                    </button>
                    <button
                      type="button"
                      style={{
                        ...btnDangerStyle,
                        opacity: runner.previous_key_fingerprint ? 1 : 0.4,
                        cursor: runner.previous_key_fingerprint
                          ? "pointer"
                          : "not-allowed",
                      }}
                      disabled={!runner.previous_key_fingerprint}
                      onClick={() =>
                        setConfirmAction({
                          type: "finalize",
                          runnerId: runner.runner_id,
                          host: runner.host,
                        })
                      }
                    >
                      Rotation'ı Sonlandır
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* ================================================================= */}
      {/* Confirmation Dialog                                                */}
      {/* ================================================================= */}
      {confirmAction && (
        <div
          style={overlayStyle}
          role="presentation"
          onMouseDown={(ev) => {
            if (ev.target === ev.currentTarget) setConfirmAction(null);
          }}
        >
          <div
            style={modalStyle}
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="ssh-confirm-title"
            aria-describedby="ssh-confirm-desc"
          >
            <h3 id="ssh-confirm-title" style={{ margin: "0 0 0.75rem 0" }}>
              İşlemi Onayla
            </h3>
            <p id="ssh-confirm-desc" style={{ margin: "0 0 1.25rem 0" }}>
              {confirmMessages[confirmAction.type]}
            </p>
            <p style={{ fontSize: "0.85rem", color: "#6b7280" }}>
              Runner: <strong>{confirmAction.host}</strong>
            </p>
            <div
              style={{
                display: "flex",
                justifyContent: "flex-end",
                gap: "0.5rem",
                marginTop: "1.25rem",
              }}
            >
              <button
                type="button"
                style={btnStyle}
                onClick={() => setConfirmAction(null)}
                disabled={actionLoading}
              >
                İptal
              </button>
              <button
                type="button"
                style={btnPrimaryStyle}
                onClick={handleConfirm}
                disabled={actionLoading}
              >
                {actionLoading ? "İşleniyor…" : "Onayla"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ================================================================= */}
      {/* Rotate Result Modal — shows new public key + instructions          */}
      {/* ================================================================= */}
      {rotateResult && (
        <div
          style={overlayStyle}
          role="presentation"
          onMouseDown={(ev) => {
            if (ev.target === ev.currentTarget) setRotateResult(null);
          }}
        >
          <div
            style={modalStyle}
            role="dialog"
            aria-modal="true"
            aria-labelledby="ssh-result-title"
          >
            <h3 id="ssh-result-title" style={{ margin: "0 0 0.75rem 0" }}>
              🔑 Yeni SSH Public Key
            </h3>
            <p style={{ fontSize: "0.9rem", marginBottom: "0.5rem" }}>
              Aşağıdaki public key <strong>{rotateResult.host}</strong>{" "}
              sunucusu için üretildi. Lütfen bu anahtarı sunucudaki{" "}
              <code>~/.ssh/authorized_keys</code> dosyasına ekleyin.
            </p>
            <div
              style={{
                background: "#eff6ff",
                border: "1px solid #bfdbfe",
                color: "#1e40af",
                padding: "0.75rem 1rem",
                borderRadius: 6,
                fontSize: "0.85rem",
                marginBottom: "0.75rem",
              }}
            >
              ℹ️ Bu anahtarı sunucudaki <code>~/.ssh/authorized_keys</code>{" "}
              dosyasına ekleyin, sonra &quot;Rotation&apos;ı Sonlandır&quot;
              butonuna basın.
            </div>
            <div style={codeBlockStyle}>
              {rotateResult.publicKey}
            </div>
            <div
              style={{
                display: "flex",
                justifyContent: "flex-end",
                gap: "0.5rem",
                marginTop: "1.25rem",
              }}
            >
              <button
                type="button"
                style={btnStyle}
                onClick={() => {
                  void navigator.clipboard.writeText(rotateResult.publicKey);
                }}
              >
                📋 Kopyala
              </button>
              <button
                type="button"
                style={btnPrimaryStyle}
                onClick={() => setRotateResult(null)}
              >
                Tamam
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
