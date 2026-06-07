"use client";

/**
 * Costs panel.
 *
 * Three cards (dept totals, by model, trend) + "Bütçe Alarmları" tab
 * for configuring per-dept alarm thresholds.
 */

import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api-client";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type DeptResp = {
  dept_id: string;
  total_usd: string;
  by_user: { user_id: string | null; usd: string }[];
};

type ModelResp = {
  by_model: { model: string; usd: string; row_count: number }[];
};

type TrendResp = {
  trend: { day: string; usd: string }[];
};

type AlarmThreshold = {
  period: "weekly" | "monthly";
  scope: "user" | "dept";
  threshold_pct: number;
  notify_channel: "slack" | "email" | "teams";
  last_alarmed_at: string | null;
};

type AlarmThresholdsResp = {
  thresholds: AlarmThreshold[];
};

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------

type Tab = "overview" | "alarms";

export default function CostsPage(): JSX.Element {
  const [tab, setTab] = useState<Tab>("overview");

  return (
    <div className="stack stack--lg">
      <header className="page-header">
        <div className="page-header__title-row">
          <div>
            <h1>Maliyetler</h1>
            <p className="page-header__lede">
              LLM kullanımının departman, model ve gün bazında dağılımı.
              Bütçe alarmları ile ön-uyarı tetikleyebilirsiniz.
            </p>
          </div>
        </div>
      </header>

      <div className="tabs">
        <button
          className={`tab${tab === "overview" ? " is-active" : ""}`}
          onClick={() => setTab("overview")}
        >
          📊 Genel bakış
        </button>
        <button
          className={`tab${tab === "alarms" ? " is-active" : ""}`}
          onClick={() => setTab("alarms")}
        >
          🔔 Bütçe alarmları
        </button>
      </div>

      {tab === "overview" && <CostsOverviewTab />}
      {tab === "alarms" && <BudgetAlarmsTab />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Costs Overview Tab
// ---------------------------------------------------------------------------

function CostsOverviewTab(): JSX.Element {
  const [deptId, setDeptId] = useState<string>("test");
  const [dept, setDept] = useState<DeptResp | null>(null);
  const [model, setModel] = useState<ModelResp | null>(null);
  const [trend, setTrend] = useState<TrendResp | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [dRes, mRes, tRes] = await Promise.all([
        apiFetch(`/admin/costs/dept/${encodeURIComponent(deptId)}`),
        apiFetch("/admin/costs/model"),
        apiFetch("/admin/costs/trend"),
      ]);
      if (!dRes.ok || !mRes.ok || !tRes.ok) {
        throw new Error("Failed to fetch cost data");
      }
      const d: DeptResp = await dRes.json();
      const m: ModelResp = await mRes.json();
      const t: TrendResp = await tRes.json();
      setDept(d);
      setModel(m);
      setTrend(t);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [deptId]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const maxTrend = trend ? Math.max(0.01, ...trend.trend.map((d) => parseFloat(d.usd))) : 1;

  return (
    <div className="stack stack--lg">
      <div className="card">
        <div className="card__header">
          <div className="card__title">Son 30 gün</div>
          <div className="row">
            <label className="muted text-sm">Departman</label>
            <input
              className="input"
              style={{ width: 180 }}
              value={deptId}
              onChange={(e) => setDeptId(e.target.value)}
            />
            <button className="btn btn--primary btn--sm" onClick={refresh} disabled={loading}>
              {loading ? <span className="spinner" /> : "Yükle"}
            </button>
          </div>
        </div>
        {error && (
          <div className="banner banner--danger" style={{ margin: "1rem" }}>
            <span className="banner__icon">⚠️</span>
            <div className="banner__body">{error}</div>
          </div>
        )}
      </div>

      <div className="grid-2">
        <div className="stat-card">
          <div className="stat-card__label">Departman toplamı</div>
          <div className="stat-card__value num">${dept?.total_usd ?? "0.00"}</div>
          <div className="stat-card__delta">{dept?.dept_id ?? "-"}</div>
        </div>
        <div className="stat-card">
          <div className="stat-card__label">Kullanıcı sayısı</div>
          <div className="stat-card__value num">{dept?.by_user.length ?? 0}</div>
          <div className="stat-card__delta">Maliyet üreten</div>
        </div>
      </div>

      <div className="grid-2">
        <div className="card">
          <div className="card__header">
            <div className="card__title">Kullanıcıya göre</div>
          </div>
          <div className="card__body card__body--flush">
            {!dept || dept.by_user.length === 0 ? (
              <div className="empty">
                <div className="empty__icon">👤</div>
                <div className="empty__title">Kayıt yok</div>
              </div>
            ) : (
              <table className="table">
                <thead>
                  <tr>
                    <th>Kullanıcı</th>
                    <th className="right">USD</th>
                  </tr>
                </thead>
                <tbody>
                  {dept.by_user.map((u, i) => (
                    <tr key={`${u.user_id ?? "system"}-${i}`}>
                      <td className="mono text-sm">{u.user_id ?? "system"}</td>
                      <td className="right num">${u.usd}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>

        <div className="card">
          <div className="card__header">
            <div className="card__title">Modele göre</div>
          </div>
          <div className="card__body card__body--flush">
            {!model || model.by_model.length === 0 ? (
              <div className="empty">
                <div className="empty__icon">🧠</div>
                <div className="empty__title">Kayıt yok</div>
              </div>
            ) : (
              <table className="table">
                <thead>
                  <tr>
                    <th>Model</th>
                    <th className="right">USD</th>
                    <th className="right">Çağrı</th>
                  </tr>
                </thead>
                <tbody>
                  {model.by_model.map((m, i) => (
                    <tr key={`${m.model}-${i}`}>
                      <td className="mono text-sm">{m.model}</td>
                      <td className="right num">${m.usd}</td>
                      <td className="right num muted">{m.row_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card__header">
          <div className="card__title">Günlük trend</div>
          <div className="muted text-sm">Son 30 gün</div>
        </div>
        <div className="card__body">
          {!trend || trend.trend.length === 0 ? (
            <div className="empty">
              <div className="empty__icon">📈</div>
              <div className="empty__title">Veri yok</div>
            </div>
          ) : (
            <div style={{ display: "flex", alignItems: "flex-end", gap: 4, height: 160 }}>
              {trend.trend.map((d) => {
                const v = parseFloat(d.usd);
                const h = Math.max(2, Math.round((v / maxTrend) * 140));
                return (
                  <div
                    key={d.day}
                    title={`${d.day}: $${d.usd}`}
                    style={{
                      flex: 1,
                      height: h,
                      background: "linear-gradient(180deg, var(--brand-500), var(--brand-700))",
                      borderRadius: "4px 4px 0 0",
                      transition: "transform 200ms",
                      cursor: "default",
                    }}
                  />
                );
              })}
            </div>
          )}
        </div>
        {trend && trend.trend.length > 0 && (
          <div className="card__footer">
            <span className="muted text-sm">{trend.trend[0].day}</span>
            <span className="muted text-sm">{trend.trend[trend.trend.length - 1].day}</span>
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Budget Alarms Tab
// ---------------------------------------------------------------------------

const PERIODS: AlarmThreshold["period"][] = ["weekly", "monthly"];
const SCOPES: AlarmThreshold["scope"][] = ["user", "dept"];
const CHANNELS: AlarmThreshold["notify_channel"][] = ["slack", "email", "teams"];

const PERIOD_LABELS: Record<AlarmThreshold["period"], string> = {
  weekly: "Haftalık",
  monthly: "Aylık",
};
const SCOPE_LABELS: Record<AlarmThreshold["scope"], string> = {
  user: "Kullanıcı",
  dept: "Departman",
};

function BudgetAlarmsTab(): JSX.Element {
  const [deptId, setDeptId] = useState<string>("payment");
  const [thresholds, setThresholds] = useState<AlarmThreshold[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveSuccess, setSaveSuccess] = useState(false);

  const buildDefaultMatrix = useCallback((): AlarmThreshold[] => {
    const defaults: AlarmThreshold[] = [];
    for (const period of PERIODS) {
      for (const scope of SCOPES) {
        defaults.push({
          period,
          scope,
          threshold_pct: 70,
          notify_channel: "slack",
          last_alarmed_at: null,
        });
      }
    }
    return defaults;
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    setSaveSuccess(false);
    try {
      const res = await apiFetch(
        `/admin/costs/alarm-thresholds?dept_id=${encodeURIComponent(deptId)}`,
      );
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const data: AlarmThresholdsResp = await res.json();
      const matrix = buildDefaultMatrix();
      for (const fetched of data.thresholds) {
        const idx = matrix.findIndex(
          (m) => m.period === fetched.period && m.scope === fetched.scope,
        );
        if (idx >= 0) {
          matrix[idx] = fetched;
        }
      }
      setThresholds(matrix);
    } catch (err) {
      setError((err as Error).message);
      setThresholds(buildDefaultMatrix());
    } finally {
      setLoading(false);
    }
  }, [deptId, buildDefaultMatrix]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleThresholdChange = (
    period: AlarmThreshold["period"],
    scope: AlarmThreshold["scope"],
    value: number,
  ) => {
    setThresholds((prev) =>
      prev.map((t) =>
        t.period === period && t.scope === scope
          ? { ...t, threshold_pct: value }
          : t,
      ),
    );
    setSaveSuccess(false);
  };

  const handleChannelChange = (
    period: AlarmThreshold["period"],
    scope: AlarmThreshold["scope"],
    channel: AlarmThreshold["notify_channel"],
  ) => {
    setThresholds((prev) =>
      prev.map((t) =>
        t.period === period && t.scope === scope
          ? { ...t, notify_channel: channel }
          : t,
      ),
    );
    setSaveSuccess(false);
  };

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    setSaveSuccess(false);
    try {
      const res = await apiFetch(
        `/admin/costs/alarm-thresholds/${encodeURIComponent(deptId)}`,
        {
          method: "PUT",
          body: JSON.stringify({ thresholds }),
        },
      );
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        throw new Error(text || `HTTP ${res.status}`);
      }
      setSaveSuccess(true);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="stack stack--lg">
      <div className="banner banner--info">
        <span className="banner__icon">ℹ️</span>
        <div className="banner__body">
          Departman bütçesinin belirlenen yüzdesine ulaşıldığında ön-uyarı
          gönderilir. Hard limit (%100) aşıldığında workflow başlatma
          reddedilir.
        </div>
      </div>

      <div className="card">
        <div className="card__header">
          <div className="card__title">Alarm eşikleri</div>
          <div className="row">
            <label className="muted text-sm">Departman</label>
            <input
              className="input"
              style={{ width: 180 }}
              value={deptId}
              onChange={(e) => setDeptId(e.target.value)}
              aria-label="Departman ID"
            />
            <button className="btn btn--sm" onClick={refresh} disabled={loading}>
              {loading ? <span className="spinner" /> : "Yükle"}
            </button>
          </div>
        </div>

        <div className="card__body card__body--flush">
          {error && (
            <div className="banner banner--danger" style={{ margin: "1rem" }}>
              <span className="banner__icon">⚠️</span>
              <div className="banner__body">{error}</div>
            </div>
          )}
          {saveSuccess && (
            <div className="banner banner--success" style={{ margin: "1rem" }}>
              <span className="banner__icon">✅</span>
              <div className="banner__body">Eşikler kaydedildi.</div>
            </div>
          )}

          <table className="table">
            <thead>
              <tr>
                <th>Periyot</th>
                <th>Kapsam</th>
                <th>Eşik</th>
                <th>Bildirim kanalı</th>
                <th>Son alarm</th>
              </tr>
            </thead>
            <tbody>
              {thresholds.map((t) => (
                <tr key={`${t.period}-${t.scope}`}>
                  <td>{PERIOD_LABELS[t.period]}</td>
                  <td>{SCOPE_LABELS[t.scope]}</td>
                  <td>
                    <div className="row">
                      <input
                        type="range"
                        min={1}
                        max={99}
                        value={t.threshold_pct}
                        onChange={(e) =>
                          handleThresholdChange(
                            t.period,
                            t.scope,
                            Number(e.target.value),
                          )
                        }
                        style={{ width: 160 }}
                        aria-label={`Eşik yüzdesi - ${PERIOD_LABELS[t.period]} ${SCOPE_LABELS[t.scope]}`}
                      />
                      <span className="badge badge--brand num" style={{ minWidth: 50, justifyContent: "center" }}>
                        %{t.threshold_pct}
                      </span>
                    </div>
                  </td>
                  <td>
                    <select
                      className="select"
                      style={{ width: 140 }}
                      value={t.notify_channel}
                      onChange={(e) =>
                        handleChannelChange(
                          t.period,
                          t.scope,
                          e.target.value as AlarmThreshold["notify_channel"],
                        )
                      }
                    >
                      {CHANNELS.map((ch) => (
                        <option key={ch} value={ch}>
                          {ch.charAt(0).toUpperCase() + ch.slice(1)}
                        </option>
                      ))}
                    </select>
                  </td>
                  <td className="muted text-sm">
                    {t.last_alarmed_at
                      ? new Date(t.last_alarmed_at).toLocaleString("tr-TR")
                      : "-"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="card__footer">
          <span className="muted text-sm">
            Değişiklikler kaydedilene dek uygulanmaz.
          </span>
          <button
            className="btn btn--primary"
            onClick={handleSave}
            disabled={saving || loading}
          >
            {saving ? <span className="spinner" /> : "💾"} Kaydet
          </button>
        </div>
      </div>
    </div>
  );
}
