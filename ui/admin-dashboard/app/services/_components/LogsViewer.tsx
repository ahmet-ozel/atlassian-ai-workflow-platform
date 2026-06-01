"use client";

import { useCallback, useEffect, useState, type CSSProperties } from "react";
import { apiFetch } from "@/lib/api-client";

export type LogsViewerProps = {
  serviceName: string;
  onClose: () => void;
};

type LogsResponse = {
  lines: string[];
};

export default function LogsViewer({ serviceName, onClose }: LogsViewerProps) {
  const [lines, setLines] = useState<string[]>([]);
  const [status, setStatus] = useState<"loading" | "ready" | "error">("loading");
  const [error, setError] = useState<string | null>(null);

  const loadLogs = useCallback(async () => {
    setStatus("loading");
    setError(null);

    try {
      const response = await apiFetch(
        `/admin/services/${encodeURIComponent(serviceName)}/logs?tail=200`,
      );

      if (!(response instanceof Response) || !response.ok) {
        const text =
          response instanceof Response ? await response.text().catch(() => "") : "";
        throw new Error(
          response instanceof Response
            ? `HTTP ${response.status}: ${text || "logs request failed"}`
            : "logs request failed",
        );
      }

      const payload = (await response.json()) as LogsResponse;
      setLines(Array.isArray(payload.lines) ? payload.lines : []);
      setStatus("ready");
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
      setStatus("error");
    }
  }, [serviceName]);

  useEffect(() => {
    void loadLogs();
  }, [loadLogs]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`Logs for ${serviceName}`}
      style={overlayStyle}
      onMouseDown={(ev) => {
        if (ev.target === ev.currentTarget) onClose();
      }}
    >
      <div style={panelStyle}>
        <div style={headerStyle}>
          <h2 style={{ margin: 0, fontSize: "1.1rem" }}>Logs - {serviceName}</h2>
          <button type="button" onClick={loadLogs} style={refreshButtonStyle}>
            Refresh
          </button>
        </div>

        {status === "loading" && (
          <div style={messageStyle} role="status">
            Loading logs...
          </div>
        )}

        {status === "error" && (
          <div style={errorStyle} role="alert">
            {error}
          </div>
        )}

        {status === "ready" && (
          <div style={terminalStyle} role="log" aria-live="polite">
            {lines.length === 0 ? (
              <span style={{ color: "#9ca3af" }}>No log lines returned.</span>
            ) : (
              lines.map((line, index) => (
                <div key={`${index}-${line.slice(0, 16)}`} style={lineStyle}>
                  {line}
                </div>
              ))
            )}
          </div>
        )}

        <div style={footerStyle}>
          <button type="button" onClick={onClose} style={closeButtonStyle}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

const overlayStyle: CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "rgba(0,0,0,0.4)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  zIndex: 50,
};

const panelStyle: CSSProperties = {
  background: "#ffffff",
  padding: "1.5rem",
  borderRadius: "0.5rem",
  width: "min(900px, 92vw)",
  maxHeight: "85vh",
  display: "flex",
  flexDirection: "column",
  boxShadow: "0 10px 40px rgba(0,0,0,0.25)",
};

const headerStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: "1rem",
  marginBottom: "0.75rem",
};

const messageStyle: CSSProperties = {
  padding: "0.75rem",
  border: "1px solid #d1d5db",
  borderRadius: "0.375rem",
  color: "#374151",
};

const errorStyle: CSSProperties = {
  padding: "0.75rem",
  border: "1px solid #fca5a5",
  borderRadius: "0.375rem",
  background: "#fee2e2",
  color: "#991b1b",
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
};

const terminalStyle: CSSProperties = {
  minHeight: "320px",
  maxHeight: "62vh",
  overflowY: "auto",
  background: "#111827",
  color: "#e5e7eb",
  fontFamily:
    "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', monospace",
  fontSize: "0.82rem",
  lineHeight: 1.5,
  padding: "0.75rem 1rem",
  borderRadius: "0.375rem",
  border: "1px solid #374151",
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
};

const lineStyle: CSSProperties = {
  minHeight: "1.3em",
};

const footerStyle: CSSProperties = {
  display: "flex",
  justifyContent: "flex-end",
  marginTop: "0.75rem",
};

const refreshButtonStyle: CSSProperties = {
  padding: "0.4rem 0.9rem",
  border: "1px solid #2563eb",
  borderRadius: "0.25rem",
  background: "#ffffff",
  color: "#2563eb",
  cursor: "pointer",
  fontWeight: 600,
};

const closeButtonStyle: CSSProperties = {
  padding: "0.4rem 0.9rem",
  border: "1px solid #d1d5db",
  borderRadius: "0.25rem",
  background: "#ffffff",
  cursor: "pointer",
};
