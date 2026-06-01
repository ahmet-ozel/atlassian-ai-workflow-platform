"use client";

/**
 * CredentialModal — 3-tab credential management modal for a single
 * department (uyumluluk task 4.1, R1.8 / R1.9 / Q1).
 *
 * Tabs:
 *   * "Jira Credential"
 *   * "Confluence Credential"
 *   * "Bitbucket Credential"
 *
 * Each tab is rendered by :file:`CredentialServiceTab.tsx` which
 * owns the form state for its service. The modal owns the
 * tab-selector + the dept-level refetch trigger so a save in one
 * tab updates the badges shown in the others (e.g. credential_ref
 * derived from the just-promoted Vault path).
 *
 * Wire shape comes from
 * ``GET /admin/departments/{id}`` →
 * services/automation-service/src/routers/dept_credentials.py
 * (``_select_department_detail``):
 *
 *     {
 *       "id": "...",
 *       "display_name": "...",
 *       "mode": "active",
 *       "default_language": "...",
 *       "web_search_enabled": true,
 *       "jira_project_keys": ["..."],
 *       "confluence_space_keys": ["..."],
 *       "bots": [
 *         {"service": "jira",
 *          "credential_ref": "vault:atlassian/<id>/jira",
 *          "account_id": "...",
 *          "username": "...",
 *          "deployment": null}
 *       ]
 *     }
 *
 * The modal owns its own GET on mount + after every save / remove.
 * The parent route only owns the open/close state (R1.9 — modal
 * closes on save success; the dept catalog listens to ``onClosed``
 * to refetch its row for that dept).
 */

import { useCallback, useEffect, useMemo, useState } from "react";

import { apiFetch } from "@/lib/api-client";

import CredentialServiceTab, {
  type BotRow,
  type ServiceName,
} from "./CredentialServiceTab";

// ---------------------------------------------------------------------------
// Wire types
// ---------------------------------------------------------------------------

type DepartmentDetail = {
  id: string;
  display_name: string;
  default_language: string | null;
  web_search_enabled: boolean;
  mode: string;
  created_at: string | null;
  updated_at: string | null;
  jira_project_keys: string[];
  confluence_space_keys: string[];
  bots: BotRow[];
};

type LoadState =
  | { kind: "loading" }
  | { kind: "ok"; detail: DepartmentDetail }
  | { kind: "error"; message: string };

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export type CredentialModalProps = {
  deptId: string;
  /**
   * Called when the modal is dismissed (Cancel button, backdrop
   * click, Escape, or after a successful save). The parent
   * unmounts the modal and refetches the departments table.
   */
  onClose: () => void;
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const TABS: ReadonlyArray<{ id: ServiceName; label: string }> = [
  { id: "jira", label: "Jira Credential" },
  { id: "confluence", label: "Confluence Credential" },
  { id: "bitbucket", label: "Bitbucket Credential" },
];

async function safeReadDetail(res: Response): Promise<string> {
  try {
    const ct = res.headers.get("content-type") ?? "";
    if (ct.includes("application/json")) {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === "string") return body.detail;
      if (Array.isArray(body.detail)) return JSON.stringify(body.detail);
      return JSON.stringify(body);
    }
    return (await res.text()).slice(0, 400);
  } catch {
    return res.statusText || `HTTP ${res.status}`;
  }
}

// ---------------------------------------------------------------------------
// Inline styles — match StartFormModal (services pages)
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
  width: "min(720px, 94vw)",
  maxHeight: "92vh",
  overflowY: "auto",
  boxShadow: "0 10px 30px rgba(0,0,0,0.3)",
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function CredentialModal({
  deptId,
  onClose,
}: CredentialModalProps): JSX.Element {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [activeTab, setActiveTab] = useState<ServiceName>("jira");

  // -------------------------------------------------------------------------
  // Dept detail fetch (mounts + manual refetch hooks).
  // -------------------------------------------------------------------------

  const fetchDetail = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      const res = await apiFetch(
        `/admin/departments/${encodeURIComponent(deptId)}`,
      );
      if (!res.ok) {
        const detail = await safeReadDetail(res);
        setState({
          kind: "error",
          message: `GET /admin/departments/${deptId} → HTTP ${res.status}: ${detail}`,
        });
        return;
      }
      const body = (await res.json()) as DepartmentDetail;
      setState({ kind: "ok", detail: body });
    } catch (err) {
      setState({
        kind: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }, [deptId]);

  useEffect(() => {
    void fetchDetail();
  }, [fetchDetail]);

  // -------------------------------------------------------------------------
  // Modal ergonomics — Escape closes, backdrop click closes.
  // -------------------------------------------------------------------------

  useEffect(() => {
    function onKey(ev: KeyboardEvent): void {
      if (ev.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // -------------------------------------------------------------------------
  // Per-tab existing-row lookup
  // -------------------------------------------------------------------------

  const botsByService = useMemo(() => {
    if (state.kind !== "ok") return {} as Record<string, BotRow>;
    const out: Record<string, BotRow> = {};
    for (const bot of state.detail.bots) {
      out[bot.service] = bot;
    }
    return out;
  }, [state]);

  const handleSaved = useCallback(() => {
    // Refresh dept detail in-place so the other tabs see the new
    // credential_ref / account_id, then close the modal per R1.9.
    void fetchDetail();
    onClose();
  }, [fetchDetail, onClose]);

  const handleRemoved = useCallback(() => {
    void fetchDetail();
  }, [fetchDetail]);

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  const titleId = `cred-modal-title-${deptId}`;

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
            Credentials · <code>{deptId}</code>
            {state.kind === "ok" && (
              <span
                style={{
                  marginLeft: "0.6rem",
                  color: "#6b7280",
                  fontWeight: 400,
                  fontSize: "0.9rem",
                }}
              >
                {state.detail.display_name}
              </span>
            )}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close credential modal"
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

        {state.kind === "loading" && <p>Loading department detail…</p>}

        {state.kind === "error" && (
          <div
            role="alert"
            style={{
              background: "#fef3c7",
              color: "#78350f",
              padding: "0.75rem",
              borderRadius: 4,
              marginBottom: "1rem",
            }}
          >
            Failed to load dept detail: {state.message}
          </div>
        )}

        {state.kind === "ok" && (
          <>
            <div
              role="tablist"
              aria-label="Credential services"
              style={{
                display: "flex",
                gap: "0.25rem",
                borderBottom: "1px solid #e5e7eb",
                marginBottom: "1rem",
              }}
            >
              {TABS.map((tab) => {
                const selected = activeTab === tab.id;
                const existing = botsByService[tab.id] != null;
                return (
                  <button
                    key={tab.id}
                    type="button"
                    role="tab"
                    id={`cred-tab-${tab.id}`}
                    aria-selected={selected}
                    aria-controls={`cred-tabpanel-${tab.id}`}
                    tabIndex={selected ? 0 : -1}
                    onClick={() => setActiveTab(tab.id)}
                    style={{
                      padding: "0.5rem 1rem",
                      border: "none",
                      borderBottom: selected
                        ? "2px solid #2563eb"
                        : "2px solid transparent",
                      background: "transparent",
                      color: selected ? "#1d4ed8" : "#374151",
                      fontWeight: selected ? 600 : 500,
                      fontSize: "0.95rem",
                      cursor: "pointer",
                      marginBottom: "-1px",
                      display: "flex",
                      alignItems: "center",
                      gap: "0.4rem",
                    }}
                  >
                    {tab.label}
                    {existing && (
                      <span
                        aria-label="credential saved"
                        title="A credential is already saved for this service"
                        style={{
                          fontSize: "0.7rem",
                          color: "#166534",
                        }}
                      >
                        ●
                      </span>
                    )}
                  </button>
                );
              })}
            </div>

            {TABS.map((tab) => (
              <div
                key={tab.id}
                role="tabpanel"
                id={`cred-tabpanel-${tab.id}`}
                aria-labelledby={`cred-tab-${tab.id}`}
                hidden={activeTab !== tab.id}
              >
                {activeTab === tab.id && (
                  <CredentialServiceTab
                    deptId={deptId}
                    service={tab.id}
                    existing={botsByService[tab.id] ?? null}
                    onSaved={handleSaved}
                    onRemoved={handleRemoved}
                  />
                )}
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
}
