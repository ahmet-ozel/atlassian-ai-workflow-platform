"use client";

import { useCallback, useState } from "react";

import { apiFetch } from "@/lib/api-client";

type SmokeKind = "bitbucket" | "confluence" | "jira" | "automation-e2e";

type SmokeResult = {
  status: string;
  dept_id: string;
  duration_ms?: number;
  steps?: Array<Record<string, unknown>>;
  cleanup?: Array<Record<string, unknown>>;
  [key: string]: unknown;
};

const TESTS: Array<{
  id: SmokeKind;
  title: string;
  description: string;
  acceptance: string;
}> = [
  {
    id: "bitbucket",
    title: "Bitbucket write",
    description: "Test repo üzerinde branch, commit ve PR oluşturur; sonra PR'i decline edip branch'i siler.",
    acceptance: "Commit/PR gerçek gider, rollback yolu çalışır.",
  },
  {
    id: "confluence",
    title: "Confluence publish",
    description: "Test page oluşturur, update eder ve cleanup ile siler.",
    acceptance: "Page create/update gerçek ortamda başarılıdır.",
  },
  {
    id: "jira",
    title: "Jira write",
    description: "Geçici issue oluşturur, comment ve MD attachment ekler, status transition dener ve cleanup yapar.",
    acceptance: "Comment/attachment/status update gerçek ortamda çalışır.",
  },
  {
    id: "automation-e2e",
    title: "Automation E2E",
    description: "Jira task atamasindan baslayip webhook, LLM karari, SSH/Docker, Bitbucket, Confluence ve Jira sonucunu tek trace ile kosturur.",
    acceptance: "Jira task -> automation trigger -> LLM karar -> SSH/Docker -> BB/Conf/Jira zinciri gecer.",
  },
];

async function readError(res: Response): Promise<string> {
  try {
    const body = await res.json();
    if (typeof body.detail === "string") return body.detail;
    return JSON.stringify(body);
  } catch {
    return res.text();
  }
}

export default function LiveSmokePage(): JSX.Element {
  const [deptId, setDeptId] = useState("test");
  const [running, setRunning] = useState<SmokeKind | null>(null);
  const [results, setResults] = useState<Partial<Record<SmokeKind, SmokeResult>>>({});
  const [errors, setErrors] = useState<Partial<Record<SmokeKind, string>>>({});

  const run = useCallback(async (kind: SmokeKind) => {
    setRunning(kind);
    setErrors((prev) => ({ ...prev, [kind]: undefined }));
    try {
      const res = await apiFetch(
        `/api/v1/live-smoke/${encodeURIComponent(deptId)}/${kind}`,
        { method: "POST", body: JSON.stringify({}) },
      );
      if (!res.ok) throw new Error(await readError(res));
      const body = (await res.json()) as SmokeResult;
      setResults((prev) => ({ ...prev, [kind]: body }));
    } catch (err) {
      setErrors((prev) => ({
        ...prev,
        [kind]: err instanceof Error ? err.message : String(err),
      }));
    } finally {
      setRunning(null);
    }
  }, [deptId]);

  return (
    <div className="stack stack--lg">
      <header className="page-header">
        <div className="page-header__title-row">
          <div>
            <h1>Live smoke tests</h1>
            <p className="page-header__lede">
              Vault credential ref'leriyle gerçek ortamda mutating Atlassian
              kabul testleri. Normal kullanıcı ekranlarına yazma aracı eklemez.
            </p>
          </div>
        </div>
      </header>

      <div className="card">
        <div className="card__body row" style={{ alignItems: "end", gap: "1rem" }}>
          <label className="stack" style={{ gap: 6, maxWidth: 280 }}>
            <span className="text-sm muted">Department</span>
            <input
              className="input"
              value={deptId}
              onChange={(ev) => setDeptId(ev.target.value)}
              aria-label="Department id"
            />
          </label>
          <span className="muted text-sm">
            Bu ekranda hardcode credential yok; backend department config'teki
            Vault ref'i okuyarak çalışır.
          </span>
        </div>
      </div>

      <div className="grid-3">
        {TESTS.map((test) => (
          <section className="card" key={test.id}>
            <div className="card__header">
              <div>
                <div className="card__title">{test.title}</div>
                <p className="muted text-sm" style={{ margin: "0.25rem 0 0" }}>
                  {test.description}
                </p>
              </div>
            </div>
            <div className="card__body stack">
              <div className="banner banner--info">
                <span className="banner__body">{test.acceptance}</span>
              </div>
              <button
                className="btn btn--primary"
                onClick={() => void run(test.id)}
                disabled={running !== null || deptId.trim().length === 0}
              >
                {running === test.id ? <span className="spinner" /> : "▶"} Run
              </button>
              {errors[test.id] && (
                <div className="banner banner--danger" role="alert">
                  <span className="banner__body">{errors[test.id]}</span>
                </div>
              )}
              {results[test.id] && <ResultView result={results[test.id]!} />}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}

function ResultView({ result }: { result: SmokeResult }): JSX.Element {
  const steps = Array.isArray(result.steps) ? result.steps : [];
  const cleanup = Array.isArray(result.cleanup) ? result.cleanup : [];
  return (
    <div className="stack" style={{ gap: "0.75rem" }}>
      <div className="banner banner--success">
        <span className="banner__body">
          {result.status.toUpperCase()} · {result.duration_ms ?? "?"}ms
        </span>
      </div>
      <MiniTable title="Steps" rows={steps} />
      <MiniTable title="Cleanup" rows={cleanup} />
      <details>
        <summary className="text-sm muted">Raw response</summary>
        <pre style={{ overflowX: "auto", fontSize: "0.8rem" }}>
          {JSON.stringify(result, null, 2)}
        </pre>
      </details>
    </div>
  );
}

function MiniTable({
  title,
  rows,
}: {
  title: string;
  rows: Array<Record<string, unknown>>;
}): JSX.Element | null {
  if (rows.length === 0) return null;
  return (
    <div>
      <div className="text-sm" style={{ fontWeight: 700, marginBottom: 4 }}>{title}</div>
      <table className="table">
        <tbody>
          {rows.map((row, index) => (
            <tr key={`${title}-${index}`}>
              <td>{String(row.name ?? index + 1)}</td>
              <td>{String(row.status ?? "")}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
