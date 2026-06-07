"use client";

/**
 * CancelButton - RBAC-aware cancel button for a running workflow.
 * * Enabled only when:
 * - workflow.state == "running"
 * - actor role is "admin" (always), or "dept_admin" with the workflow's
 * dept_id in their viewer_dept_ids list.
 * * The RBAC check is enforced server-side by `POST /admin/workflows/{id}/cancel`.
 * The client-side check here is a UX hint only - it disables the button when
 * the workflow is not running, matching the spec requirement (R8.3).
 */

import { useState } from "react";
import { apiFetch } from "@/lib/api-client";

interface CancelButtonProps {
  workflowId: string;
  status?: string;
  deptId: string | null;
  onCancelled?: () => void;
}

export default function CancelButton({
  workflowId,
  status,
  onCancelled,
}: CancelButtonProps): JSX.Element {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);

  const isRunning = status === "running";

  const handleCancel = async () => {
    if (!confirmed) {
      setConfirmed(true);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      await apiFetch(`/admin/workflows/${encodeURIComponent(workflowId)}/cancel`, {
        method: "POST",
      });
      onCancelled?.();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
      setConfirmed(false);
    }
  };

  return (
    <div style={{ margin: "0.75rem 0", display: "flex", alignItems: "center", gap: "0.75rem" }}>
      <button
        onClick={handleCancel}
        disabled={!isRunning || loading}
        title={
          !isRunning
            ? "Workflow is not running"
            : confirmed
            ? "Click again to confirm cancellation"
            : "Cancel this workflow"
        }
        style={{
          padding: "6px 16px",
          borderRadius: "4px",
          border: "1px solid #dc2626",
          background: isRunning ? "#fee2e2" : "#f3f4f6",
          color: isRunning ? "#dc2626" : "#9ca3af",
          cursor: isRunning ? "pointer" : "not-allowed",
          fontWeight: 500,
          fontSize: "0.875rem",
        }}
      >
        {loading ? "Cancelling…" : confirmed ? "Confirm Cancel?" : "Cancel Workflow"}
      </button>
      {confirmed && !loading && (
        <button
          onClick={() => setConfirmed(false)}
          style={{
            padding: "6px 12px",
            borderRadius: "4px",
            border: "1px solid #d1d5db",
            background: "#fff",
            cursor: "pointer",
            fontSize: "0.875rem",
          }}
        >
          Abort
        </button>
      )}
      {error && (
        <span style={{ color: "#dc2626", fontSize: "0.875rem" }}>
          Error: {error}
        </span>
      )}
    </div>
  );
}
