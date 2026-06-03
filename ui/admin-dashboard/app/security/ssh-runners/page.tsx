"use client";

/**
 * SSH Runners Admin Page.
 *
 * Route: /admin/security/ssh-runners
 *
 * Provides full CRUD for the SSH runner pool:
 * - List all runners with metrics (active workflows, healthcheck, rotation)
 * - Create new runner (host, port, username, private_key)
 * - Edit runner (host, port)
 * - Disable/Enable toggle (PATCH status)
 */

import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api-client";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type SshRunner = {
  runner_id: string;
  host: string;
  port: number;
  username: string;
  base_path: string;
  vault_path: string;
  status: string; // "active" | "disabled" | "quarantine"
  created_at: string;
  updated_at: string;
  // Extended metrics (may be absent if backend doesn't provide yet)
  active_workflow_count?: number;
  last_healthcheck_at?: string | null;
  last_healthcheck_status?: string | null;
  last_rotation_at?: string | null;
};

type CreateRunnerForm = {
  runner_id: string;
  host: string;
  port: number;
  username: string;
  base_path: string;
  private_key: string;
};

type EditRunnerForm = {
  host: string;
  port: number;
  base_path: string;
  status: string;
};

type DockerSmokeResult = {
  runner_id: string;
  status: string;
  exit_code: number;
  duration_ms: number;
  stdout: string;
  stderr: string;
  workspace: string;
};

// ---------------------------------------------------------------------------
// Inline styles
// ---------------------------------------------------------------------------

const pageStyle: React.CSSProperties = {
  padding: "1rem",
};

const headerStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "space-between",
  alignItems: "center",
  marginBottom: "1rem",
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

const btnSuccessStyle: React.CSSProperties = {
  ...btnStyle,
  background: "#16a34a",
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

const formGroupStyle: React.CSSProperties = {
  marginBottom: "1rem",
};

const labelStyle: React.CSSProperties = {
  display: "block",
  marginBottom: "0.25rem",
  fontWeight: 500,
  fontSize: "0.85rem",
};

const inputStyle: React.CSSProperties = {
  width: "100%",
  padding: "0.5rem",
  border: "1px solid #d1d5db",
  borderRadius: 4,
  fontSize: "0.9rem",
  boxSizing: "border-box",
};

const textareaStyle: React.CSSProperties = {
  ...inputStyle,
  minHeight: "100px",
  fontFamily: "monospace",
  fontSize: "0.8rem",
};

const statusBadge = (status: string): React.CSSProperties => {
  const colors: Record<string, { bg: string; color: string }> = {
    active: { bg: "#dcfce7", color: "#166534" },
    disabled: { bg: "#fef9c3", color: "#854d0e" },
    quarantine: { bg: "#fef2f2", color: "#991b1b" },
  };
  const c = colors[status] ?? { bg: "#f3f4f6", color: "#374151" };
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

export default function SshRunnersPage(): JSX.Element {
  const [runners, setRunners] = useState<SshRunner[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [actionLoading, setActionLoading] = useState(false);
  const [dockerSmoke, setDockerSmoke] = useState<DockerSmokeResult | null>(null);

  // Modal state
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [editRunner, setEditRunner] = useState<SshRunner | null>(null);

  // Create form state
  const [createForm, setCreateForm] = useState<CreateRunnerForm>({
    runner_id: "",
    host: "",
    port: 22,
    username: "",
    base_path: "/var/ai-runner",
    private_key: "",
  });

  // Edit form state
  const [editForm, setEditForm] = useState<EditRunnerForm>({
    host: "",
    port: 22,
    base_path: "/var/ai-runner",
    status: "active",
  });

  // --- Fetch runners -------------------------------------------------------

  const fetchRunners = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch("/admin/ssh-runners");
      if (!res.ok) {
        const body = await res.text();
        setError(`HTTP ${res.status}: ${body.slice(0, 200)}`);
        return;
      }
      const data = (await res.json()) as { runners: SshRunner[] };
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

  // --- Create runner -------------------------------------------------------

  const handleCreate = useCallback(async () => {
    if (!createForm.runner_id || !createForm.host || !createForm.username || !createForm.base_path || !createForm.private_key) {
      setError("Tüm alanlar zorunludur.");
      return;
    }

    setActionLoading(true);
    setError(null);
    try {
      const res = await apiFetch("/admin/ssh-runners", {
        method: "POST",
        body: JSON.stringify({
          runner_id: createForm.runner_id,
          host: createForm.host,
          port: createForm.port,
          username: createForm.username,
          base_path: createForm.base_path,
          private_key: createForm.private_key,
        }),
      });

      if (!res.ok) {
        const body = await res.text();
        setError(`Runner oluşturulamadı (HTTP ${res.status}): ${body.slice(0, 200)}`);
        return;
      }

      setShowCreateModal(false);
      setCreateForm({ runner_id: "", host: "", port: 22, username: "", base_path: "/var/ai-runner", private_key: "" });
      await fetchRunners();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setActionLoading(false);
    }
  }, [createForm, fetchRunners]);

  // --- Edit runner ---------------------------------------------------------

  const openEditModal = useCallback((runner: SshRunner) => {
    setEditRunner(runner);
    setEditForm({
      host: runner.host,
      port: runner.port,
      base_path: runner.base_path || "/var/ai-runner",
      status: runner.status,
    });
  }, []);

  const handleEdit = useCallback(async () => {
    if (!editRunner) return;

    setActionLoading(true);
    setError(null);
    try {
      const body: Record<string, unknown> = {};
      if (editForm.host !== editRunner.host) body.host = editForm.host;
      if (editForm.port !== editRunner.port) body.port = editForm.port;
      if (editForm.base_path !== editRunner.base_path) body.base_path = editForm.base_path;
      if (editForm.status !== editRunner.status) body.status = editForm.status;

      if (Object.keys(body).length === 0) {
        setEditRunner(null);
        return;
      }

      const res = await apiFetch(
        `/admin/ssh-runners/${encodeURIComponent(editRunner.runner_id)}`,
        {
          method: "PATCH",
          body: JSON.stringify(body),
        },
      );

      if (!res.ok) {
        const text = await res.text();
        setError(`Güncelleme başarısız (HTTP ${res.status}): ${text.slice(0, 200)}`);
        return;
      }

      setEditRunner(null);
      await fetchRunners();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setActionLoading(false);
    }
  }, [editRunner, editForm, fetchRunners]);

  // --- Toggle status -------------------------------------------------------

  const handleToggleStatus = useCallback(
    async (runner: SshRunner) => {
      const newStatus = runner.status === "active" ? "disabled" : "active";
      setActionLoading(true);
      setError(null);
      try {
        const res = await apiFetch(
          `/admin/ssh-runners/${encodeURIComponent(runner.runner_id)}`,
          {
            method: "PATCH",
            body: JSON.stringify({ status: newStatus }),
          },
        );

        if (!res.ok) {
          const text = await res.text();
          setError(`Durum değiştirilemedi (HTTP ${res.status}): ${text.slice(0, 200)}`);
          return;
        }

        await fetchRunners();
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setActionLoading(false);
      }
    },
    [fetchRunners],
  );

  const handleDockerSmoke = useCallback(
    async (runner: SshRunner) => {
      setActionLoading(true);
      setError(null);
      setDockerSmoke(null);
      try {
        const res = await apiFetch(
          `/admin/ssh-runners/${encodeURIComponent(runner.runner_id)}/docker-smoke`,
          { method: "POST" },
        );
        const text = await res.text();
        if (!res.ok) {
          setError(`Docker smoke failed (HTTP ${res.status}): ${text.slice(0, 300)}`);
          return;
        }
        setDockerSmoke(JSON.parse(text) as DockerSmokeResult);
        await fetchRunners();
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setActionLoading(false);
      }
    },
    [fetchRunners],
  );

  // --- Helpers -------------------------------------------------------------

  const formatDate = (dateStr: string | null | undefined): string => {
    if (!dateStr) return "—";
    try {
      return new Date(dateStr).toLocaleString("tr-TR");
    } catch {
      return dateStr;
    }
  };

  const getHealthcheckBadge = (runner: SshRunner): JSX.Element => {
    const status = runner.last_healthcheck_status;
    if (!status) return <span style={{ color: "#6b7280" }}>—</span>;
    const icon = status === "healthy" ? "🟢" : status === "unhealthy" ? "🔴" : "🟡";
    return (
      <span>
        {icon} {status}
      </span>
    );
  };

  // --- Render --------------------------------------------------------------

  return (
    <main style={pageStyle}>
      <div style={headerStyle}>
        <h1 style={{ margin: 0 }}>SSH Runners</h1>
        <div>
          <button style={btnStyle} onClick={fetchRunners} disabled={loading}>
            {loading ? "Yükleniyor…" : "Yenile"}
          </button>
          <button style={btnPrimaryStyle} onClick={() => setShowCreateModal(true)}>
            + Yeni Runner Ekle
          </button>
        </div>
      </div>

      {error && (
        <p role="alert" style={{ color: "crimson", fontSize: "0.9rem" }}>
          Hata: {error}
        </p>
      )}

      {/* Runner Table */}
      {loading && runners.length === 0 ? (
        <p style={{ color: "#6b7280" }}>Yükleniyor…</p>
      ) : runners.length === 0 ? (
        <p style={{ color: "#6b7280" }}>
          Kayıtlı SSH runner bulunamadı. Yeni bir runner eklemek için yukarıdaki butonu kullanın.
        </p>
      ) : (
        <div style={{ overflowX: "auto" }}>
          <table style={tableStyle}>
            <thead>
              <tr>
                <th style={thStyle}>Runner ID</th>
                <th style={thStyle}>Host</th>
                <th style={thStyle}>Port</th>
                <th style={thStyle}>Base Path</th>
                <th style={thStyle}>Durum</th>
                <th style={thStyle}>Aktif Workflow</th>
                <th style={thStyle}>Son Healthcheck</th>
                <th style={thStyle}>Son Rotation</th>
                <th style={thStyle}>İşlemler</th>
              </tr>
            </thead>
            <tbody>
              {runners.map((runner) => (
                <tr key={runner.runner_id}>
                  <td style={tdStyle}>
                    <code style={{ fontSize: "0.85rem" }}>{runner.runner_id}</code>
                  </td>
                  <td style={tdStyle}>{runner.host}</td>
                  <td style={tdStyle}>{runner.port}</td>
                  <td style={tdStyle}>
                    <code style={{ fontSize: "0.8rem" }}>
                      {runner.base_path || "/var/ai-runner"}
                    </code>
                  </td>
                  <td style={tdStyle}>
                    <span style={statusBadge(runner.status)}>
                      {runner.status === "active"
                        ? "Aktif"
                        : runner.status === "disabled"
                          ? "Devre Dışı"
                          : "Karantina"}
                    </span>
                  </td>
                  <td style={tdStyle}>
                    {runner.active_workflow_count != null
                      ? runner.active_workflow_count
                      : "—"}
                  </td>
                  <td style={tdStyle}>
                    {runner.last_healthcheck_at ? (
                      <span>
                        {getHealthcheckBadge(runner)}{" "}
                        <span style={{ fontSize: "0.8rem", color: "#6b7280" }}>
                          {formatDate(runner.last_healthcheck_at)}
                        </span>
                      </span>
                    ) : (
                      <span style={{ color: "#6b7280" }}>—</span>
                    )}
                  </td>
                  <td style={tdStyle}>
                    {formatDate(runner.last_rotation_at ?? runner.updated_at)}
                  </td>
                  <td style={tdStyle}>
                    <button
                      type="button"
                      style={btnStyle}
                      onClick={() => openEditModal(runner)}
                      disabled={actionLoading}
                    >
                      Düzenle
                    </button>
                    <button
                      type="button"
                      style={btnStyle}
                      onClick={() => void handleDockerSmoke(runner)}
                      disabled={actionLoading || runner.status !== "active"}
                    >
                      Docker Test
                    </button>
                    {runner.status === "active" ? (
                      <button
                        type="button"
                        style={btnDangerStyle}
                        onClick={() => handleToggleStatus(runner)}
                        disabled={actionLoading}
                      >
                        Devre Dışı Bırak
                      </button>
                    ) : (
                      <button
                        type="button"
                        style={btnSuccessStyle}
                        onClick={() => handleToggleStatus(runner)}
                        disabled={actionLoading}
                      >
                        Etkinleştir
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {dockerSmoke && (
        <div style={overlayStyle} role="presentation">
          <div style={modalStyle} role="dialog" aria-modal="true">
            <h3 style={{ margin: "0 0 1rem 0" }}>
              Docker Smoke - <code>{dockerSmoke.runner_id}</code>
            </h3>
            <p>
              <strong>{dockerSmoke.status === "passed" ? "PASSED" : "FAILED"}</strong>
              {" | exit "}
              <code>{dockerSmoke.exit_code}</code>
              {" | "}
              {dockerSmoke.duration_ms}ms
            </p>
            <p>
              Workspace: <code>{dockerSmoke.workspace}</code>
            </p>
            <pre style={{ whiteSpace: "pre-wrap", fontSize: "0.8rem" }}>
              {dockerSmoke.stdout || "(stdout empty)"}
            </pre>
            {dockerSmoke.stderr && (
              <pre style={{ whiteSpace: "pre-wrap", fontSize: "0.8rem", color: "#991b1b" }}>
                {dockerSmoke.stderr}
              </pre>
            )}
            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <button type="button" style={btnStyle} onClick={() => setDockerSmoke(null)}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ================================================================= */}
      {/* Create Runner Modal                                                */}
      {/* ================================================================= */}
      {showCreateModal && (
        <div
          style={overlayStyle}
          role="presentation"
          onMouseDown={(ev) => {
            if (ev.target === ev.currentTarget) setShowCreateModal(false);
          }}
        >
          <div
            style={modalStyle}
            role="dialog"
            aria-modal="true"
            aria-labelledby="create-runner-title"
          >
            <h3 id="create-runner-title" style={{ margin: "0 0 1rem 0" }}>
              Yeni SSH Runner Oluştur
            </h3>

            <div style={formGroupStyle}>
              <label style={labelStyle} htmlFor="create-runner-id">
                Runner ID
              </label>
              <input
                id="create-runner-id"
                style={inputStyle}
                type="text"
                placeholder="prod-runner-1"
                value={createForm.runner_id}
                onChange={(e) =>
                  setCreateForm((f) => ({ ...f, runner_id: e.target.value }))
                }
              />
            </div>

            <div style={formGroupStyle}>
              <label style={labelStyle} htmlFor="create-host">
                Host
              </label>
              <input
                id="create-host"
                style={inputStyle}
                type="text"
                placeholder="192.168.1.100"
                value={createForm.host}
                onChange={(e) =>
                  setCreateForm((f) => ({ ...f, host: e.target.value }))
                }
              />
            </div>

            <div style={formGroupStyle}>
              <label style={labelStyle} htmlFor="create-port">
                Port
              </label>
              <input
                id="create-port"
                style={inputStyle}
                type="number"
                min={1}
                max={65535}
                value={createForm.port}
                onChange={(e) =>
                  setCreateForm((f) => ({
                    ...f,
                    port: parseInt(e.target.value, 10) || 22,
                  }))
                }
              />
            </div>

            <div style={formGroupStyle}>
              <label style={labelStyle} htmlFor="create-username">
                Username
              </label>
              <input
                id="create-username"
                style={inputStyle}
                type="text"
                placeholder="ai-runner"
                value={createForm.username}
                onChange={(e) =>
                  setCreateForm((f) => ({ ...f, username: e.target.value }))
                }
              />
            </div>

            <div style={formGroupStyle}>
              <label style={labelStyle} htmlFor="create-base-path">
                Base Path
              </label>
              <input
                id="create-base-path"
                style={inputStyle}
                type="text"
                placeholder="/var/ai-runner"
                value={createForm.base_path}
                onChange={(e) =>
                  setCreateForm((f) => ({ ...f, base_path: e.target.value }))
                }
              />
            </div>

            <div style={formGroupStyle}>
              <label style={labelStyle} htmlFor="create-private-key">
                Private Key
              </label>
              <textarea
                id="create-private-key"
                style={textareaStyle}
                placeholder="-----BEGIN OPENSSH PRIVATE KEY-----&#10;..."
                value={createForm.private_key}
                onChange={(e) =>
                  setCreateForm((f) => ({ ...f, private_key: e.target.value }))
                }
              />
              <span style={{ fontSize: "0.75rem", color: "#6b7280" }}>
                Anahtar Vault&apos;a yazılacak, API&apos;den geri döndürülmez.
              </span>
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
                onClick={() => setShowCreateModal(false)}
                disabled={actionLoading}
              >
                İptal
              </button>
              <button
                type="button"
                style={btnPrimaryStyle}
                onClick={handleCreate}
                disabled={actionLoading}
              >
                {actionLoading ? "Oluşturuluyor…" : "Oluştur"}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ================================================================= */}
      {/* Edit Runner Modal                                                  */}
      {/* ================================================================= */}
      {editRunner && (
        <div
          style={overlayStyle}
          role="presentation"
          onMouseDown={(ev) => {
            if (ev.target === ev.currentTarget) setEditRunner(null);
          }}
        >
          <div
            style={modalStyle}
            role="dialog"
            aria-modal="true"
            aria-labelledby="edit-runner-title"
          >
            <h3 id="edit-runner-title" style={{ margin: "0 0 1rem 0" }}>
              Runner Düzenle: <code>{editRunner.runner_id}</code>
            </h3>

            <div style={formGroupStyle}>
              <label style={labelStyle} htmlFor="edit-host">
                Host
              </label>
              <input
                id="edit-host"
                style={inputStyle}
                type="text"
                value={editForm.host}
                onChange={(e) =>
                  setEditForm((f) => ({ ...f, host: e.target.value }))
                }
              />
            </div>

            <div style={formGroupStyle}>
              <label style={labelStyle} htmlFor="edit-port">
                Port
              </label>
              <input
                id="edit-port"
                style={inputStyle}
                type="number"
                min={1}
                max={65535}
                value={editForm.port}
                onChange={(e) =>
                  setEditForm((f) => ({
                    ...f,
                    port: parseInt(e.target.value, 10) || 22,
                  }))
                }
              />
            </div>

            <div style={formGroupStyle}>
              <label style={labelStyle} htmlFor="edit-base-path">
                Base Path
              </label>
              <input
                id="edit-base-path"
                style={inputStyle}
                type="text"
                value={editForm.base_path}
                onChange={(e) =>
                  setEditForm((f) => ({ ...f, base_path: e.target.value }))
                }
              />
            </div>

            <div style={formGroupStyle}>
              <label style={labelStyle} htmlFor="edit-status">
                Durum
              </label>
              <select
                id="edit-status"
                style={inputStyle}
                value={editForm.status}
                onChange={(e) =>
                  setEditForm((f) => ({ ...f, status: e.target.value }))
                }
              >
                <option value="active">Aktif</option>
                <option value="disabled">Devre Dışı</option>
                <option value="quarantine">Karantina</option>
              </select>
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
                onClick={() => setEditRunner(null)}
                disabled={actionLoading}
              >
                İptal
              </button>
              <button
                type="button"
                style={btnPrimaryStyle}
                onClick={handleEdit}
                disabled={actionLoading}
              >
                {actionLoading ? "Kaydediliyor…" : "Kaydet"}
              </button>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}
