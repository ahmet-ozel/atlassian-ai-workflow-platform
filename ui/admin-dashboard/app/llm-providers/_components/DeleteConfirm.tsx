"use client";

/**
 * Delete-confirmation dialog for a provider row (R14.8, R14.9).
 *
 * On confirm calls `DELETE /admin/llm-providers/{id}`. When the
 * backend responds with HTTP 409 `provider_in_use`, the dialog stays
 * open and renders an inline toast listing every `dept_id` so the
 * operator can unpin the override(s) before retrying. The parent
 * page reuses the same `onClose` callback to dismiss the dialog
 * and refreshes the row list via `onDeleted` on a clean 204.
 */

import { useState } from "react";

import { ApiError, useProviderApi } from "./useProviderApi";
import type { ProviderRow } from "./types";

interface DeleteConfirmProps {
  row: ProviderRow;
  onClose: () => void;
  onDeleted: () => void;
}

export default function DeleteConfirm({
  row,
  onClose,
  onDeleted,
}: DeleteConfirmProps): JSX.Element {
  const api = useProviderApi();
  const [submitting, setSubmitting] = useState(false);
  const [conflictDeptIds, setConflictDeptIds] = useState<string[] | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);

  const confirm = async () => {
    setSubmitting(true);
    setError(null);
    setConflictDeptIds(null);
    try {
      await api.delete(row.id);
      onDeleted();
      onClose();
    } catch (exc) {
      if (exc instanceof ApiError && exc.status === 409) {
        const body = exc.body as { dept_ids?: string[]; error?: string };
        if (Array.isArray(body?.dept_ids)) {
          setConflictDeptIds(body.dept_ids);
          return;
        }
      }
      setError(
        exc instanceof Error ? exc.message : String(exc ?? "delete failed"),
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/40"
      data-testid="llm-provider-delete-modal"
    >
      <div className="w-full max-w-md rounded bg-white p-6 shadow-lg">
        <h2 className="text-lg font-semibold">Delete provider?</h2>
        <p className="mt-2 text-sm text-gray-700">
          You are about to delete <strong>{row.name}</strong>. This
          action cannot be undone.
        </p>

        {conflictDeptIds ? (
          <div
            role="alert"
            className="mt-3 rounded bg-amber-50 px-3 py-2 text-sm text-amber-800"
            data-testid="llm-provider-delete-conflict-toast"
          >
            <p className="font-medium">
              Cannot delete — provider in use
            </p>
            <p>
              The following departments still pin this provider:
            </p>
            <ul className="mt-1 list-disc pl-5">
              {conflictDeptIds.map((dept) => (
                <li key={dept} data-testid="llm-provider-delete-conflict-dept">
                  {dept}
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {error ? (
          <p className="mt-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700">
            {error}
          </p>
        ) : null}

        <footer className="mt-6 flex justify-end gap-2">
          <button
            type="button"
            className="rounded border border-gray-300 px-3 py-1 text-sm"
            onClick={onClose}
            disabled={submitting}
          >
            Cancel
          </button>
          <button
            type="button"
            className={
              "rounded bg-red-600 px-3 py-1 text-sm text-white " +
              "hover:bg-red-700 disabled:opacity-50"
            }
            onClick={confirm}
            disabled={submitting || conflictDeptIds !== null}
            data-testid="llm-provider-delete-confirm"
          >
            {submitting ? "Deleting…" : "Delete"}
          </button>
        </footer>
      </div>
    </div>
  );
}
