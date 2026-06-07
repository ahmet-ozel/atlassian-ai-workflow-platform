"use client";

/**
 * Prompt editor page.
 *
 * Editor surface for the
 * Prompt_Versioning system - diff, sandbox LLM call, draft PR).
 *
 * Catch-all route - the prompt name is everything after `/prompts/`
 * so that paths with slashes (eg. `notifications/build_failed.md`)
 * round-trip cleanly. Next.js delivers each segment as an array on
 * `params.name`; we re-join them with `/` and forward that to the
 * v1 API.
 *
 * UI flow:
 *   1. Fetch current content via `GET /api/v1/prompts/{name}`.
 *   2. Operator edits the body in a textarea; a per-line diff view
 *      renders next to it (`./_lib/diff.mjs`).
 *   3. **Sandbox** button - `POST /api/v1/prompts/{name}/sandbox`
 *      with `{ body, sample_input }`. The response panel shows the
 *      LLM output plus model / provider / token counts.
 *   4. **Commit** button - opens a 5-second confirm dialog (mirrors
 *      `app/feature-flags/page.tsx`) before calling
 *      `POST /api/v1/prompts/{name}/commit`. On success the panel
 *      shows the resulting PR URL with a clickable link.
 *
 * 503 handling: both the sandbox and commit endpoints surface
 * `{ status: "not_ready", reason }` JSON payloads when their backing
 * `app.state` slot (LLM client, git committer, Bitbucket client,
 * Postgres pool) is missing. The editor renders that as a clear
 * "service not ready" banner with the reason from the response -
 * admins should not interpret a 503 as a generic 5xx.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "next/navigation";

import { apiFetch } from "@/lib/api-client";

import { diffLines, hasChanges } from "../_lib/diff.mjs";
import type { DiffLine } from "../_lib/diff.d.mts";

// ---------------------------------------------------------------------------
// Wire types - kept in sync with `src/routers/prompts.py`.
// ---------------------------------------------------------------------------

type PromptDetailResponse = {
  name: string;
  content: string;
  content_hash: string;
  last_modified: string;
  size_bytes: number;
};

type PromptSandboxResponse = {
  name: string;
  response_text: string;
  model: string | null;
  provider: string | null;
  token_in: number | null;
  token_out: number | null;
};

type PromptCommitResponse = {
  name: string;
  branch: string;
  commit_sha: string;
  pr_id: string;
  pr_url: string;
  content_hash: string;
  version_id: number;
};

/**
 * Shape of the `detail` field on a 503 from the v1 prompts surface
 * (see `_get_llm_client` / `_get_committer` / `_get_bitbucket` /
 * `_get_pg_pool` in `src/routers/prompts.py`).
 */
type ServiceNotReadyDetail = {
  status: "not_ready";
  reason: string;
  message?: string;
};

// ---------------------------------------------------------------------------
// API call results
// ---------------------------------------------------------------------------

type ApiError =
  | { kind: "not_ready"; reason: string; message?: string }
  | { kind: "error"; message: string };

type SandboxState =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "done"; result: PromptSandboxResponse }
  | ApiError;

type CommitState =
  | { kind: "idle" }
  | { kind: "running" }
  | { kind: "done"; result: PromptCommitResponse }
  | ApiError;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Re-encode each path segment so `apiFetch` builds the same URL the
 *  router expects. The catch-all returns each segment URL-decoded. */
function buildApiPath(name: string, suffix = ""): string {
  const segments = name.split("/").map(encodeURIComponent).join("/");
  return `/api/v1/prompts/${segments}${suffix}`;
}

/** Coerce an unknown payload into `ServiceNotReadyDetail` if it
 *  matches the shape; returns `null` otherwise. */
function asNotReady(detail: unknown): ServiceNotReadyDetail | null {
  if (
    detail !== null &&
    typeof detail === "object" &&
    "status" in detail &&
    (detail as { status: unknown }).status === "not_ready" &&
    "reason" in detail &&
    typeof (detail as { reason: unknown }).reason === "string"
  ) {
    const obj = detail as ServiceNotReadyDetail;
    return {
      status: "not_ready",
      reason: obj.reason,
      message: typeof obj.message === "string" ? obj.message : undefined,
    };
  }
  return null;
}

/** Read a non-2xx Response into a discriminated `ApiError`. The
 *  router emits FastAPI-style `{ detail: ... }` envelopes; we unwrap
 *  one level to surface the not-ready reason verbatim. */
async function classifyError(res: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    /* fall through - try plain text */
    try {
      const text = await res.text();
      return {
        kind: "error",
        message: `HTTP ${res.status}${text ? `: ${text.slice(0, 300)}` : ""}`,
      };
    } catch {
      return { kind: "error", message: `HTTP ${res.status}` };
    }
  }

  const detail = (body as { detail?: unknown }).detail;

  if (res.status === 503) {
    const notReady = asNotReady(detail);
    if (notReady !== null) {
      return {
        kind: "not_ready",
        reason: notReady.reason,
        message: notReady.message,
      };
    }
    return {
      kind: "error",
      message:
        typeof detail === "string"
          ? `HTTP 503: ${detail}`
          : `HTTP 503: service not ready`,
    };
  }

  if (typeof detail === "string") {
    return { kind: "error", message: `HTTP ${res.status}: ${detail}` };
  }
  if (detail !== null && typeof detail === "object") {
    const inner = detail as { error?: unknown; message?: unknown };
    const msg =
      (typeof inner.message === "string" ? inner.message : undefined) ??
      (typeof inner.error === "string" ? inner.error : undefined) ??
      JSON.stringify(detail).slice(0, 300);
    return { kind: "error", message: `HTTP ${res.status}: ${msg}` };
  }
  return { kind: "error", message: `HTTP ${res.status}` };
}

// ---------------------------------------------------------------------------
// Diff view
// ---------------------------------------------------------------------------

function DiffView({ oldBody, newBody }: { oldBody: string; newBody: string }) {
  const lines: DiffLine[] = useMemo(
    () => diffLines(oldBody, newBody),
    [oldBody, newBody],
  );

  if (!hasChanges(oldBody, newBody)) {
    return (
      <div
        style={{
          padding: "0.75rem",
          color: "#666",
          fontStyle: "italic",
          background: "#f9fafb",
          border: "1px solid #e5e7eb",
          borderRadius: "0.25rem",
        }}
      >
        (no changes yet - edit the body above to preview a diff)
      </div>
    );
  }

  return (
    <pre
      style={{
        margin: 0,
        padding: "0.5rem",
        background: "#0f172a",
        color: "#e2e8f0",
        borderRadius: "0.25rem",
        fontSize: "0.8rem",
        overflowX: "auto",
        maxHeight: "24rem",
      }}
    >
      {lines.map((line, idx) => {
        const prefix =
          line.kind === "add" ? "+" : line.kind === "remove" ? "-" : " ";
        const color =
          line.kind === "add"
            ? "#86efac"
            : line.kind === "remove"
              ? "#fca5a5"
              : "#cbd5e1";
        return (
          <div
            key={idx}
            style={{
              color,
              whiteSpace: "pre-wrap",
              wordBreak: "break-all",
              fontFamily: "monospace",
            }}
          >
            <span style={{ display: "inline-block", width: "1.25ch" }}>
              {prefix}
            </span>
            {line.text}
          </div>
        );
      })}
    </pre>
  );
}

// ---------------------------------------------------------------------------
// 5-second commit confirm dialog (mirrors feature-flags/page.tsx)
// ---------------------------------------------------------------------------

const CONFIRM_SECONDS = 5;

function ConfirmCommitDialog({
  promptName,
  onConfirm,
  onCancel,
}: {
  promptName: string;
  onConfirm: () => void;
  onCancel: () => void;
}): JSX.Element {
  const [secondsLeft, setSecondsLeft] = useState<number>(CONFIRM_SECONDS);

  useEffect(() => {
    if (secondsLeft <= 0) return;
    const handle = window.setTimeout(() => {
      setSecondsLeft((prev) => Math.max(0, prev - 1));
    }, 1000);
    return () => window.clearTimeout(handle);
  }, [secondsLeft]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="prompt-commit-confirm-title"
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.45)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 1000,
      }}
    >
      <div
        style={{
          background: "white",
          padding: "1.5rem",
          borderRadius: "8px",
          minWidth: "360px",
          maxWidth: "520px",
          boxShadow: "0 8px 24px rgba(0,0,0,0.2)",
        }}
      >
        <h2 id="prompt-commit-confirm-title" style={{ marginTop: 0 }}>
          Commit prompt change
        </h2>
        <p style={{ fontSize: "0.95rem", color: "#333" }}>
          Open a draft PR with the new content for{" "}
          <code>{promptName}</code>?
        </p>
        <p style={{ fontSize: "0.85rem", color: "#666", marginBottom: "1rem" }}>
          The change is applied immediately when you press <strong>Apply</strong>,
          or after the {CONFIRM_SECONDS}-second countdown reaches 0. Press{" "}
          <strong>Cancel</strong> to abort.
        </p>
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
          }}
        >
          <span aria-live="polite" style={{ fontVariantNumeric: "tabular-nums" }}>
            {secondsLeft > 0 ? `Auto-apply in ${secondsLeft}s` : "Ready."}
          </span>
          <span style={{ display: "flex", gap: "0.5rem" }}>
            <button onClick={onCancel} type="button">
              Cancel
            </button>
            <button onClick={onConfirm} type="button" style={{ fontWeight: "bold" }}>
              Apply now
            </button>
          </span>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sandbox / commit result panels
// ---------------------------------------------------------------------------

function NotReadyBanner({
  state,
  label,
}: {
  state: { kind: "not_ready"; reason: string; message?: string };
  label: string;
}) {
  return (
    <div
      role="alert"
      style={{
        padding: "0.75rem 1rem",
        background: "#fef3c7",
        border: "1px solid #fde68a",
        borderRadius: "0.25rem",
        color: "#78350f",
      }}
    >
      <strong>{label} service not ready.</strong>{" "}
      <span>
        Reason: <code>{state.reason}</code>
      </span>
      {state.message && (
        <div style={{ marginTop: "0.25rem", fontSize: "0.85rem" }}>
          {state.message}
        </div>
      )}
    </div>
  );
}

function SandboxResultPanel({ result }: { result: PromptSandboxResponse }) {
  return (
    <div
      style={{
        padding: "0.75rem",
        background: "#f0fdf4",
        border: "1px solid #bbf7d0",
        borderRadius: "0.25rem",
      }}
    >
      <div
        style={{
          display: "flex",
          gap: "1rem",
          flexWrap: "wrap",
          fontSize: "0.85rem",
          color: "#166534",
          marginBottom: "0.5rem",
        }}
      >
        <span>
          model: <code>{result.model ?? "-"}</code>
        </span>
        <span>
          provider: <code>{result.provider ?? "-"}</code>
        </span>
        <span>
          token_in: <code>{result.token_in ?? "-"}</code>
        </span>
        <span>
          token_out: <code>{result.token_out ?? "-"}</code>
        </span>
      </div>
      <pre
        style={{
          margin: 0,
          padding: "0.5rem",
          background: "#ffffff",
          border: "1px solid #d1fae5",
          borderRadius: "0.25rem",
          fontSize: "0.85rem",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          maxHeight: "20rem",
          overflowY: "auto",
        }}
      >
        {result.response_text}
      </pre>
    </div>
  );
}

function CommitResultPanel({ result }: { result: PromptCommitResponse }) {
  return (
    <div
      style={{
        padding: "0.75rem",
        background: "#eff6ff",
        border: "1px solid #bfdbfe",
        borderRadius: "0.25rem",
      }}
    >
      <p style={{ margin: "0 0 0.5rem 0", fontWeight: 600 }}>
        Draft PR opened.
      </p>
      <ul style={{ margin: 0, paddingLeft: "1.25rem", fontSize: "0.85rem" }}>
        <li>
          <strong>PR:</strong>{" "}
          <a
            href={result.pr_url}
            target="_blank"
            rel="noopener noreferrer"
            style={{ color: "#1d4ed8" }}
          >
            {result.pr_url}
          </a>{" "}
          <span style={{ color: "#666" }}>(id: {result.pr_id})</span>
        </li>
        <li>
          <strong>branch:</strong> <code>{result.branch}</code>
        </li>
        <li>
          <strong>commit_sha:</strong> <code>{result.commit_sha}</code>
        </li>
        <li>
          <strong>content_hash:</strong> <code>{result.content_hash}</code>
        </li>
        <li>
          <strong>version_id:</strong> {result.version_id}
        </li>
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

type LoadState =
  | { kind: "loading" }
  | { kind: "ok"; detail: PromptDetailResponse }
  | { kind: "error"; message: string };

export default function PromptEditorPage(): JSX.Element {
  const params = useParams();
  const rawName = params?.name;
  const promptName = useMemo(() => {
    if (Array.isArray(rawName)) {
      return rawName.map((s) => decodeURIComponent(s)).join("/");
    }
    if (typeof rawName === "string") {
      return decodeURIComponent(rawName);
    }
    return "";
  }, [rawName]);

  const [load, setLoad] = useState<LoadState>({ kind: "loading" });
  const [draftBody, setDraftBody] = useState<string>("");
  const [sampleInput, setSampleInput] = useState<string>("");
  const [sandboxState, setSandboxState] = useState<SandboxState>({
    kind: "idle",
  });
  const [commitState, setCommitState] = useState<CommitState>({ kind: "idle" });
  const [pendingCommit, setPendingCommit] = useState(false);

  // Auto-apply timer for the commit confirm dialog. Mirrors the
  // pattern in app/feature-flags/page.tsx so the two surfaces feel
  // identical to the operator.
  const autoApplyRef = useRef<number | null>(null);

  const refreshContent = useCallback(async () => {
    if (promptName === "") return;
    setLoad({ kind: "loading" });
    try {
      const res = await apiFetch(buildApiPath(promptName));
      if (!res.ok) {
        const err = await classifyError(res);
        throw new Error(
          err.kind === "not_ready"
            ? `service not ready: ${err.reason}`
            : err.message,
        );
      }
      const detail = (await res.json()) as PromptDetailResponse;
      setLoad({ kind: "ok", detail });
      setDraftBody(detail.content);
    } catch (err) {
      setLoad({ kind: "error", message: (err as Error).message });
    }
  }, [promptName]);

  useEffect(() => {
    void refreshContent();
  }, [refreshContent]);

  // -------------------------------------------------------------------------
  // Sandbox
  // -------------------------------------------------------------------------

  const handleSandbox = useCallback(async () => {
    if (load.kind !== "ok") return;
    setSandboxState({ kind: "running" });
    try {
      const res = await apiFetch(buildApiPath(promptName, "/sandbox"), {
        method: "POST",
        body: JSON.stringify({
          body: draftBody,
          sample_input: sampleInput,
        }),
      });
      if (!res.ok) {
        const err = await classifyError(res);
        setSandboxState(err);
        return;
      }
      const result = (await res.json()) as PromptSandboxResponse;
      setSandboxState({ kind: "done", result });
    } catch (err) {
      setSandboxState({
        kind: "error",
        message: (err as Error).message,
      });
    }
  }, [load.kind, promptName, draftBody, sampleInput]);

  // -------------------------------------------------------------------------
  // Commit (with 5s confirm dialog)
  // -------------------------------------------------------------------------

  const cancelPendingCommit = useCallback(() => {
    if (autoApplyRef.current !== null) {
      window.clearTimeout(autoApplyRef.current);
      autoApplyRef.current = null;
    }
    setPendingCommit(false);
  }, []);

  const applyCommit = useCallback(async () => {
    cancelPendingCommit();
    if (load.kind !== "ok") return;
    setCommitState({ kind: "running" });
    try {
      const res = await apiFetch(buildApiPath(promptName, "/commit"), {
        method: "POST",
        body: JSON.stringify({ body: draftBody }),
      });
      if (!res.ok) {
        const err = await classifyError(res);
        setCommitState(err);
        return;
      }
      const result = (await res.json()) as PromptCommitResponse;
      setCommitState({ kind: "done", result });
      // Refresh in background so subsequent edits diff against the
      // freshly-committed body.
      void refreshContent();
    } catch (err) {
      setCommitState({
        kind: "error",
        message: (err as Error).message,
      });
    }
  }, [cancelPendingCommit, load.kind, promptName, draftBody, refreshContent]);

  // Auto-apply 5s after the dialog opens - same UX as feature-flags.
  useEffect(() => {
    if (!pendingCommit) return;
    if (autoApplyRef.current !== null) {
      window.clearTimeout(autoApplyRef.current);
    }
    autoApplyRef.current = window.setTimeout(() => {
      autoApplyRef.current = null;
      void applyCommit();
    }, CONFIRM_SECONDS * 1000);
    return () => {
      if (autoApplyRef.current !== null) {
        window.clearTimeout(autoApplyRef.current);
        autoApplyRef.current = null;
      }
    };
  }, [pendingCommit, applyCommit]);

  // -------------------------------------------------------------------------
  // Render
  // -------------------------------------------------------------------------

  if (promptName === "") {
    return (
      <main style={{ padding: "1rem" }}>
        <p>Loading editor…</p>
      </main>
    );
  }

  if (load.kind === "loading") {
    return (
      <main style={{ padding: "1rem" }}>
        <a href="/prompts" style={{ fontSize: "0.85rem" }}>
          ← Back to Prompts
        </a>
        <p>Loading prompt…</p>
      </main>
    );
  }

  if (load.kind === "error") {
    return (
      <main style={{ padding: "1rem" }}>
        <a href="/prompts" style={{ fontSize: "0.85rem" }}>
          ← Back to Prompts
        </a>
        <p role="alert" style={{ color: "crimson" }}>
          Error loading <code>{promptName}</code>: {load.message}
        </p>
        <button type="button" onClick={() => void refreshContent()}>
          Retry
        </button>
      </main>
    );
  }

  const detail = load.detail;
  const dirty = hasChanges(detail.content, draftBody);
  const sandboxBusy = sandboxState.kind === "running";
  const commitBusy = commitState.kind === "running" || pendingCommit;

  return (
    <main
      style={{
        padding: "1rem",
        fontFamily: "system-ui, sans-serif",
        maxWidth: "1400px",
      }}
    >
      <a href="/prompts" style={{ fontSize: "0.85rem" }}>
        ← Back to Prompts
      </a>

      <h1 style={{ marginTop: "0.5rem" }}>
        Edit prompt: <code>{promptName}</code>
      </h1>
      <p
        style={{
          marginTop: 0,
          marginBottom: "0.75rem",
          color: "#555",
          fontSize: "0.9rem",
        }}
      >
        last_modified: {detail.last_modified} · size: {detail.size_bytes} B ·
        content_hash: <code>{detail.content_hash}</code>
      </p>

      {/* ----- Editor + diff side-by-side ----- */}
      <section
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: "1rem",
          marginBottom: "1rem",
        }}
      >
        <div>
          <h2 style={{ fontSize: "1rem" }}>Edit body</h2>
          <textarea
            value={draftBody}
            onChange={(e) => setDraftBody(e.target.value)}
            spellCheck={false}
            style={{
              width: "100%",
              minHeight: "24rem",
              fontFamily: "monospace",
              fontSize: "0.85rem",
              padding: "0.5rem",
              border: "1px solid #d1d5db",
              borderRadius: "0.25rem",
              resize: "vertical",
            }}
          />
          <div
            style={{
              fontSize: "0.8rem",
              color: dirty ? "#1d4ed8" : "#666",
              marginTop: "0.25rem",
            }}
          >
            {dirty ? "● unsaved changes" : "no changes"}
            {" · "}
            {new Blob([draftBody]).size} bytes
          </div>
        </div>

        <div>
          <h2 style={{ fontSize: "1rem" }}>Diff (old vs new)</h2>
          <DiffView oldBody={detail.content} newBody={draftBody} />
        </div>
      </section>

      {/* ----- Sandbox panel ----- */}
      <section
        style={{
          marginBottom: "1rem",
          padding: "0.75rem",
          border: "1px solid #e5e7eb",
          borderRadius: "0.25rem",
          background: "#fafafa",
        }}
      >
        <h2 style={{ fontSize: "1rem", marginTop: 0 }}>Sandbox</h2>
        <p style={{ fontSize: "0.85rem", color: "#555", marginTop: 0 }}>
          Run the edited body against the sandbox LLM (no persistence).
        </p>
        <label
          htmlFor="sample-input"
          style={{
            display: "block",
            fontSize: "0.85rem",
            fontWeight: 600,
            marginBottom: "0.25rem",
          }}
        >
          Sample input
        </label>
        <textarea
          id="sample-input"
          value={sampleInput}
          onChange={(e) => setSampleInput(e.target.value)}
          placeholder="Optional sample user input forwarded to the LLM."
          style={{
            width: "100%",
            minHeight: "5rem",
            fontFamily: "monospace",
            fontSize: "0.85rem",
            padding: "0.5rem",
            border: "1px solid #d1d5db",
            borderRadius: "0.25rem",
            resize: "vertical",
          }}
        />
        <div
          style={{
            display: "flex",
            gap: "0.5rem",
            alignItems: "center",
            marginTop: "0.5rem",
          }}
        >
          <button
            type="button"
            disabled={sandboxBusy || draftBody.length === 0}
            onClick={() => void handleSandbox()}
            style={{
              padding: "0.4rem 0.9rem",
              fontSize: "0.9rem",
              background: "#2563eb",
              color: "#ffffff",
              border: "none",
              borderRadius: "0.25rem",
              cursor: sandboxBusy ? "not-allowed" : "pointer",
              opacity: sandboxBusy ? 0.6 : 1,
            }}
          >
            {sandboxBusy ? "Running…" : "Sandbox"}
          </button>
          {sandboxState.kind === "done" && (
            <span style={{ fontSize: "0.85rem", color: "#15803d" }}>
              ✅ ran successfully
            </span>
          )}
        </div>

        <div style={{ marginTop: "0.75rem" }}>
          {sandboxState.kind === "not_ready" && (
            <NotReadyBanner state={sandboxState} label="Sandbox" />
          )}
          {sandboxState.kind === "error" && (
            <p role="alert" style={{ color: "crimson", fontSize: "0.85rem" }}>
              {sandboxState.message}
            </p>
          )}
          {sandboxState.kind === "done" && (
            <SandboxResultPanel result={sandboxState.result} />
          )}
        </div>
      </section>

      {/* ----- Commit panel ----- */}
      <section
        style={{
          marginBottom: "1rem",
          padding: "0.75rem",
          border: "1px solid #e5e7eb",
          borderRadius: "0.25rem",
          background: "#fafafa",
        }}
      >
        <h2 style={{ fontSize: "1rem", marginTop: 0 }}>Commit</h2>
        <p style={{ fontSize: "0.85rem", color: "#555", marginTop: 0 }}>
          Open a draft PR with the new content. A {CONFIRM_SECONDS}-second
          confirm dialog protects against misclicks.
        </p>
        <button
          type="button"
          disabled={!dirty || commitBusy}
          onClick={() => setPendingCommit(true)}
          style={{
            padding: "0.4rem 0.9rem",
            fontSize: "0.9rem",
            background: "#16a34a",
            color: "#ffffff",
            border: "none",
            borderRadius: "0.25rem",
            cursor: !dirty || commitBusy ? "not-allowed" : "pointer",
            opacity: !dirty || commitBusy ? 0.6 : 1,
            fontWeight: 600,
          }}
        >
          {commitState.kind === "running" ? "Committing…" : "Commit (open draft PR)"}
        </button>

        <div style={{ marginTop: "0.75rem" }}>
          {commitState.kind === "not_ready" && (
            <NotReadyBanner state={commitState} label="Commit" />
          )}
          {commitState.kind === "error" && (
            <p role="alert" style={{ color: "crimson", fontSize: "0.85rem" }}>
              {commitState.message}
            </p>
          )}
          {commitState.kind === "done" && (
            <CommitResultPanel result={commitState.result} />
          )}
        </div>
      </section>

      {pendingCommit && (
        <ConfirmCommitDialog
          promptName={promptName}
          onConfirm={() => void applyCommit()}
          onCancel={cancelPendingCommit}
        />
      )}
    </main>
  );
}
