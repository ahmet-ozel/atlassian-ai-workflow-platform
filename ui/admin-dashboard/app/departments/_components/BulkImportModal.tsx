"use client";

/**
 * BulkImportModal — Modal for bulk-importing departments from a JSON file.
 *
 * Features:
 * - File upload (JSON format matching `departments.schema.json`)
 * - "Önce Önizle (dry-run)" / "Şimdi Uygula" radio selection
 * - Result table showing per-department status (✅/❌/⚠️ + reason)
 *
 * Requirements: 11.5
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { apiFetch } from "@/lib/api-client";
import { getAdminApiBaseUrl } from "@/lib/config";

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export type BulkImportModalProps = {
  /** Called when the modal is dismissed. */
  onClose: () => void;
  /** Called after a successful import to refresh the parent list. */
  onImported: () => void;
};

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type ImportMode = "dry_run" | "apply";

type DeptResult = {
  dept_id: string;
  status: "success" | "failed" | "skipped";
  reason?: string;
};

type BulkImportResponse = {
  validated: string[];
  imported: string[];
  failed: { dept_id: string; error: string }[];
  probe_results: {
    dept_id: string;
    service: string;
    status: "ok" | "failed";
    error?: string;
  }[];
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
  width: "min(700px, 94vw)",
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

const tableStyle: React.CSSProperties = {
  width: "100%",
  borderCollapse: "collapse",
  marginTop: "1rem",
  fontSize: "0.9rem",
};

const thStyle: React.CSSProperties = {
  textAlign: "left",
  borderBottom: "2px solid #e5e7eb",
  padding: "0.5rem 0.75rem",
  fontWeight: 600,
};

const tdStyle: React.CSSProperties = {
  borderBottom: "1px solid #f3f4f6",
  padding: "0.5rem 0.75rem",
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function getStatusIcon(status: DeptResult["status"]): string {
  switch (status) {
    case "success":
      return "✅";
    case "failed":
      return "❌";
    case "skipped":
      return "⚠️";
    default:
      return "—";
  }
}

function buildResults(response: BulkImportResponse): DeptResult[] {
  const results: DeptResult[] = [];

  // Imported departments
  for (const deptId of response.imported) {
    results.push({ dept_id: deptId, status: "success", reason: "Başarıyla import edildi" });
  }

  // Validated (dry-run) departments
  for (const deptId of response.validated) {
    if (!response.imported.includes(deptId) && !response.failed.some((f) => f.dept_id === deptId)) {
      results.push({ dept_id: deptId, status: "success", reason: "Validasyon başarılı (dry-run)" });
    }
  }

  // Failed departments
  for (const fail of response.failed) {
    results.push({ dept_id: fail.dept_id, status: "failed", reason: fail.error });
  }

  // Check probe results for warnings
  for (const probe of response.probe_results) {
    if (probe.status === "failed") {
      const existing = results.find((r) => r.dept_id === probe.dept_id);
      if (existing && existing.status === "success") {
        existing.status = "skipped";
        existing.reason = `Probe başarısız (${probe.service}): ${probe.error ?? "bilinmeyen hata"}`;
      }
    }
  }

  return results;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function BulkImportModal({
  onClose,
  onImported,
}: BulkImportModalProps): JSX.Element {
  const [file, setFile] = useState<File | null>(null);
  const [mode, setMode] = useState<ImportMode>("dry_run");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [results, setResults] = useState<DeptResult[] | null>(null);
  const [httpStatus, setHttpStatus] = useState<number | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Escape key closes modal
  useEffect(() => {
    function onKey(ev: KeyboardEvent): void {
      if (ev.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // Handle file selection
  const handleFileChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const selected = e.target.files?.[0] ?? null;
      if (selected && !selected.name.endsWith(".json")) {
        setError("Yalnızca .json dosyaları kabul edilir.");
        setFile(null);
        return;
      }
      setFile(selected);
      setError(null);
      setResults(null);
    },
    [],
  );

  // Submit import
  const handleSubmit = useCallback(async () => {
    if (!file) {
      setError("Lütfen bir JSON dosyası seçin.");
      return;
    }

    setSubmitting(true);
    setError(null);
    setResults(null);
    setHttpStatus(null);

    try {
      const formData = new FormData();
      formData.append("file", file);
      formData.append("dry_run", mode === "dry_run" ? "true" : "false");

      const baseUrl = getAdminApiBaseUrl();
      const url = `${baseUrl}/admin/departments/bulk-import`;

      const res = await fetch(url, {
        method: "POST",
        body: formData,
        // No Content-Type header — browser sets multipart boundary automatically
      });

      setHttpStatus(res.status);

      if (res.status === 422) {
        const body = await res.json().catch(() => ({ detail: "Schema validasyon hatası" }));
        setError(
          `Schema validasyon hatası: ${body.detail ?? body.message ?? JSON.stringify(body)}`,
        );
        return;
      }

      if (!res.ok && res.status !== 207) {
        const body = await res.text();
        setError(`HTTP ${res.status}: ${body.slice(0, 300)}`);
        return;
      }

      const data = (await res.json()) as BulkImportResponse;
      const builtResults = buildResults(data);
      setResults(builtResults);

      // If this was an actual apply (not dry-run) and there were successes, notify parent
      if (mode === "apply" && data.imported.length > 0) {
        onImported();
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSubmitting(false);
    }
  }, [file, mode, onImported]);

  // Reset to try again
  const handleReset = useCallback(() => {
    setFile(null);
    setResults(null);
    setError(null);
    setHttpStatus(null);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  }, []);

  const titleId = "bulk-import-modal-title";

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
            📦 Toplu Departman İçe Aktarma
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

        {/* Info banner */}
        <div
          style={{
            background: "#eff6ff",
            border: "1px solid #bfdbfe",
            color: "#1e40af",
            padding: "0.75rem 1rem",
            borderRadius: 6,
            marginBottom: "1rem",
            fontSize: "0.85rem",
          }}
        >
          <strong>Bilgi:</strong> <code>departments.schema.json</code> formatına
          uygun bir JSON dosyası yükleyin. Her departman için connectivity probe
          çalıştırılır; başarılı olanlar import edilir, başarısız olanlar rapor
          edilir.
        </div>

        {/* Error display */}
        {error && (
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

        {/* File upload */}
        <div style={{ marginBottom: "1rem" }}>
          <label htmlFor="bulk-import-file" style={labelStyle}>
            JSON Dosyası
          </label>
          <input
            ref={fileInputRef}
            id="bulk-import-file"
            type="file"
            accept=".json,application/json"
            onChange={handleFileChange}
            style={inputStyle}
            disabled={submitting}
          />
          {file && (
            <p style={{ margin: "0.3rem 0 0", fontSize: "0.85rem", color: "#6b7280" }}>
              Seçilen: <strong>{file.name}</strong> ({(file.size / 1024).toFixed(1)} KB)
            </p>
          )}
        </div>

        {/* Mode selection */}
        <fieldset
          style={{
            border: "1px solid #e5e7eb",
            borderRadius: 6,
            padding: "0.75rem 1rem",
            marginBottom: "1rem",
          }}
        >
          <legend style={{ fontWeight: 500, fontSize: "0.9rem", padding: "0 0.3rem" }}>
            İşlem Modu
          </legend>
          <div style={{ display: "flex", gap: "1.5rem", marginTop: "0.5rem" }}>
            <label
              style={{
                display: "flex",
                alignItems: "center",
                gap: "0.4rem",
                cursor: "pointer",
                fontSize: "0.9rem",
              }}
            >
              <input
                type="radio"
                name="import-mode"
                value="dry_run"
                checked={mode === "dry_run"}
                onChange={() => setMode("dry_run")}
                disabled={submitting}
              />
              <span>
                🔍 Önce Önizle <em>(dry-run)</em>
              </span>
            </label>
            <label
              style={{
                display: "flex",
                alignItems: "center",
                gap: "0.4rem",
                cursor: "pointer",
                fontSize: "0.9rem",
              }}
            >
              <input
                type="radio"
                name="import-mode"
                value="apply"
                checked={mode === "apply"}
                onChange={() => setMode("apply")}
                disabled={submitting}
              />
              <span>
                🚀 Şimdi Uygula
              </span>
            </label>
          </div>
        </fieldset>

        {/* Action buttons */}
        <div
          style={{
            display: "flex",
            justifyContent: "flex-end",
            gap: "0.5rem",
            marginBottom: results ? "1rem" : 0,
          }}
        >
          {results && (
            <button
              type="button"
              onClick={handleReset}
              style={{
                padding: "0.5rem 1rem",
                border: "1px solid #d1d5db",
                background: "#fff",
                borderRadius: 4,
                cursor: "pointer",
                fontSize: "0.9rem",
              }}
            >
              Yeni Dosya Yükle
            </button>
          )}
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
            Kapat
          </button>
          <button
            type="button"
            onClick={handleSubmit}
            disabled={submitting || !file}
            style={{
              padding: "0.5rem 1rem",
              background: mode === "apply" ? "#dc2626" : "#2563eb",
              color: "#fff",
              border: "none",
              borderRadius: 4,
              cursor: submitting || !file ? "not-allowed" : "pointer",
              opacity: submitting || !file ? 0.6 : 1,
              fontWeight: 500,
            }}
          >
            {submitting
              ? "İşleniyor…"
              : mode === "dry_run"
                ? "🔍 Önizle"
                : "🚀 Uygula"}
          </button>
        </div>

        {/* Results table */}
        {results && results.length > 0 && (
          <div>
            <h3 style={{ fontSize: "1rem", margin: "0.5rem 0" }}>
              Sonuçlar
              {httpStatus === 207 && (
                <span
                  style={{
                    marginLeft: "0.5rem",
                    fontSize: "0.8rem",
                    color: "#d97706",
                    fontWeight: 400,
                  }}
                >
                  (kısmi başarı)
                </span>
              )}
            </h3>

            {/* Summary */}
            <div
              style={{
                display: "flex",
                gap: "1rem",
                marginBottom: "0.75rem",
                fontSize: "0.85rem",
              }}
            >
              <span style={{ color: "#16a34a" }}>
                ✅ Başarılı: {results.filter((r) => r.status === "success").length}
              </span>
              <span style={{ color: "#dc2626" }}>
                ❌ Başarısız: {results.filter((r) => r.status === "failed").length}
              </span>
              <span style={{ color: "#d97706" }}>
                ⚠️ Uyarı: {results.filter((r) => r.status === "skipped").length}
              </span>
            </div>

            <table style={tableStyle}>
              <thead>
                <tr>
                  <th style={thStyle}>Durum</th>
                  <th style={thStyle}>Departman ID</th>
                  <th style={thStyle}>Neden</th>
                </tr>
              </thead>
              <tbody>
                {results.map((r) => (
                  <tr key={r.dept_id}>
                    <td style={tdStyle}>{getStatusIcon(r.status)}</td>
                    <td style={tdStyle}>
                      <code>{r.dept_id}</code>
                    </td>
                    <td
                      style={{
                        ...tdStyle,
                        color:
                          r.status === "failed"
                            ? "#dc2626"
                            : r.status === "skipped"
                              ? "#d97706"
                              : "#374151",
                      }}
                    >
                      {r.reason ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {results && results.length === 0 && (
          <p style={{ color: "#6b7280", fontSize: "0.9rem", marginTop: "0.5rem" }}>
            Dosyada işlenecek departman bulunamadı.
          </p>
        )}
      </div>
    </div>
  );
}
