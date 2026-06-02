"use client";

/**
 * Admin Dashboard Home — Setup Wizard.
 *
 * Guides operators through platform bring-up step-by-step. When the
 * wizard reaches the final step ("add_first_department"), the page
 * redirects to ``/departments?wizard=1`` so the departments page can
 * open the "Yeni Departman Ekle" modal in wizard mode.
 *
 * Requirements: 5.4
 */

import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState, Suspense } from "react";

import { apiFetch } from "@/lib/api-client";
import { getStreamlitUrl } from "@/lib/config";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type StepInfo = {
  name: string;
  status: "pending" | "completed" | "failed";
  config_data: Record<string, unknown> | null;
  error: string | null;
};

type SetupStatus = {
  steps: StepInfo[];
  current_step: string | null;
  all_complete: boolean;
};

// ---------------------------------------------------------------------------
// Step copy
// ---------------------------------------------------------------------------

const STEP_META: Record<string, { label: string; hint: string }> = {
  vault: {
    label: "Vault",
    hint: "Sırlar için merkezi depolama hazır mı?",
  },
  postgresql: {
    label: "PostgreSQL",
    hint: "Birincil veritabanı bağlantısı.",
  },
  temporal: {
    label: "Temporal",
    hint: "Workflow orkestrasyonu ayağa kalksın.",
  },
  mcp_server: {
    label: "MCP Server",
    hint: "Atlassian araç köprüsü ulaşılabilir.",
  },
  workers: {
    label: "Workers",
    hint: "Temporal worker'ları çalışıyor.",
  },
  services: {
    label: "Servisler",
    hint: "Yardımcı servisler hazır.",
  },
  add_first_department: {
    label: "İlk Departman",
    hint: "Bir departman ekleyerek platformu kullanmaya başlayın.",
  },
};

// ---------------------------------------------------------------------------
// Inner component (uses useSearchParams)
// ---------------------------------------------------------------------------

function HomePageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [status, setStatus] = useState<SetupStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Check if wizard is done (redirected back from departments page)
  const wizardDone = searchParams.get("wizard") === "done";

  const fetchStatus = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch("/api/v1/setup/status");
      if (!res.ok) {
        setError(`HTTP ${res.status}`);
        return;
      }
      const data = (await res.json()) as SetupStatus;
      setStatus(data);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchStatus();
  }, [fetchStatus]);

  // When current step is "add_first_department", redirect to departments page
  // in wizard mode.
  useEffect(() => {
    if (!status) return;
    if (status.current_step === "add_first_department" && !wizardDone) {
      router.push("/departments?wizard=1");
    }
  }, [status, router, wizardDone]);

  // Handler for completing a step manually (for non-department steps)
  const handleCompleteStep = useCallback(
    async (stepName: string) => {
      try {
        const res = await apiFetch(`/api/v1/setup/${stepName}/complete`, {
          method: "POST",
        });
        if (res.ok) {
          await fetchStatus();
        }
      } catch {
        // Silently fail — user can retry
      }
    },
    [fetchStatus],
  );

  // ---------------------------------------------------------------------------
  // Wizard completed screen
  // ---------------------------------------------------------------------------

  if (wizardDone || status?.all_complete) {
    return (
      <div className="stack stack--lg">
        <section className="hero">
          <h1>✅ Kurulum tamamlandı</h1>
          <p>
            Platform hazır. End-user arayüzünü açarak ekipler için ilk
            görevi başlatabilir veya servisleri yönetmeye geçebilirsiniz.
          </p>
          <div className="hero__actions">
            <a
              className="btn btn--lg"
              href={getStreamlitUrl()}
              target="_blank"
              rel="noopener noreferrer"
            >
              🚀 Streamlit&apos;i aç
            </a>
            <button
              type="button"
              className="btn btn--ghost btn--lg"
              onClick={() => router.push("/services")}
            >
              🧩 Servisleri yönet
            </button>
          </div>
        </section>

        <div className="stat-grid">
          <a href="/departments" className="stat-card card--hover" style={{ textDecoration: "none" }}>
            <div className="stat-card__label">Departmanlar</div>
            <div className="stat-card__value">🏢</div>
            <div className="stat-card__delta">Bot kullanıcı ve kanal yapılandırması</div>
          </a>
          <a href="/operations" className="stat-card card--hover" style={{ textDecoration: "none" }}>
            <div className="stat-card__label">Operasyonlar</div>
            <div className="stat-card__value">⚡</div>
            <div className="stat-card__delta">Runner kuyruğu ve canlı durum</div>
          </a>
          <a href="/costs" className="stat-card card--hover" style={{ textDecoration: "none" }}>
            <div className="stat-card__label">Maliyetler</div>
            <div className="stat-card__value">💰</div>
            <div className="stat-card__delta">Bütçe alarmları ve trend</div>
          </a>
        </div>
      </div>
    );
  }

  // ---------------------------------------------------------------------------
  // Loading / Error states
  // ---------------------------------------------------------------------------

  if (loading) {
    return (
      <div className="stack stack--lg">
        <section className="hero">
          <h1>Kurulum sihirbazı</h1>
          <p>Durum yükleniyor…</p>
        </section>
        <div className="card">
          <div className="card__body">
            <div className="stack">
              <div className="skeleton" style={{ width: "60%" }} />
              <div className="skeleton" style={{ width: "80%" }} />
              <div className="skeleton" style={{ width: "70%" }} />
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="stack stack--lg">
        <section className="hero">
          <h1>Kurulum sihirbazı</h1>
          <p>Adımlar okunamadı.</p>
        </section>
        <div className="banner banner--danger">
          <span className="banner__icon">⚠️</span>
          <div className="banner__body">
            <strong>Bağlantı hatası</strong>
            <div className="text-sm">{error}</div>
          </div>
          <button type="button" className="btn btn--sm" onClick={fetchStatus}>
            Tekrar dene
          </button>
        </div>
      </div>
    );
  }

  // ---------------------------------------------------------------------------
  // Wizard step list
  // ---------------------------------------------------------------------------

  const total = status?.steps.length ?? 0;
  const done = status?.steps.filter((s) => s.status === "completed").length ?? 0;
  const progressPct = total > 0 ? Math.round((done / total) * 100) : 0;

  return (
    <div className="stack stack--lg">
      <section className="hero">
        <h1>Platforma hoş geldin 👋</h1>
        <p>
          Kullanmaya başlamak için aşağıdaki adımları sırayla tamamlayın.
          Her adım bağımsız çalışır; istediğiniz zaman yenileyip devam
          edebilirsiniz.
        </p>
        <div className="hero__actions">
          <span className="badge badge--brand" style={{ background: "rgba(255,255,255,0.94)", color: "#4338ca", borderColor: "transparent" }}>
            <span className="badge__dot" /> {done}/{total} adım tamam · %{progressPct}
          </span>
        </div>
      </section>

      <div className="card">
        <div className="card__header">
          <div>
            <div className="card__title">Kurulum adımları</div>
            <div className="card__sub">Sıradaki adım vurgulanır.</div>
          </div>
          <button type="button" className="btn btn--sm btn--ghost" onClick={fetchStatus}>
            🔄 Yenile
          </button>
        </div>
        <div className="card__body">
          <ol className="steps">
            {status?.steps.map((step, idx) => {
              const isCurrent = step.name === status.current_step;
              const isDone = step.status === "completed";
              const isFailed = step.status === "failed";
              const meta = STEP_META[step.name] ?? { label: step.name, hint: "" };

              return (
                <li
                  key={step.name}
                  className={`step${isCurrent ? " is-current" : ""}${
                    isDone ? " is-done" : ""
                  }${isFailed ? " is-failed" : ""}`}
                >
                  <span className="step__indicator">
                    {isDone ? "✓" : isFailed ? "!" : idx + 1}
                  </span>
                  <div className="step__body">
                    <span className="step__title">{meta.label}</span>
                    <span className="step__hint">{meta.hint}</span>
                    {isFailed && step.error && (
                      <span className="text-xs" style={{ color: "var(--danger-700)" }}>
                        {step.error}
                      </span>
                    )}
                  </div>
                  {isCurrent && step.name !== "add_first_department" && (
                    <button
                      type="button"
                      className="btn btn--primary btn--sm"
                      onClick={() => handleCompleteStep(step.name)}
                    >
                      Tamamla
                    </button>
                  )}
                  {isDone && (
                    <span className="badge badge--success">
                      <span className="badge__dot" /> tamam
                    </span>
                  )}
                </li>
              );
            })}
          </ol>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Exported page with Suspense boundary for useSearchParams
// ---------------------------------------------------------------------------

export default function HomePage() {
  return (
    <Suspense
      fallback={
        <div className="stack">
          <div className="skeleton" style={{ height: 120 }} />
          <div className="skeleton" style={{ height: 60 }} />
        </div>
      }
    >
      <HomePageInner />
    </Suspense>
  );
}
