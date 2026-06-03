"use client";

/**
 * Department detail route.
 *
 * The route exists primarily as a mount point for the credential
 * modal (:file:`../_components/CredentialModal.tsx`). Operators
 * navigate here from the departments table (row click
 * handler) and the modal opens automatically when credentials exist.
 *
 * When no credentials are bound to the department, the page shows a
 * yellow "Pending Credentials" badge with an "Add Credential" button
 * Clicking the button opens the CredentialModal.
 *
 * When credentials exist, the page shows a green "Active" badge and
 * the CredentialModal opens automatically (existing behavior).
 *
 * Closing the modal navigates back to ``/departments`` so the
 * catalog refetches and the credential indicators reflect the
 * latest state.
 *
 * Additionally, an "Assigned SSH Runners" section allows operators to
 * assign/unassign SSH runners to this department via a multi-select
 * dropdown, and displays a table of assigned runners with metrics.
 * If the global runner pool is empty, a `runner_pool_empty` warning
 * banner is shown.
 */

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api-client";
import CredentialModal from "../_components/CredentialModal";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type BotRow = {
  service: string;
  credential_ref: string | null;
  account_id: string | null;
  username: string | null;
  deployment: string | null;
};

type DepartmentDetail = {
  id: string;
  display_name: string;
  mode: string;
  bots: BotRow[];
};

type LoadState =
  | { kind: "loading" }
  | { kind: "ok"; detail: DepartmentDetail }
  | { kind: "error"; message: string };

type PageProps = {
  params: { id: string };
};

/** SSH runner as returned by GET /admin/ssh-runners */
type SshRunner = {
  runner_id: string;
  host: string;
  port: number;
  username: string;
  status: "active" | "disabled" | "quarantine";
  active_workflows?: number;
  last_healthcheck?: string | null;
  last_rotation?: string | null;
};

// ---------------------------------------------------------------------------
// Inline styles — SSH Runners section
// ---------------------------------------------------------------------------

const sectionStyle: React.CSSProperties = {
  border: "1px solid #e5e7eb",
  borderRadius: 8,
  padding: "1.25rem",
  marginTop: "1.5rem",
};

const runnerTableStyle: React.CSSProperties = {
  width: "100%",
  borderCollapse: "collapse",
  fontSize: "0.9rem",
  marginTop: "0.75rem",
};

const runnerThStyle: React.CSSProperties = {
  borderBottom: "1px solid #ccc",
  textAlign: "left",
  padding: "0.5rem 0.75rem",
  fontWeight: 600,
};

const runnerTdStyle: React.CSSProperties = {
  borderBottom: "1px solid #eee",
  padding: "0.5rem 0.75rem",
  verticalAlign: "middle",
};

const multiSelectContainerStyle: React.CSSProperties = {
  position: "relative",
  marginBottom: "0.75rem",
};

const multiSelectButtonStyle: React.CSSProperties = {
  width: "100%",
  minHeight: "2.25rem",
  padding: "0.4rem 0.75rem",
  border: "1px solid #d1d5db",
  borderRadius: 4,
  background: "#fff",
  cursor: "pointer",
  textAlign: "left",
  fontSize: "0.9rem",
  display: "flex",
  alignItems: "center",
  flexWrap: "wrap",
  gap: "0.35rem",
};

const dropdownStyle: React.CSSProperties = {
  position: "absolute",
  top: "100%",
  left: 0,
  right: 0,
  background: "#fff",
  border: "1px solid #d1d5db",
  borderRadius: 4,
  boxShadow: "0 4px 12px rgba(0,0,0,0.1)",
  zIndex: 50,
  maxHeight: 200,
  overflowY: "auto",
  marginTop: 2,
};

const dropdownItemStyle = (selected: boolean): React.CSSProperties => ({
  padding: "0.5rem 0.75rem",
  cursor: "pointer",
  background: selected ? "#eff6ff" : "transparent",
  fontSize: "0.85rem",
  display: "flex",
  alignItems: "center",
  gap: "0.5rem",
});

const chipStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: "0.25rem",
  background: "#e0e7ff",
  color: "#3730a3",
  borderRadius: 12,
  padding: "0.15rem 0.5rem",
  fontSize: "0.75rem",
  fontWeight: 500,
};

const warningBannerStyle: React.CSSProperties = {
  background: "#fef3c7",
  color: "#92400e",
  border: "1px solid #fcd34d",
  borderRadius: 6,
  padding: "0.75rem 1rem",
  fontSize: "0.9rem",
  display: "flex",
  alignItems: "center",
  gap: "0.5rem",
  marginBottom: "0.75rem",
};

const runnerStatusBadge = (
  status: SshRunner["status"],
): React.CSSProperties => {
  const colors: Record<SshRunner["status"], { bg: string; color: string }> = {
    active: { bg: "#dcfce7", color: "#166534" },
    disabled: { bg: "#f3f4f6", color: "#374151" },
    quarantine: { bg: "#fef2f2", color: "#991b1b" },
  };
  const c = colors[status] ?? colors.active;
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
// Component
// ---------------------------------------------------------------------------

export default function DepartmentDetailPage({
  params,
}: PageProps): JSX.Element {
  const router = useRouter();
  const { id } = params;

  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [showCredentialModal, setShowCredentialModal] = useState(false);

  // SSH Runners state
  const [allRunners, setAllRunners] = useState<SshRunner[]>([]);
  const [assignedRunnerIds, setAssignedRunnerIds] = useState<string[]>([]);
  const [runnersLoading, setRunnersLoading] = useState(false);
  const [runnersError, setRunnersError] = useState<string | null>(null);
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const [assignmentSaving, setAssignmentSaving] = useState(false);

  // -------------------------------------------------------------------------
  // Fetch department detail to determine credential status
  // -------------------------------------------------------------------------

  const fetchDetail = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      const res = await apiFetch(
        `/admin/departments/${encodeURIComponent(id)}`,
      );
      if (!res.ok) {
        setState({
          kind: "error",
          message: `HTTP ${res.status}`,
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
  }, [id]);

  useEffect(() => {
    void fetchDetail();
  }, [fetchDetail]);

  // -------------------------------------------------------------------------
  // Fetch SSH runners (all + assigned to this dept)
  // -------------------------------------------------------------------------

  const fetchRunners = useCallback(async () => {
    setRunnersLoading(true);
    setRunnersError(null);
    try {
      const [allRes, assignedRes] = await Promise.all([
        apiFetch("/admin/ssh-runners"),
        apiFetch(`/admin/departments/${encodeURIComponent(id)}/ssh-runners`),
      ]);

      if (allRes.ok) {
        const data = (await allRes.json()) as { runners: SshRunner[] };
        setAllRunners(data.runners ?? []);
      } else {
        setRunnersError(`Failed to load runners: HTTP ${allRes.status}`);
      }

      if (assignedRes.ok) {
        const data = (await assignedRes.json()) as { runners: SshRunner[] };
        setAssignedRunnerIds(
          (data.runners ?? []).map((r: SshRunner) => r.runner_id),
        );
      }
    } catch (err) {
      setRunnersError((err as Error).message);
    } finally {
      setRunnersLoading(false);
    }
  }, [id]);

  useEffect(() => {
    void fetchRunners();
  }, [fetchRunners]);

  // -------------------------------------------------------------------------
  // Derived state: does the department have any bound credentials?
  // -------------------------------------------------------------------------

  const hasCredentials =
    state.kind === "ok" &&
    state.detail.bots.some((bot) => bot.credential_ref != null);

  // Derived: is the runner pool empty (no runners exist at all)?
  const runnerPoolEmpty = !runnersLoading && allRunners.length === 0;

  // Derived: assigned runners with full details
  const assignedRunners = allRunners.filter((r) =>
    assignedRunnerIds.includes(r.runner_id),
  );

  // -------------------------------------------------------------------------
  // Handlers
  // -------------------------------------------------------------------------

  const handleClose = useCallback(() => {
    setShowCredentialModal(false);
    router.push("/departments");
  }, [router]);

  const handleOpenCredentialModal = useCallback(() => {
    setShowCredentialModal(true);
  }, []);

  /** Toggle a runner in the multi-select and persist to backend */
  const handleRunnerToggle = useCallback(
    async (runnerId: string) => {
      const isCurrentlyAssigned = assignedRunnerIds.includes(runnerId);
      const newIds = isCurrentlyAssigned
        ? assignedRunnerIds.filter((rid) => rid !== runnerId)
        : [...assignedRunnerIds, runnerId];

      // Optimistic update
      setAssignedRunnerIds(newIds);
      setAssignmentSaving(true);
      setRunnersError(null);

      try {
        const res = await apiFetch(
          `/admin/departments/${encodeURIComponent(id)}/ssh-runners`,
          {
            method: "POST",
            body: JSON.stringify({ runner_ids: newIds }),
          },
        );
        if (!res.ok) {
          // Revert on failure
          setAssignedRunnerIds(assignedRunnerIds);
          const body = await res.text();
          setRunnersError(
            `Assignment failed: HTTP ${res.status} — ${body.slice(0, 200)}`,
          );
        }
      } catch (err) {
        // Revert on failure
        setAssignedRunnerIds(assignedRunnerIds);
        setRunnersError((err as Error).message);
      } finally {
        setAssignmentSaving(false);
      }
    },
    [assignedRunnerIds, id],
  );

  const formatDate = (dateStr: string | null | undefined): string => {
    if (!dateStr) return "—";
    try {
      return new Date(dateStr).toLocaleString("tr-TR");
    } catch {
      return dateStr;
    }
  };

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  return (
    <main
      style={{
        padding: "1.5rem",
        fontFamily: "system-ui, sans-serif",
        maxWidth: 960,
        margin: "0 auto",
      }}
    >
      <nav
        style={{
          fontSize: "0.85rem",
          color: "#6b7280",
          marginBottom: "0.75rem",
        }}
      >
        <a href="/departments" style={{ color: "#2563eb", textDecoration: "none" }}>
          ← Departmanlar
        </a>
      </nav>

      <header style={{ marginBottom: "1rem" }}>
        <h1 style={{ margin: 0, fontSize: "1.4rem" }}>
          Departman · <code>{id}</code>
          {state.kind === "ok" && state.detail.display_name && (
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
        </h1>
        <p style={{ margin: "0.25rem 0 0", color: "#6b7280", fontSize: "0.9rem" }}>
          Bu departman için Jira / Confluence / Bitbucket bot kimlik
          bilgilerini yönetin.
        </p>
      </header>

      {/* Status badge section */}
      {state.kind === "loading" && (
        <p style={{ color: "#6b7280" }}>Departman detayı yükleniyor…</p>
      )}

      {state.kind === "error" && (
        <div
          role="alert"
          style={{
            background: "#fef2f2",
            color: "#991b1b",
            padding: "0.75rem",
            borderRadius: 4,
            marginBottom: "1rem",
          }}
        >
          Departman yüklenemedi: {state.message}
        </div>
      )}

      {state.kind === "ok" && !hasCredentials && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "0.75rem",
            marginBottom: "1rem",
          }}
        >
          <span
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: "0.35rem",
              background: "#fef3c7",
              color: "#92400e",
              border: "1px solid #fcd34d",
              borderRadius: 4,
              padding: "0.3rem 0.65rem",
              fontSize: "0.85rem",
              fontWeight: 600,
            }}
            role="status"
            aria-label="Pending Credentials"
          >
            ⚠ Kimlik bilgileri bekleniyor
          </span>
          <button
            type="button"
            onClick={handleOpenCredentialModal}
            style={{
              background: "#2563eb",
              color: "#fff",
              border: "none",
              padding: "0.4rem 0.8rem",
              borderRadius: 4,
              fontSize: "0.85rem",
              fontWeight: 500,
              cursor: "pointer",
            }}
          >
            Kimlik bilgisi ekle
          </button>
        </div>
      )}

      {state.kind === "ok" && hasCredentials && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "0.75rem",
            marginBottom: "1rem",
          }}
        >
          <span
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: "0.35rem",
              background: "#dcfce7",
              color: "#166534",
              border: "1px solid #86efac",
              borderRadius: 4,
              padding: "0.3rem 0.65rem",
              fontSize: "0.85rem",
              fontWeight: 600,
            }}
            role="status"
            aria-label="Active"
          >
            ✓ Aktif
          </span>
          <button
            type="button"
            onClick={handleOpenCredentialModal}
            style={{
              background: "#2563eb",
              color: "#fff",
              border: "none",
              padding: "0.4rem 0.8rem",
              borderRadius: 4,
              fontSize: "0.85rem",
              fontWeight: 500,
              cursor: "pointer",
            }}
          >
            Kimlik bilgilerini yönet
          </button>
        </div>
      )}

      {/* Credential modal — shown on demand or auto-opened when credentials exist */}
      {showCredentialModal && (
        <CredentialModal deptId={id} onClose={handleClose} />
      )}

      {/* ================================================================= */}
      {/* Assigned SSH Runners Section */}
      {/* ================================================================= */}
      {state.kind === "ok" && (
        <section style={sectionStyle} aria-labelledby="ssh-runners-heading">
          <h2
            id="ssh-runners-heading"
            style={{ margin: "0 0 0.75rem 0", fontSize: "1.1rem" }}
          >
            Atanmış SSH Runner'ları
          </h2>

          {/* Runner pool empty warning banner */}
          {runnerPoolEmpty && (
            <div
              role="alert"
              aria-label="runner_pool_empty"
              style={warningBannerStyle}
            >
              <span aria-hidden="true">⚠️</span>
              <span>
                SSH runner havuzu boş — henüz hiçbir runner tanımlanmamış.
                Lütfen önce{" "}
                <a
                  href="/security"
                  style={{ color: "#92400e", fontWeight: 600 }}
                >
                  Security → SSH Runners
                </a>{" "}
                sayfasından runner ekleyin.
              </span>
            </div>
          )}

          {runnersError && (
            <p role="alert" style={{ color: "crimson", fontSize: "0.9rem" }}>
              Hata: {runnersError}
            </p>
          )}

          {runnersLoading && (
            <p style={{ color: "#6b7280", fontSize: "0.9rem" }}>
              Runner bilgileri yükleniyor…
            </p>
          )}

          {/* Multi-select dropdown for runner assignment */}
          {!runnersLoading && !runnerPoolEmpty && (
            <div style={multiSelectContainerStyle}>
              <label
                htmlFor="runner-multiselect"
                style={{
                  display: "block",
                  fontSize: "0.85rem",
                  fontWeight: 500,
                  marginBottom: "0.35rem",
                  color: "#374151",
                }}
              >
                Runner Ataması
                {assignmentSaving && (
                  <span
                    style={{
                      marginLeft: "0.5rem",
                      fontSize: "0.75rem",
                      color: "#6b7280",
                    }}
                  >
                    (kaydediliyor…)
                  </span>
                )}
              </label>
              <button
                id="runner-multiselect"
                type="button"
                style={multiSelectButtonStyle}
                onClick={() => setDropdownOpen((prev) => !prev)}
                aria-expanded={dropdownOpen}
                aria-haspopup="listbox"
              >
                {assignedRunnerIds.length === 0 ? (
                  <span style={{ color: "#9ca3af" }}>
                    Runner seçin…
                  </span>
                ) : (
                  assignedRunners.map((r) => (
                    <span key={r.runner_id} style={chipStyle}>
                      {r.host}
                      {r.port !== 22 && `:${r.port}`}
                    </span>
                  ))
                )}
              </button>

              {dropdownOpen && (
                <div
                  style={dropdownStyle}
                  role="listbox"
                  aria-multiselectable="true"
                  aria-label="SSH Runner listesi"
                >
                  {allRunners.map((runner) => {
                    const isSelected = assignedRunnerIds.includes(
                      runner.runner_id,
                    );
                    return (
                      <div
                        key={runner.runner_id}
                        role="option"
                        aria-selected={isSelected}
                        style={dropdownItemStyle(isSelected)}
                        onClick={() => void handleRunnerToggle(runner.runner_id)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            void handleRunnerToggle(runner.runner_id);
                          }
                        }}
                        tabIndex={0}
                      >
                        <input
                          type="checkbox"
                          checked={isSelected}
                          readOnly
                          tabIndex={-1}
                          style={{ margin: 0 }}
                        />
                        <span>
                          <strong>{runner.host}</strong>
                          {runner.port !== 22 && (
                            <span style={{ color: "#6b7280" }}>
                              :{runner.port}
                            </span>
                          )}
                          <span
                            style={{
                              marginLeft: "0.5rem",
                              fontSize: "0.75rem",
                              color: "#6b7280",
                            }}
                          >
                            ({runner.runner_id})
                          </span>
                        </span>
                        <span style={runnerStatusBadge(runner.status)}>
                          {runner.status}
                        </span>
                      </div>
                    );
                  })}
                  {allRunners.length === 0 && (
                    <div
                      style={{
                        padding: "0.75rem",
                        color: "#6b7280",
                        fontSize: "0.85rem",
                      }}
                    >
                      Kayıtlı runner bulunamadı.
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          {/* Runner Table — assigned runners with metrics */}
          {!runnersLoading && assignedRunners.length > 0 && (
            <div style={{ overflowX: "auto" }}>
              <table style={runnerTableStyle}>
                <thead>
                  <tr>
                    <th style={runnerThStyle}>Runner ID</th>
                    <th style={runnerThStyle}>Host</th>
                    <th style={runnerThStyle}>Durum</th>
                    <th style={runnerThStyle}>Aktif Workflow</th>
                    <th style={runnerThStyle}>Son Healthcheck</th>
                    <th style={runnerThStyle}>Son Rotation</th>
                  </tr>
                </thead>
                <tbody>
                  {assignedRunners.map((runner) => (
                    <tr key={runner.runner_id}>
                      <td style={runnerTdStyle}>
                        <code style={{ fontSize: "0.85rem" }}>
                          {runner.runner_id}
                        </code>
                      </td>
                      <td style={runnerTdStyle}>
                        <strong>{runner.host}</strong>
                        {runner.port !== 22 && (
                          <span style={{ color: "#6b7280" }}>
                            :{runner.port}
                          </span>
                        )}
                      </td>
                      <td style={runnerTdStyle}>
                        <span style={runnerStatusBadge(runner.status)}>
                          {runner.status === "active"
                            ? "Aktif"
                            : runner.status === "disabled"
                              ? "Devre Dışı"
                              : "Karantina"}
                        </span>
                      </td>
                      <td style={runnerTdStyle}>
                        {runner.active_workflows ?? 0}
                      </td>
                      <td style={runnerTdStyle}>
                        {formatDate(runner.last_healthcheck)}
                      </td>
                      <td style={runnerTdStyle}>
                        {formatDate(runner.last_rotation)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Empty state when runners exist but none assigned */}
          {!runnersLoading &&
            !runnerPoolEmpty &&
            assignedRunners.length === 0 && (
              <p
                style={{
                  color: "#6b7280",
                  fontSize: "0.9rem",
                  marginTop: "0.5rem",
                }}
              >
                Bu departmana henüz runner atanmamış. Yukarıdaki dropdown'dan
                runner seçebilirsiniz.
              </p>
            )}
        </section>
      )}
    </main>
  );
}
