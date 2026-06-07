"use client";

/**
 * Departments panel.
 *
 * Lists every department with its bot credential reference plus
 * inline buttons for the CRUD wizard (create / edit / decommission).
 *
 * Row click navigation:
 * Clicking anywhere on a row (outside the Actions cell) routes to
 * ``/departments/{id}`` where the credential modal opens. The
 * Actions cell stops propagation so the existing Edit /
 * Decommission anchors keep their original semantics.
 *
 * Wizard mode:
 * When ``?wizard=1`` query param is present, the "Yeni Departman Ekle"
 * modal opens automatically on mount. Closing the modal shows a
 * confirmation dialog warning that at least one department is required
 * to continue the wizard flow.
 */

import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState, Suspense } from "react";

import { apiFetch } from "@/lib/api-client";
import BulkImportModal from "./_components/BulkImportModal";
import CreateDepartmentModal from "./_components/CreateDepartmentModal";

type DeptRow = {
  id: string;
  display_name: string;
  bot_user: string | null;
  notify_on_success: boolean;
  notify_channels: string[];
  active_workflows?: number;
  last_probe_at?: string | null;
};

// ---------------------------------------------------------------------------
// Inner component (uses useSearchParams)
// ---------------------------------------------------------------------------

function DepartmentsPageInner(): JSX.Element {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [rows, setRows] = useState<DeptRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showBulkImportModal, setShowBulkImportModal] = useState(false);
  const [query, setQuery] = useState("");
  const [decommissioningId, setDecommissioningId] = useState<string | null>(
    null,
  );

  // Wizard mode detection
  const isWizardMode = searchParams.get("wizard") === "1";

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch("/admin/departments");
      if (!res.ok) {
        setError(`HTTP ${res.status}`);
        return;
      }
      const data = (await res.json()) as {
        items?: DeptRow[];
        departments?: DeptRow[];
      };
      setRows(data.items ?? data.departments ?? []);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Auto-open create modal in wizard mode
  useEffect(() => {
    if (isWizardMode) {
      setShowCreateModal(true);
    }
  }, [isWizardMode]);

  // Handle modal close with wizard confirmation
  const handleCreateModalClose = useCallback(() => {
    if (isWizardMode) {
      const confirmed = window.confirm(
        "Sihirbaz akışına devam etmek için en az bir departman gerekli - kapatmak istediğinizden emin misiniz?",
      );
      if (!confirmed) {
        return; // Keep modal open
      }
    }
    setShowCreateModal(false);
  }, [isWizardMode]);

  // Handle successful department creation in wizard mode
  const handleDepartmentCreated = useCallback(async () => {
    setShowCreateModal(false);
    await refresh();

    if (isWizardMode) {
      try {
        await apiFetch("/api/v1/setup/add_first_department/complete", {
          method: "POST",
        });
      } catch {
        // Best-effort - wizard page will re-check on mount
      }
      router.push("/?wizard=done");
    }
  }, [isWizardMode, refresh, router]);

  const handleDecommission = useCallback(
    async (deptId: string) => {
      const confirmed = window.confirm(
        `${deptId} departmanını kaldırmak istiyor musunuz?`,
      );
      if (!confirmed) return;

      setDecommissioningId(deptId);
      setError(null);
      try {
        const res = await apiFetch(
          `/admin/departments/${encodeURIComponent(deptId)}`,
          { method: "DELETE" },
        );
        if (!res.ok) {
          const body = await res.text();
          setError(`HTTP ${res.status}: ${body.slice(0, 200)}`);
          return;
        }
        await refresh();
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setDecommissioningId(null);
      }
    },
    [refresh],
  );

  const filtered = rows.filter((d) => {
    if (!query.trim()) return true;
    const q = query.toLowerCase();
    return (
      d.id.toLowerCase().includes(q) ||
      d.display_name.toLowerCase().includes(q) ||
      (d.bot_user ?? "").toLowerCase().includes(q)
    );
  });

  const totalActive = rows.reduce((sum, r) => sum + (r.active_workflows ?? 0), 0);

  return (
    <div className="stack stack--lg">
      <header className="page-header">
        <div className="page-header__title-row">
          <div>
            <h1>Departmanlar</h1>
            <p className="page-header__lede">
              Her departman bot kullanıcısı, bildirim kanalları ve onaylı
              repolar ile ilişkilidir. Bir satıra tıklayarak kimlik
              bilgilerini ve detayları açabilirsiniz.
            </p>
          </div>
          <div className="page-header__actions">
            <button
              type="button"
              className="btn"
              onClick={refresh}
              disabled={loading}
            >
              {loading ? <span className="spinner" /> : "🔄"} Yenile
            </button>
            <button
              type="button"
              className="btn btn--ghost"
              onClick={() => setShowBulkImportModal(true)}
            >
              📦 Toplu içe aktar
            </button>
            <button
              type="button"
              className="btn btn--primary"
              onClick={() => setShowCreateModal(true)}
            >
              + Yeni departman
            </button>
          </div>
        </div>
      </header>

      {isWizardMode && (
        <div className="banner banner--info" role="alert">
          <span className="banner__icon">🧙</span>
          <div className="banner__body">
            <strong>Setup Wizard</strong>
            <div className="text-sm">
              Platformu kullanmaya başlamak için en az bir departman ekleyin.
            </div>
          </div>
        </div>
      )}

      <div className="stat-grid">
        <div className="stat-card">
          <div className="stat-card__label">Toplam departman</div>
          <div className="stat-card__value num">{rows.length}</div>
          <div className="stat-card__delta">Yapılandırılmış kayıt</div>
        </div>
        <div className="stat-card">
          <div className="stat-card__label">Bot ataması</div>
          <div className="stat-card__value num">
            {rows.filter((r) => r.bot_user).length}
            <span className="muted text-sm" style={{ fontWeight: 400 }}> / {rows.length}</span>
          </div>
          <div className="stat-card__delta">Bot kullanıcı tanımlı</div>
        </div>
        <div className="stat-card">
          <div className="stat-card__label">Aktif workflow</div>
          <div className="stat-card__value num">{totalActive}</div>
          <div className="stat-card__delta">Tüm departmanların toplamı</div>
        </div>
      </div>

      {error && (
        <div className="banner banner--danger">
          <span className="banner__icon">⚠️</span>
          <div className="banner__body">{error}</div>
        </div>
      )}

      <div className="card">
        <div className="card__header">
          <div className="card__title">Departman listesi</div>
          <input
            className="input"
            style={{ maxWidth: 280 }}
            type="search"
            placeholder="Ara: id, ad, bot user…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Departman ara"
          />
        </div>

        <div className="card__body card__body--flush">
          {filtered.length === 0 ? (
            <div className="empty">
              <div className="empty__icon">🏢</div>
              <div className="empty__title">
                {rows.length === 0 ? "Henüz departman yok" : "Eşleşme bulunamadı"}
              </div>
              <div className="muted">
                {rows.length === 0
                  ? "Yeni departman düğmesi ile ilk kaydı oluşturabilirsiniz."
                  : "Arama terimini değiştirin."}
              </div>
            </div>
          ) : (
            <div className="table-wrap" style={{ borderRadius: 0, border: 0, boxShadow: "none" }}>
              <table className="table">
                <thead>
                  <tr>
                    <th>ID</th>
                    <th>Ad</th>
                    <th>Bot user</th>
                    <th>Bildirim</th>
                    <th>Kanallar</th>
                    <th className="right">Aktif</th>
                    <th className="right">İşlemler</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((d) => (
                    <tr
                      key={d.id}
                      className="is-clickable"
                      onClick={() =>
                        router.push(`/departments/${encodeURIComponent(d.id)}`)
                      }
                    >
                      <td>
                        <code>{d.id}</code>
                      </td>
                      <td>{d.display_name}</td>
                      <td>
                        {d.bot_user ? (
                          <span className="mono text-sm">{d.bot_user}</span>
                        ) : (
                          <span className="badge badge--warn">
                            <span className="badge__dot" /> atanmamış
                          </span>
                        )}
                      </td>
                      <td>
                        {d.notify_on_success ? (
                          <span className="badge badge--success">on success</span>
                        ) : (
                          <span className="badge">failure-only</span>
                        )}
                      </td>
                      <td className="muted text-sm">
                        {(d.notify_channels ?? []).join(", ") || "-"}
                      </td>
                      <td className="right num">
                        {(d.active_workflows ?? 0) > 0 ? (
                          <span className="badge badge--info">
                            {d.active_workflows}
                          </span>
                        ) : (
                          <span className="muted">0</span>
                        )}
                      </td>
                      <td className="right" onClick={(e) => e.stopPropagation()}>
                        <a
                          className="btn btn--sm btn--ghost"
                          href={`/departments/${encodeURIComponent(d.id)}`}
                        >
                          Düzenle
                        </a>{" "}
                        <button
                          type="button"
                          className="btn btn--sm btn--ghost"
                          style={{ color: "var(--danger-700)" }}
                          disabled={decommissioningId === d.id}
                          onClick={() => void handleDecommission(d.id)}
                        >
                          {decommissioningId === d.id
                            ? "Kaldırılıyor…"
                            : "Kaldır"}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      {showCreateModal && (
        <CreateDepartmentModal
          onClose={handleCreateModalClose}
          onCreated={handleDepartmentCreated}
          wizardMode={true}
        />
      )}

      {showBulkImportModal && (
        <BulkImportModal
          onClose={() => setShowBulkImportModal(false)}
          onImported={() => {
            void refresh();
          }}
        />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Exported page with Suspense boundary for useSearchParams
// ---------------------------------------------------------------------------

export default function DepartmentsPage(): JSX.Element {
  return (
    <Suspense
      fallback={
        <div className="stack">
          <div className="skeleton" style={{ height: 80 }} />
          <div className="skeleton" style={{ height: 240 }} />
        </div>
      }
    >
      <DepartmentsPageInner />
    </Suspense>
  );
}
