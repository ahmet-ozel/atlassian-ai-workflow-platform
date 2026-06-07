"use client";

/**
 * Security panel.
 *
 * Cards: dept connectivity probe artifacts, bot credential rotation
 * banner (TTL countdown per dept), SSH runners, webhook secrets.
 */

import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api-client";
import SSHRunnersCard from "./_components/SSHRunnersCard";
import WebhookSecretsCard from "./_components/WebhookSecretsCard";

type ProbeArtifact = {
  dept_id: string;
  service: string;
  status: string;
  evaluated_at: string;
};

type RotateBannerEntry = {
  dept_id: string;
  service: string;
  rotates_in_days: number;
};

export default function SecurityPage(): JSX.Element {
  const [probes, setProbes] = useState<ProbeArtifact[]>([]);
  const [rotate, setRotate] = useState<RotateBannerEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [probeRes, banner] = await Promise.all([
        apiFetch("/admin/security/probe-artifacts"),
        apiFetch("/admin/security/credential-rotate-banner"),
      ]);
      if (!probeRes.ok) {
        throw new Error(`Probe artifacts HTTP ${probeRes.status}`);
      }
      if (!banner.ok) {
        throw new Error(`Credential rotation HTTP ${banner.status}`);
      }
      const probeBody = (await probeRes.json()) as { items: ProbeArtifact[] };
      const bannerBody = (await banner.json()) as { depts: RotateBannerEntry[] };
      setProbes(probeBody.items ?? []);
      setRotate(bannerBody.depts ?? []);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const probeOk = probes.filter((p) => p.status === "ok" || p.status === "healthy").length;
  const probeFail = probes.length - probeOk;

  return (
    <div className="stack stack--lg">
      <header className="page-header">
        <div className="page-header__title-row">
          <div>
            <h1>Güvenlik</h1>
            <p className="page-header__lede">
              Bağlantı denetimleri, kimlik bilgisi rotasyon hatırlatıcıları,
              SSH runner ve webhook gizli anahtar yönetimi.
            </p>
          </div>
          <div className="page-header__actions">
            <button className="btn" onClick={refresh} disabled={loading}>
              {loading ? <span className="spinner" /> : "🔄"} Yenile
            </button>
          </div>
        </div>
      </header>

      <div className="stat-grid">
        <div className="stat-card">
          <div className="stat-card__label">Sağlıklı probe</div>
          <div className="stat-card__value num">{probeOk}</div>
          <div className="stat-card__delta">{probes.length} toplam denetim</div>
        </div>
        <div className="stat-card">
          <div className="stat-card__label">Hatalı probe</div>
          <div className="stat-card__value num" style={{ color: probeFail > 0 ? "var(--danger-600)" : undefined }}>{probeFail}</div>
          <div className="stat-card__delta">İncelenmesi gereken</div>
        </div>
        <div className="stat-card">
          <div className="stat-card__label">Yaklaşan rotasyon</div>
          <div className="stat-card__value num">{rotate.length}</div>
          <div className="stat-card__delta">30 gün içinde</div>
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
          <div className="card__title">Kimlik bilgisi rotasyonu</div>
        </div>
        <div className="card__body">
          {rotate.length === 0 ? (
            <div className="banner banner--success">
              <span className="banner__icon">✅</span>
              <div className="banner__body">Tüm bot kimlikleri rotasyon penceresi içinde.</div>
            </div>
          ) : (
            <ul className="stack" style={{ margin: 0, padding: 0, listStyle: "none" }}>
              {rotate.map((r, i) => (
                <li key={`${r.dept_id}-${r.service}-${i}`} className="banner banner--warn">
                  <span className="banner__icon">⚠️</span>
                  <div className="banner__body">
                    <strong>{r.dept_id}</strong> / {r.service} -{" "}
                    <strong>{r.rotates_in_days} gün</strong> içinde rotasyon gerekli.
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card__header">
          <div className="card__title">Probe artifacts</div>
          <div className="card__sub">{probes.length} kayıt</div>
        </div>
        <div className="card__body card__body--flush">
          {probes.length === 0 ? (
            <div className="empty">
              <div className="empty__icon">🩺</div>
              <div className="empty__title">Probe kaydı yok</div>
            </div>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Departman</th>
                  <th>Servis</th>
                  <th>Durum</th>
                  <th>Değerlendirme zamanı</th>
                </tr>
              </thead>
              <tbody>
                {probes.map((p, i) => {
                  const isOk = p.status === "ok" || p.status === "healthy";
                  return (
                    <tr key={`${p.dept_id}-${p.service}-${i}`}>
                      <td><code>{p.dept_id}</code></td>
                      <td className="text-sm">{p.service}</td>
                      <td>
                        <span className={`badge ${isOk ? "badge--success" : "badge--danger"}`}>
                          <span className="badge__dot" /> {p.status}
                        </span>
                      </td>
                      <td className="muted text-sm">{p.evaluated_at}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <SSHRunnersCard />
      <WebhookSecretsCard />
    </div>
  );
}
