"use client";

/**
 * TestRunnerPanel - Real-time SSE test runner with terminal-like output.
 * * Frontend component for
 * ``POST /admin/services/{service_name}/test?stream=true``.
 * * Features:
 * - SSE streaming via fetch + ReadableStream (POST not supported by EventSource)
 * - Real-time line-by-line rendering in a scrollable terminal panel
 * - Cancel button with AbortController to abort the SSE stream
 * - PASSED (green) / FAILED (red) badge on completion
 * - "Connection lost" warning with reconnect option on unexpected disconnect
 * */

import { useCallback, useEffect, useRef, useState } from "react";
import { getAdminApiBaseUrl, getAdminAuthHeaders } from "@/lib/api-client";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type TestRunnerPanelProps = {
  serviceName: string;
  onClose: () => void;
};

type StreamStatus =
  | "idle"
  | "connecting"
  | "streaming"
  | "passed"
  | "failed"
  | "cancelled"
  | "disconnected";

// ---------------------------------------------------------------------------
// SSE line parser
// ---------------------------------------------------------------------------

/**
 * Parse SSE frames from a text chunk. Handles partial lines across chunks.
 * Returns parsed events and any remaining incomplete data.
 */
function parseSSEChunk(
  buffer: string,
  chunk: string,
): { events: SSEEvent[]; remaining: string } {
  const combined = buffer + chunk;
  const events: SSEEvent[] = [];
  const blocks = combined.split("\n\n");

  // Last element may be incomplete (no trailing \n\n)
  const remaining = blocks.pop() ?? "";

  for (const block of blocks) {
    if (!block.trim()) continue;

    let eventType = "message";
    let data = "";

    for (const line of block.split("\n")) {
      if (line.startsWith("event: ")) {
        eventType = line.slice(7).trim();
      } else if (line.startsWith("data: ")) {
        data = line.slice(6);
      } else if (line.startsWith("data:")) {
        data = line.slice(5);
      }
    }

    events.push({ type: eventType, data });
  }

  return { events, remaining };
}

type SSEEvent = {
  type: string;
  data: string;
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function TestRunnerPanel({
  serviceName,
  onClose,
}: TestRunnerPanelProps) {
  const [status, setStatus] = useState<StreamStatus>("idle");
  const [lines, setLines] = useState<string[]>([]);
  const [exitCode, setExitCode] = useState<number | null>(null);

  const abortControllerRef = useRef<AbortController | null>(null);
  const terminalRef = useRef<HTMLDivElement>(null);
  const autoScrollRef = useRef(true);

  // Auto-scroll terminal to bottom when new lines arrive
  useEffect(() => {
    if (autoScrollRef.current && terminalRef.current) {
      terminalRef.current.scrollTop = terminalRef.current.scrollHeight;
    }
  }, [lines]);

  // Handle user scroll - disable auto-scroll if user scrolls up
  const handleTerminalScroll = useCallback(() => {
    if (!terminalRef.current) return;
    const { scrollTop, scrollHeight, clientHeight } = terminalRef.current;
    // If user is within 50px of bottom, keep auto-scrolling
    autoScrollRef.current = scrollHeight - scrollTop - clientHeight < 50;
  }, []);

  // Start the SSE stream
  const startStream = useCallback(async () => {
    // Reset state
    setLines([]);
    setExitCode(null);
    setStatus("connecting");
    autoScrollRef.current = true;

    const controller = new AbortController();
    abortControllerRef.current = controller;

    try {
      const baseUrl = getAdminApiBaseUrl();
      const url = `${baseUrl}/admin/services/${encodeURIComponent(serviceName)}/test?stream=true`;

      const response = await fetch(url, {
        method: "POST",
        headers: getAdminAuthHeaders({ "Content-Type": "application/json" }),
        signal: controller.signal,
      });

      if (!response.ok) {
        const text = await response.text().catch(() => "");
        setLines((prev) => [
          ...prev,
          `Error: HTTP ${response.status} - ${text || "Request failed"}`,
        ]);
        setStatus("failed");
        return;
      }

      if (!response.body) {
        setLines((prev) => [...prev, "Error: No response body (streaming not supported)"]);
        setStatus("failed");
        return;
      }

      setStatus("streaming");

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let sseBuffer = "";
      let receivedDone = false;

      while (true) {
        const { done, value } = await reader.read();

        if (done) {
          // Stream ended - process any remaining buffer
          if (sseBuffer.trim()) {
            const { events } = parseSSEChunk(sseBuffer, "\n\n");
            for (const event of events) {
              if (event.type === "done") {
                try {
                  const payload = JSON.parse(event.data);
                  setExitCode(payload.exit_code);
                  setStatus(payload.exit_code === 0 ? "passed" : "failed");
                  receivedDone = true;
                } catch {
                  setStatus("failed");
                  receivedDone = true;
                }
              } else if (event.type === "error") {
                setLines((prev) => [...prev, `[ERROR] ${event.data}`]);
                setStatus("failed");
                receivedDone = true;
              } else {
                setLines((prev) => [...prev, event.data]);
              }
            }
          }

          // If we haven't received a done event, treat as unexpected disconnect
          if (!receivedDone) {
            setStatus("disconnected");
          }
          break;
        }

        const chunk = decoder.decode(value, { stream: true });
        const { events, remaining } = parseSSEChunk(sseBuffer, chunk);
        sseBuffer = remaining;

        for (const event of events) {
          if (event.type === "done") {
            try {
              const payload = JSON.parse(event.data);
              setExitCode(payload.exit_code);
              setStatus(payload.exit_code === 0 ? "passed" : "failed");
              receivedDone = true;
            } catch {
              setStatus("failed");
              receivedDone = true;
            }
          } else if (event.type === "error") {
            setLines((prev) => [...prev, `[ERROR] ${event.data}`]);
            setStatus("failed");
            receivedDone = true;
          } else {
            // Regular data event - append line
            setLines((prev) => [...prev, event.data]);
          }
        }
      }
    } catch (err: unknown) {
      if (err instanceof DOMException && err.name === "AbortError") {
        // User cancelled - this is expected
        setStatus("cancelled");
      } else {
        // Unexpected disconnect
        setLines((prev) => [
          ...prev,
          `[CONNECTION ERROR] ${err instanceof Error ? err.message : String(err)}`,
        ]);
        setStatus("disconnected");
      }
    } finally {
      abortControllerRef.current = null;
    }
  }, [serviceName]);

  // Cancel the stream
  const handleCancel = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
  }, []);

  // Reconnect after disconnect
  const handleReconnect = useCallback(() => {
    void startStream();
  }, [startStream]);

  // Start stream on mount
  useEffect(() => {
    void startStream();
    return () => {
      // Cleanup on unmount
      if (abortControllerRef.current) {
        abortControllerRef.current.abort();
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Close on Escape key
  useEffect(() => {
    function onKey(ev: KeyboardEvent) {
      if (ev.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const isRunning = status === "connecting" || status === "streaming";

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`Run tests for ${serviceName}`}
      style={overlayStyle}
      onMouseDown={(ev) => {
        if (ev.target === ev.currentTarget) onClose();
      }}
    >
      <div style={panelStyle}>
        {/* Header */}
        <div style={headerStyle}>
          <h2 style={{ margin: 0, fontSize: "1.1rem" }}>
            Run Tests - {serviceName}
          </h2>
          <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
            {/* Status badge */}
            {status === "passed" && (
              <span style={passedBadgeStyle} role="status" aria-label="Tests passed">
                PASSED
              </span>
            )}
            {status === "failed" && (
              <span style={failedBadgeStyle} role="status" aria-label="Tests failed">
                FAILED
              </span>
            )}
            {status === "cancelled" && (
              <span style={cancelledBadgeStyle} role="status" aria-label="Tests cancelled">
                CANCELLED
              </span>
            )}
            {isRunning && (
              <span style={runningIndicatorStyle} role="status" aria-label="Tests running">
                ● Running
              </span>
            )}
          </div>
        </div>

        {/* Connection lost warning */}
        {status === "disconnected" && (
          <div style={disconnectWarningStyle} role="alert">
            <span> Connection lost</span>
            <button
              type="button"
              onClick={handleReconnect}
              style={reconnectButtonStyle}
            >
              Reconnect
            </button>
          </div>
        )}

        {/* Terminal output panel */}
        <div
          ref={terminalRef}
          onScroll={handleTerminalScroll}
          style={terminalStyle}
          aria-label="Test output"
          role="log"
          aria-live="polite"
        >
          {lines.length === 0 && isRunning && (
            <span style={{ color: "#9ca3af" }}>Waiting for output…</span>
          )}
          {lines.map((line, idx) => (
            <div key={idx} style={lineStyle}>
              {line}
            </div>
          ))}
          {exitCode !== null && (
            <div
              style={{
                ...lineStyle,
                color: exitCode === 0 ? "#16a34a" : "#dc2626",
                fontWeight: 600,
                marginTop: "0.5rem",
                borderTop: "1px solid #374151",
                paddingTop: "0.5rem",
              }}
            >
              Process exited with code {exitCode}
            </div>
          )}
        </div>

        {/* Action buttons */}
        <div style={footerStyle}>
          {/* Cancel button */}
          {isRunning && (
            <button
              type="button"
              onClick={handleCancel}
              style={cancelButtonStyle}
            >
              Cancel
            </button>
          )}

          {/* Re-run button when finished */}
          {!isRunning && status !== "idle" && (
            <button
              type="button"
              onClick={handleReconnect}
              style={rerunButtonStyle}
            >
              Re-run Tests
            </button>
          )}

          <button type="button" onClick={onClose} style={closeButtonStyle}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Styles
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

const panelStyle: React.CSSProperties = {
  background: "#ffffff",
  padding: "1.5rem",
  borderRadius: "0.5rem",
  width: "min(800px, 92vw)",
  maxHeight: "85vh",
  display: "flex",
  flexDirection: "column",
  boxShadow: "0 10px 40px rgba(0,0,0,0.3)",
};

const headerStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  marginBottom: "0.75rem",
};

const terminalStyle: React.CSSProperties = {
  flex: 1,
  minHeight: "300px",
  maxHeight: "55vh",
  overflowY: "auto",
  background: "#1e1e2e",
  color: "#cdd6f4",
  fontFamily:
    "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', monospace",
  fontSize: "0.82rem",
  lineHeight: 1.5,
  padding: "0.75rem 1rem",
  borderRadius: "0.375rem",
  border: "1px solid #313244",
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
};

const lineStyle: React.CSSProperties = {
  minHeight: "1.3em",
};

const footerStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "flex-end",
  gap: "0.5rem",
  marginTop: "0.75rem",
};

const passedBadgeStyle: React.CSSProperties = {
  display: "inline-block",
  padding: "0.2rem 0.6rem",
  borderRadius: "0.25rem",
  fontSize: "0.8rem",
  fontWeight: 700,
  background: "#dcfce7",
  color: "#166534",
  border: "1px solid #86efac",
};

const failedBadgeStyle: React.CSSProperties = {
  display: "inline-block",
  padding: "0.2rem 0.6rem",
  borderRadius: "0.25rem",
  fontSize: "0.8rem",
  fontWeight: 700,
  background: "#fee2e2",
  color: "#991b1b",
  border: "1px solid #fca5a5",
};

const cancelledBadgeStyle: React.CSSProperties = {
  display: "inline-block",
  padding: "0.2rem 0.6rem",
  borderRadius: "0.25rem",
  fontSize: "0.8rem",
  fontWeight: 700,
  background: "#fef3c7",
  color: "#92400e",
  border: "1px solid #fcd34d",
};

const runningIndicatorStyle: React.CSSProperties = {
  display: "inline-block",
  padding: "0.2rem 0.6rem",
  fontSize: "0.8rem",
  fontWeight: 600,
  color: "#2563eb",
  animation: "pulse 1.5s ease-in-out infinite",
};

const disconnectWarningStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  padding: "0.5rem 0.75rem",
  marginBottom: "0.5rem",
  background: "#fef3c7",
  color: "#92400e",
  borderRadius: "0.25rem",
  border: "1px solid #fcd34d",
  fontSize: "0.9rem",
  fontWeight: 500,
};

const reconnectButtonStyle: React.CSSProperties = {
  padding: "0.25rem 0.6rem",
  fontSize: "0.8rem",
  fontWeight: 600,
  border: "1px solid #d97706",
  borderRadius: "0.25rem",
  background: "#ffffff",
  color: "#d97706",
  cursor: "pointer",
};

const cancelButtonStyle: React.CSSProperties = {
  padding: "0.4rem 0.9rem",
  fontSize: "0.9rem",
  fontWeight: 600,
  border: "1px solid #dc2626",
  borderRadius: "0.25rem",
  background: "#ffffff",
  color: "#dc2626",
  cursor: "pointer",
};

const rerunButtonStyle: React.CSSProperties = {
  padding: "0.4rem 0.9rem",
  fontSize: "0.9rem",
  fontWeight: 600,
  border: "1px solid #2563eb",
  borderRadius: "0.25rem",
  background: "#ffffff",
  color: "#2563eb",
  cursor: "pointer",
};

const closeButtonStyle: React.CSSProperties = {
  padding: "0.4rem 0.9rem",
  fontSize: "0.9rem",
  border: "1px solid #d1d5db",
  borderRadius: "0.25rem",
  background: "#ffffff",
  cursor: "pointer",
};
