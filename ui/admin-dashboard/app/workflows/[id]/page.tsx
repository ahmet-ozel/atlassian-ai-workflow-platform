"use client";

/**
 * Workflow detail page.
 *
 * Dynamic route `/workflows/[id]` - renders the full drill-down view for a
 * single Temporal workflow: header, event history timeline, activity list,
 * LLM usage table, audit chain, external links and a RBAC-aware cancel button.
 *
 * Data is fetched from `GET /admin/workflows/{workflow_id}` which returns the
 * merged envelope (upstream Temporal payload + local Postgres enrichments).
 */

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";

import { apiFetch } from "@/lib/api-client";
import Header from "./_components/Header";
import EventHistoryTimeline from "./_components/EventHistoryTimeline";
import ActivityList from "./_components/ActivityList";
import LlmUsageTable from "./_components/LlmUsageTable";
import AuditChain from "./_components/AuditChain";
import ExternalLinks from "./_components/ExternalLinks";
import CancelButton from "./_components/CancelButton";

export type WorkflowDetail = {
  workflow_id: string;
  workflow_type?: string;
  dept_id?: string | null;
  status?: string;
  started_at?: string | null;
  duration_ms?: number | null;
  cost_usd?: string | null;
  events?: unknown[];
  activities?: unknown[];
  failures?: unknown[];
  llm_usage?: LlmUsageRow[];
  audit_chain?: AuditChainRow[];
  external_links?: ExternalLinksShape;
};

export type LlmUsageRow = {
  activity_id: string;
  prompt_path?: string | null;
  prompt_version?: string | null;
  model?: string | null;
  token_in?: number;
  token_out?: number;
  cost_usd?: string;
};

export type AuditChainRow = {
  action: string;
  actor?: string | null;
  actor_role?: string | null;
  timestamp: string;
  payload_summary?: string | null;
};

export type ExternalLinksShape = {
  jira_issue_url?: string | null;
  bitbucket_pr_url?: string | null;
  confluence_page_url?: string | null;
};

export default function WorkflowDetailPage(): JSX.Element {
  const params = useParams();
  const workflowId = Array.isArray(params?.id) ? params.id[0] : (params?.id ?? "");

  const [detail, setDetail] = useState<WorkflowDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!workflowId) return;
    setLoading(true);
    setError(null);
    apiFetch<WorkflowDetail>(`/admin/workflows/${encodeURIComponent(workflowId)}`)
      .then((data) => setDetail(data))
      .catch((err: Error) => setError(err.message))
      .finally(() => setLoading(false));
  }, [workflowId]);

  if (loading) {
    return <main style={{ padding: "1rem" }}><p>Loading workflow…</p></main>;
  }

  if (error) {
    return (
      <main style={{ padding: "1rem" }}>
        <p style={{ color: "crimson" }}>Error: {error}</p>
        <a href="/workflows">← Back to Workflows</a>
      </main>
    );
  }

  if (!detail) {
    return (
      <main style={{ padding: "1rem" }}>
        <p>Workflow not found.</p>
        <a href="/workflows">← Back to Workflows</a>
      </main>
    );
  }

  return (
    <main style={{ padding: "1rem", maxWidth: "1200px" }}>
      <a href="/workflows" style={{ fontSize: "0.875rem" }}>← Back to Workflows</a>

      <Header detail={detail} />

      <CancelButton
        workflowId={detail.workflow_id}
        status={detail.status}
        deptId={detail.dept_id ?? null}
        onCancelled={() => {
          setDetail((prev) => prev ? { ...prev, status: "cancelled" } : prev);
        }}
      />

      <ExternalLinks links={detail.external_links ?? {}} />

      <EventHistoryTimeline events={detail.events ?? []} />

      <ActivityList activities={detail.activities ?? []} />

      <LlmUsageTable rows={detail.llm_usage ?? []} />

      <AuditChain rows={detail.audit_chain ?? []} />
    </main>
  );
}
