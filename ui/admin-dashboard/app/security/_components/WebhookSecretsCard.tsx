"use client";

/**
 * WebhookSecretsCard - Displays webhook HMAC secrets per dept × provider
 * with rotation controls and live overlap countdown.
 * * Fetches data from `GET /admin/security/webhooks` and provides:
 * - Last rotation timestamp per entry
 * - Live countdown for remaining overlap window
 * - "Döndür" (Rotate) button  modal with new secret + 3-step guide
 * - "Sonlandır" (Finalize) button  ends overlap early
 * * Requirements: 9.4, 9.5
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { apiFetch } from "@/lib/api-client";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type WebhookEntry = {
  dept_id: string;
  provider: "jira" | "bitbucket" | "confluence";
  last_rotated_at: string | null;
  overlap_window_remaining_s: number | null;
  status: "ok" | "overlap_active" | "never_rotated";
};

type RotateResponse = {
  new_secret: string;
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

const statusBadge = (
  status: WebhookEntry["status"],
): React.CSSProperties => {
  const colors: Record<
    WebhookEntry["status"],
    { bg: string; color: string }
  > = {
    ok: { bg: "#dcfce7", color: "#166534" },
    overlap_active: { bg: "#fef9c3", color: "#854d0e" },
    never_rotated: { bg: "#f3f4f6", color: "#6b7280" },
  };
  const c = colors[status] ?? colors.ok;
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

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatDate(dateStr: string | null): string {
  if (!dateStr) return "-";
  try {
    return new Date(dateStr).toLocaleString("tr-TR");
  } catch {
    return dateStr;
  }
}

function formatCountdown(totalSeconds: number): string {
  if (totalSeconds <= 0) return "0s";
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  if (h > 0) return `${h}sa ${m}dk`;
  if (m > 0) return `${m}dk ${s}s`;
  return `${s}s`;
}

function providerLabel(provider: WebhookEntry["provider"]): string {
  const labels: Record<WebhookEntry["provider"], string> = {
    jira: "Jira",
    bitbucket: "Bitbucket",
    confluence: "Confluence",
  };
  return labels[provider] ?? provider;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function WebhookSecretsCard(): JSX.Element {
  const [entries, setEntries] = useState<WebhookEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState(false);

  // Live countdown tick
  const [tick, setTick] = useState(0);
  const tickRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Confirmation dialog state
  const [confirmAction, setConfirmAction] = useState<{
    type: "rotate" | "finalize";
    deptId: string;
    provider: WebhookEntry["provider"];
  } | null>(null);

  // Rotate result modal state
  const [rotateResult, setRotateResult] = useState<{
    newSecret: string;
    deptId: string;
    provider: WebhookEntry["provider"];
  } | null>(null);

  // --- Fetch entries -------------------------------------------------------

  const fetchEntries = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch("/admin/security/webhooks");
      if (!res.ok) {
        const body = await res.text();
        setError(`HTTP ${res.status}: ${body.slice(0, 200)}`);
        return;
      }
      const data = (await res.json()) as { entries: WebhookEntry[] };
      setEntries(data.entries ?? []);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchEntries();
  }, [fetchEntries]);

  // --- Live countdown timer ------------------------------------------------

  useEffect(() => {
    const hasOverlap = entries.some(
      (e) =>
        e.status === "overlap_active" &&
        e.overlap_window_remaining_s != null &&
        e.overlap_window_remaining_s > 0,
    );

    if (hasOverlap && !tickRef.current) {
      tickRef.current = setInterval(() => {
        setTick((t) => t + 1);
      }, 1000);
    } else if (!hasOverlap && tickRef.current) {
      clearInterval(tickRef.current);
      tickRef.current = null;
    }

    return () => {
      if (tickRef.current) {
        clearInterval(tickRef.current);
        tickRef.current = null;
      }
    };
  }, [entries]);

  // Compute remaining seconds accounting for tick
  const getRemaining = (entry: WebhookEntry): number => {
    if (
      entry.overlap_window_remaining_s == null ||
      entry.overlap_window_remaining_s <= 0
    ) {
      return 0;
    }
    const remaining = entry.overlap_window_remaining_s - tick;
    return remaining > 0 ? remaining : 0;
  };

  // --- Action handlers -----------------------------------------------------

  const handleConfirm = useCallback(async () => {
    if (!confirmAction) return;

    setActionLoading(true);
    setError(null);

    try {
      const { type, deptId, provider } = confirmAction;
      const endpoint =
        type === "rotate"
          ? `/admin/security/webhooks/${encodeURIComponent(deptId)}/${encodeURIComponent(provider)}/rotate`
          : `/admin/security/webhooks/${encodeURIComponent(deptId)}/${encodeURIComponent(provider)}/finalize`;

      const res = await apiFetch(endpoint, { method: "POST" });

      if (!res.ok) {
        const body = await res.text();
        setError(
          `İşlem başarısız (HTTP ${res.status}): ${body.slice(0, 200)}`,
        );
        setConfirmAction(null);
        return;
      }

      if (type === "rotate") {
        const data = (await res.json()) as RotateResponse;
        setRotateResult({
          newSecret: data.new_secret,
          deptId,
          provider,
        });
      }

      setConfirmAction(null);
      // Reset tick counter and refresh
      setTick(0);
      await fetchEntries();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setActionLoading(false);
    }
  }, [confirmAction, fetchEntries]);

  // --- Render --------------------------------------------------------------

  return (
    <section style={cardStyle} aria-labelledby="webhook-secrets-title">
      <h2
        id="webhook-secrets-title"
        style={{ margin: "0 0 0.75rem 0", fontSize: "1.1rem" }}
      >
        Webhook Secrets
      </h2>

      {error && (
        <p role="alert" style={{ color: "crimson", fontSize: "0.9rem" }}>
          Hata: {error}
        </p>
      )}

      {loading && entries.length === 0 ? (
        <p style={{ color: "#6b7280" }}>Yükleniyor…</p>
      ) : entries.length === 0 ? (
        <p style={{ color: "#6b7280" }}>
          Kayıtlı webhook secret bulunamadı.
        </p>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={tableStyle}>
            <thead>
              <tr>
                <th style={thStyle}>Departman</th>
                <th style={thStyle}>Provider</th>
                <th style={thStyle}>Son Rotation</th>
                <th style={thStyle}>Overlap Kalan</th>
                <th style={thStyle}>Durum</th>
                <th style={thStyle}>İşlemler</th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => {
                const remaining = getRemaining(entry);
                return (
                  <tr key={`${entry.dept_id}-${entry.provider}`}>
                    <td style={tdStyle}>
                      <strong>{entry.dept_id}</strong>
                    </td>
                    <td style={tdStyle}>{providerLabel(entry.provider)}</td>
                    <td style={tdStyle}>
                      {formatDate(entry.last_rotated_at)}
                    </td>
                    <td style={tdStyle}>
                      {entry.status === "overlap_active" && remaining > 0 ? (
                        <span
                          style={{
                            fontFamily: "monospace",
                            color: "#854d0e",
                            fontWeight: 600,
                          }}
                        >
                           {formatCountdown(remaining)}
                        </span>
                      ) : (
                        "-"
                      )}
                    </td>
                    <td style={tdStyle}>
                      <span style={statusBadge(entry.status)}>
                        {entry.status === "ok"
                          ? "OK"
                          : entry.status === "overlap_active"
                            ? "Overlap Aktif"
                            : "Hiç Döndürülmedi"}
                      </span>
                    </td>
                    <td style={tdStyle}>
                      <button
                        type="button"
                        style={btnPrimaryStyle}
                        onClick={() =>
                          setConfirmAction({
                            type: "rotate",
                            deptId: entry.dept_id,
                            provider: entry.provider,
                          })
                        }
                      >
                        Döndür
                      </button>
                      <button
                        type="button"
                        style={{
                          ...btnDangerStyle,
                          opacity:
                            entry.status === "overlap_active" ? 1 : 0.4,
                          cursor:
                            entry.status === "overlap_active"
                              ? "pointer"
                              : "not-allowed",
                        }}
                        disabled={entry.status !== "overlap_active"}
                        onClick={() =>
                          setConfirmAction({
                            type: "finalize",
                            deptId: entry.dept_id,
                            provider: entry.provider,
                          })
                        }
                      >
                        Sonlandır
                      </button>
                    </td>
                  </tr>
                );
              })}
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
            aria-labelledby="webhook-confirm-title"
            aria-describedby="webhook-confirm-desc"
          >
            <h3
              id="webhook-confirm-title"
              style={{ margin: "0 0 0.75rem 0" }}
            >
              İşlemi Onayla
            </h3>
            <p id="webhook-confirm-desc" style={{ margin: "0 0 1.25rem 0" }}>
              {confirmAction.type === "rotate"
                ? "Webhook secret'ı döndürmek istediğinizden emin misiniz? Mevcut secret yedek slot'a taşınacak ve 1 saatlik overlap penceresi başlayacak."
                : "Overlap penceresini sonlandırmak istediğinizden emin misiniz? Yedek (previous) secret silinecek - Atlassian/Bitbucket tarafında yeni secret'ı yapıştırdığınızdan emin olun."}
            </p>
            <p style={{ fontSize: "0.85rem", color: "#6b7280" }}>
              Departman: <strong>{confirmAction.deptId}</strong> /{" "}
              {providerLabel(confirmAction.provider)}
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
      {/* Rotate Result Modal - new secret + 3-step guide                    */}
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
            aria-labelledby="webhook-result-title"
          >
            <h3
              id="webhook-result-title"
              style={{ margin: "0 0 0.75rem 0" }}
            >
               Yeni Webhook Secret
            </h3>
            <p style={{ fontSize: "0.9rem", marginBottom: "0.75rem" }}>
              <strong>{rotateResult.deptId}</strong> /{" "}
              {providerLabel(rotateResult.provider)} için yeni secret
              üretildi. Aşağıdaki 3 adımı takip edin:
            </p>

            {/* 3-step guide */}
            <ol
              style={{
                margin: "0 0 1rem 0",
                paddingLeft: "1.25rem",
                fontSize: "0.9rem",
                lineHeight: 1.7,
              }}
            >
              <li>
                Aşağıdaki yeni secret&apos;ı <strong>kopyalayın</strong>.
              </li>
              <li>
                {providerLabel(rotateResult.provider)} webhook ayarları
                UI&apos;sına gidin ve secret alanına{" "}
                <strong>yapıştırın</strong>.
              </li>
              <li>
                Geri dönüp <strong>&quot;Sonlandır&quot;</strong> butonuna
                basın (veya 1 saat sonra otomatik sonlanır).
              </li>
            </ol>

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
              ℹ Overlap penceresi boyunca hem eski hem yeni secret kabul
              edilir - geçiş sırasında kesinti yaşanmaz.
            </div>

            <div style={codeBlockStyle}>{rotateResult.newSecret}</div>

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
                  void navigator.clipboard.writeText(rotateResult.newSecret);
                }}
              >
                 Kopyala
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
