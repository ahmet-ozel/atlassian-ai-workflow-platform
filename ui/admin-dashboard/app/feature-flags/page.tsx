"use client";

/**
 * Feature flags panel.
 *
 * Supports listing and toggling runtime feature flags.
 *
 * Lists every flag in `shared.feature_flags` with global value and
 * per-dept overrides. Each toggle opens a 5-second countdown confirm
 * dialog; mutations target the v1 surface (`/api/v1/feature-flags`).
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { apiFetch } from "@/lib/api-client";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type Flag = {
  key: string;
  global_value: boolean;
  default_value: boolean;
  description: string;
  impact_note: string;
  updated_by: string | null;
  updated_at: string | null;
  dept_overrides: Record<string, boolean>;
};

type Department = {
  id: string;
  display_name?: string;
};

type ToggleAction =
  | { kind: "global"; flag: Flag; nextValue: boolean }
  | { kind: "dept-set"; flag: Flag; deptId: string; nextValue: boolean }
  | { kind: "dept-remove"; flag: Flag; deptId: string };

const CONFIRM_SECONDS = 5;

function describeAction(action: ToggleAction): string {
  switch (action.kind) {
    case "global":
      return `Global "${action.flag.key}" → ${
        action.nextValue ? "AÇIK" : "KAPALI"
      }`;
    case "dept-set":
      return `"${action.deptId}" departman override "${action.flag.key}" → ${
        action.nextValue ? "AÇIK" : "KAPALI"
      }`;
    case "dept-remove":
      return `"${action.deptId}" departman override "${action.flag.key}" kaldırıldı (global değere döner)`;
  }
}

// ---------------------------------------------------------------------------
// Confirm dialog
// ---------------------------------------------------------------------------

interface ConfirmDialogProps {
  action: ToggleAction;
  onConfirm: () => void;
  onCancel: () => void;
}

function ConfirmDialog({
  action,
  onConfirm,
  onCancel,
}: ConfirmDialogProps): JSX.Element {
  const [secondsLeft, setSecondsLeft] = useState<number>(CONFIRM_SECONDS);

  useEffect(() => {
    if (secondsLeft <= 0) {
      return;
    }
    const handle = window.setTimeout(() => {
      setSecondsLeft((prev) => Math.max(0, prev - 1));
    }, 1000);
    return () => window.clearTimeout(handle);
  }, [secondsLeft]);

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true" aria-labelledby="ff-confirm-title">
      <div className="modal" style={{ padding: "1.5rem" }}>
        <h2 id="ff-confirm-title" style={{ marginBottom: "0.5rem" }}>Değişikliği onayla</h2>
        <p>{describeAction(action)}</p>
        <p className="muted text-sm" style={{ marginTop: "0.75rem" }}>
          Yanlışlıkla tıkladıysanız {CONFIRM_SECONDS} saniye içinde iptal edin.
          Geri sayım sıfırlandığında veya "Şimdi uygula" düğmesine basıldığında
          değişiklik anlık uygulanır.
        </p>
        <div className="row--between" style={{ marginTop: "1.25rem" }}>
          <span className="muted text-sm" aria-live="polite">
            {secondsLeft > 0 ? `Otomatik uygulama: ${secondsLeft} sn` : "Hazır"}
          </span>
          <span className="row">
            <button className="btn" onClick={onCancel} type="button">İptal</button>
            <button className="btn btn--primary" onClick={onConfirm} type="button">Şimdi uygula</button>
          </span>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function FeatureFlagsPage(): JSX.Element {
  const [flags, setFlags] = useState<Flag[]>([]);
  const [departments, setDepartments] = useState<Department[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [pendingAction, setPendingAction] = useState<ToggleAction | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [query, setQuery] = useState("");

  const autoApplyRef = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [flagsRes, deptsRes] = await Promise.all([
        apiFetch("/api/v1/feature-flags"),
        apiFetch("/admin/departments"),
      ]);
      if (!flagsRes.ok) {
        const text = await flagsRes.text().catch(() => "");
        throw new Error(`feature flags request failed: ${flagsRes.status} ${text}`);
      }
      const flagsBody = (await flagsRes.json()) as { flags?: Flag[] };
      setFlags(flagsBody.flags ?? []);

      if (deptsRes.ok) {
        const deptsBody = (await deptsRes.json()) as { departments?: Department[]; items?: Department[] };
        setDepartments(deptsBody.departments ?? deptsBody.items ?? []);
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const cancelPending = useCallback(() => {
    if (autoApplyRef.current !== null) {
      window.clearTimeout(autoApplyRef.current);
      autoApplyRef.current = null;
    }
    setPendingAction(null);
  }, []);

  const applyAction = useCallback(
    async (action: ToggleAction) => {
      cancelPending();
      setBusyKey(action.flag.key);
      setError(null);
      try {
        if (action.kind === "global") {
          const res = await apiFetch(
            `/api/v1/feature-flags/${encodeURIComponent(action.flag.key)}`,
            {
              method: "PATCH",
              body: JSON.stringify({ value: action.nextValue }),
            },
          );
          if (!res.ok) {
            const text = await res.text().catch(() => "");
            throw new Error(`PATCH failed: ${res.status} ${text}`);
          }
        } else if (action.kind === "dept-set") {
          const res = await apiFetch(
            `/api/v1/feature-flags/${encodeURIComponent(action.flag.key)}`,
            {
              method: "PATCH",
              body: JSON.stringify({ value: action.nextValue, dept_id: action.deptId }),
            },
          );
          if (!res.ok) {
            const text = await res.text().catch(() => "");
            throw new Error(`PATCH failed: ${res.status} ${text}`);
          }
        } else {
          const res = await apiFetch(
            `/api/v1/feature-flags/${encodeURIComponent(action.flag.key)}/overrides/${encodeURIComponent(action.deptId)}`,
            { method: "DELETE" },
          );
          if (!res.ok) {
            const text = await res.text().catch(() => "");
            throw new Error(`DELETE failed: ${res.status} ${text}`);
          }
        }
        await refresh();
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setBusyKey(null);
      }
    },
    [cancelPending, refresh],
  );

  useEffect(() => {
    if (pendingAction === null) return;
    if (autoApplyRef.current !== null) {
      window.clearTimeout(autoApplyRef.current);
    }
    autoApplyRef.current = window.setTimeout(() => {
      autoApplyRef.current = null;
      void applyAction(pendingAction);
    }, CONFIRM_SECONDS * 1000);
    return () => {
      if (autoApplyRef.current !== null) {
        window.clearTimeout(autoApplyRef.current);
        autoApplyRef.current = null;
      }
    };
  }, [pendingAction, applyAction]);

  const queueGlobalToggle = (flag: Flag) =>
    setPendingAction({ kind: "global", flag, nextValue: !flag.global_value });

  const queueDeptOverrideAdd = (flag: Flag) => {
    if (departments.length === 0) {
      setError("Departman listesi yüklenemedi.");
      return;
    }
    const existingOverrides = new Set(Object.keys(flag.dept_overrides));
    const candidate = departments.find((d) => !existingOverrides.has(d.id));
    if (candidate === undefined) {
      setError("Tüm departmanlar için zaten override mevcut.");
      return;
    }
    const promptInput = window.prompt(
      `"${flag.key}" için override eklenecek departman id (varsayılan: ${candidate.id}):`,
      candidate.id,
    );
    if (promptInput === null) return;
    const deptId = promptInput.trim();
    if (deptId.length === 0) {
      setError("Departman id zorunlu.");
      return;
    }
    if (!departments.some((d) => d.id === deptId)) {
      setError(`Bilinmeyen departman: ${deptId}`);
      return;
    }
    setPendingAction({ kind: "dept-set", flag, deptId, nextValue: !flag.global_value });
  };

  const queueDeptOverrideToggle = (flag: Flag, deptId: string, current: boolean) =>
    setPendingAction({ kind: "dept-set", flag, deptId, nextValue: !current });

  const queueDeptOverrideRemove = (flag: Flag, deptId: string) =>
    setPendingAction({ kind: "dept-remove", flag, deptId });

  const sortedFlags = useMemo(() => {
    const list = query
      ? flags.filter((f) => f.key.toLowerCase().includes(query.toLowerCase()))
      : flags;
    return [...list].sort((a, b) => a.key.localeCompare(b.key));
  }, [flags, query]);

  return (
    <div className="stack stack--lg">
      <header className="page-header">
        <div className="page-header__title-row">
          <div>
            <h1>Feature flags</h1>
            <p className="page-header__lede">
              Her toggle {CONFIRM_SECONDS} saniyelik geri sayımlı onay açar.
              Departman override&apos;ları global değere göre öncelik kazanır.
            </p>
          </div>
          <div className="page-header__actions">
            <button className="btn" onClick={refresh} disabled={loading}>
              {loading ? <span className="spinner" /> : "🔄"} Yenile
            </button>
          </div>
        </div>
      </header>

      {error && (
        <div className="banner banner--danger" role="alert">
          <span className="banner__icon">⚠️</span>
          <div className="banner__body">{error}</div>
        </div>
      )}

      <div className="card">
        <div className="card__header">
          <div className="card__title">Bayraklar</div>
          <input
            className="input"
            style={{ maxWidth: 280 }}
            type="search"
            placeholder="Bayrak adı ara…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>

        <div className="card__body card__body--flush">
          {sortedFlags.length === 0 ? (
            <div className="empty">
              <div className="empty__icon">🚩</div>
              <div className="empty__title">Bayrak yok</div>
            </div>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Bayrak</th>
                  <th>Global</th>
                  <th>Departman override</th>
                  <th>Etki</th>
                  <th>Güncellenme</th>
                </tr>
              </thead>
              <tbody>
                {sortedFlags.map((flag) => {
                  const overrideEntries = Object.entries(flag.dept_overrides);
                  return (
                    <tr key={flag.key}>
                      <td>
                        <code>{flag.key}</code>
                        {flag.description && (
                          <div className="muted text-xs">{flag.description}</div>
                        )}
                        <div className="text-xs faint">
                          default: {flag.default_value ? "on" : "off"}
                        </div>
                      </td>
                      <td>
                        <button
                          type="button"
                          className={`btn btn--sm ${flag.global_value ? "btn--success" : ""}`}
                          disabled={busyKey === flag.key}
                          onClick={() => queueGlobalToggle(flag)}
                          aria-label={`${flag.key} global değerini değiştir`}
                        >
                          {flag.global_value ? "✅ AÇIK" : "⛔ KAPALI"}
                        </button>
                      </td>
                      <td>
                        {overrideEntries.length === 0 ? (
                          <span className="muted text-sm">(yok)</span>
                        ) : (
                          <ul style={{ margin: 0, padding: 0, listStyle: "none" }} className="stack" >
                            {overrideEntries.map(([deptId, value]) => (
                              <li key={deptId} className="row" style={{ gap: 6 }}>
                                <code className="text-sm" style={{ minWidth: "8rem" }}>{deptId}</code>
                                <button
                                  type="button"
                                  className="btn btn--sm"
                                  disabled={busyKey === flag.key}
                                  onClick={() => queueDeptOverrideToggle(flag, deptId, value)}
                                >
                                  {value ? "✅" : "⛔"}
                                </button>
                                <button
                                  type="button"
                                  className="btn btn--sm btn--ghost"
                                  disabled={busyKey === flag.key}
                                  onClick={() => queueDeptOverrideRemove(flag, deptId)}
                                >
                                  Kaldır
                                </button>
                              </li>
                            ))}
                          </ul>
                        )}
                        <button
                          type="button"
                          className="btn btn--sm btn--ghost"
                          disabled={busyKey === flag.key}
                          onClick={() => queueDeptOverrideAdd(flag)}
                          style={{ marginTop: 6 }}
                        >
                          + Override ekle
                        </button>
                      </td>
                      <td className="muted text-sm" style={{ maxWidth: 320 }}>
                        {flag.impact_note}
                      </td>
                      <td className="muted text-xs">
                        {flag.updated_at
                          ? `${flag.updated_at} · ${flag.updated_by ?? "-"}`
                          : "-"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>

      {pendingAction !== null && (
        <ConfirmDialog
          action={pendingAction}
          onConfirm={() => void applyAction(pendingAction)}
          onCancel={cancelPending}
        />
      )}
    </div>
  );
}
