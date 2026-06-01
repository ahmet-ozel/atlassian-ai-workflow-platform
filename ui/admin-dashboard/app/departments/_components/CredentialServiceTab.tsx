"use client";

/**
 * CredentialServiceTab — single-service tab body inside the dept
 * credential modal (uyumluluk task 4.1, R1.8 / R1.9).
 *
 * Rendered three times by :file:`CredentialModal.tsx` (one per
 * Atlassian service: ``jira``, ``confluence``, ``bitbucket``). The
 * tab owns the form state for *its* service only; the modal owns
 * the active-tab selector and the dept-level refresh.
 *
 * Form fields mirror the
 * ``POST /admin/departments/{id}/credentials/{service}`` body
 * (services/automation-service/src/routers/dept_credentials.py
 * → :func:`add_or_update_credential`):
 *
 * * ``url`` — required, plain text.
 * * ``username`` — required, plain text.
 * * ``personal_token`` — required for save, ``<input type=password>``,
 *   never echoed back from the server (write-only).
 * * ``account_id`` — read-only; populated by a successful probe.
 * * ``deployment`` — optional dropdown rendered only for bitbucket.
 *
 * Buttons (R1.8):
 *
 * * ``Test (Probe)`` — calls
 *   ``POST /admin/departments/{id}/probe?service={service}`` and
 *   updates the green ✅ / red ❌ badge from the response.
 * * ``Kaydet``      — calls
 *   ``POST /admin/departments/{id}/credentials/{service}`` and, on
 *   success, asks the modal to close + refetch (R1.9).
 * * ``Sil``         — calls
 *   ``DELETE /admin/departments/{id}/credentials/{service}`` after
 *   a confirm prompt; refetches on success.
 *
 * The component is intentionally pure — it never owns the modal
 * lifecycle or the dept fetch; the parent ``CredentialModal``
 * passes ``onSaved`` / ``onRemoved`` callbacks so the catalog can
 * refetch the dept detail after each mutation lands.
 */

import { useEffect, useMemo, useState } from "react";

import { apiFetch } from "@/lib/api-client";

// ---------------------------------------------------------------------------
// Wire types — kept in sync with services/automation-service/src/routers/
// dept_credentials.py + services/dept_credential_service.py.
// ---------------------------------------------------------------------------

export type ServiceName = "jira" | "confluence" | "bitbucket";

/** One row of ``DepartmentDetail.bots[]`` used to seed the form. */
export type BotRow = {
  service: ServiceName | string;
  credential_ref: string | null;
  account_id: string | null;
  username: string | null;
  deployment: string | null;
};

type AddCredentialSuccess = {
  dept_id: string;
  service: ServiceName;
  account_id: string | null;
  last_probe_at: string | null;
  vault_path: string;
  outcome: "created" | "updated";
};

type RemoveCredentialSuccess = {
  status: "removed";
  dept_id: string;
  service: ServiceName;
  existed: boolean;
};

type ProbeOutcomeRow = {
  service: ServiceName | string;
  status: "ok" | "failed";
  error: string | null;
  account_id: string | null;
};

type ProbeResponseBody = {
  dept_id: string;
  results: ProbeOutcomeRow[];
  probed_at: string;
};

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

export type CredentialServiceTabProps = {
  /** Dept primary key (path segment for every endpoint we hit). */
  deptId: string;
  /** Which service this tab represents. */
  service: ServiceName;
  /** Existing bot row (when the dept already has this credential). */
  existing: BotRow | null;
  /**
   * Called after a successful save (POST credentials). The parent
   * refetches the dept detail so the badge / form reflects the new
   * vault path + account_id. The modal also closes per R1.9.
   */
  onSaved: () => void;
  /**
   * Called after a successful remove (DELETE credentials). The
   * parent refetches the dept detail; the modal stays open so the
   * operator can see the now-empty form.
   */
  onRemoved: () => void;
};

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Default form values for a brand-new credential row. */
function emptyFormState(service: ServiceName): FormState {
  return {
    url: "",
    username: "",
    personalToken: "",
    accountId: "",
    deployment: service === "bitbucket" ? "cloud" : "",
    bitbucketWorkspace: "",
    bitbucketRepo: "",
    confluenceSpaceKey: "",
  };
}

/** Initialise the form from an existing bot row (token is never returned). */
function seedFromExisting(
  service: ServiceName,
  existing: BotRow | null,
): FormState {
  if (existing == null) return emptyFormState(service);
  return {
    url: "", // backend only stores credential_ref, not raw URL
    username: existing.username ?? "",
    personalToken: "", // write-only field — operator must re-enter on update
    accountId: existing.account_id ?? "",
    deployment:
      service === "bitbucket"
        ? (existing.deployment ?? "cloud")
        : (existing.deployment ?? ""),
    bitbucketWorkspace: "",
    bitbucketRepo: "",
    confluenceSpaceKey: "",
  };
}

type FormState = {
  url: string;
  username: string;
  personalToken: string;
  accountId: string;
  deployment: string;
  bitbucketWorkspace: string;
  bitbucketRepo: string;
  confluenceSpaceKey: string;
};

type ProbeBadge =
  | { kind: "none" }
  | { kind: "ok"; account_id: string | null; probed_at: string }
  | { kind: "failed"; error: string; probed_at: string };

async function safeReadDetail(res: Response): Promise<string> {
  try {
    const ct = res.headers.get("content-type") ?? "";
    if (ct.includes("application/json")) {
      const body = (await res.json()) as {
        detail?: unknown;
        error?: unknown;
      };
      if (typeof body.detail === "string") return body.detail;
      if (typeof body.error === "string") return body.error;
      if (Array.isArray(body.detail)) return JSON.stringify(body.detail);
      return JSON.stringify(body);
    }
    const text = await res.text();
    return text.slice(0, 400);
  } catch {
    return res.statusText || `HTTP ${res.status}`;
  }
}

function formatTimestamp(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

// ---------------------------------------------------------------------------
// Inline styles — match StartFormModal / services pages conventions
// ---------------------------------------------------------------------------

const fieldRowStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: "0.25rem",
  marginBottom: "0.75rem",
};

const labelStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: "0.25rem",
  fontFamily: "monospace",
  fontWeight: 600,
};

const inputStyle: React.CSSProperties = {
  padding: "0.4rem 0.5rem",
  border: "1px solid #ccc",
  borderRadius: 4,
  fontFamily: "monospace",
};

const readOnlyInputStyle: React.CSSProperties = {
  ...inputStyle,
  background: "#f3f4f6",
  color: "#374151",
};

const buttonRowStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "flex-end",
  gap: "0.5rem",
  marginTop: "1rem",
  flexWrap: "wrap",
};

const errorBoxStyle: React.CSSProperties = {
  background: "#fdecea",
  border: "1px solid #f5c2c0",
  color: "#611a15",
  padding: "0.5rem 0.75rem",
  borderRadius: 4,
  marginBottom: "0.75rem",
  fontSize: "0.9rem",
};

const successBoxStyle: React.CSSProperties = {
  background: "#dcfce7",
  border: "1px solid #86efac",
  color: "#166534",
  padding: "0.5rem 0.75rem",
  borderRadius: 4,
  marginBottom: "0.75rem",
  fontSize: "0.9rem",
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function CredentialServiceTab({
  deptId,
  service,
  existing,
  onSaved,
  onRemoved,
}: CredentialServiceTabProps): JSX.Element {
  const [form, setForm] = useState<FormState>(() =>
    seedFromExisting(service, existing),
  );
  const [busy, setBusy] = useState<"idle" | "save" | "probe" | "remove">(
    "idle",
  );
  const [error, setError] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);
  const [probeBadge, setProbeBadge] = useState<ProbeBadge>({ kind: "none" });

  // Re-seed when the parent swaps the row underneath us (after a
  // refetch) or when the operator switches to a different tab.
  useEffect(() => {
    setForm(seedFromExisting(service, existing));
    setError(null);
    setSuccessMsg(null);
    setProbeBadge({ kind: "none" });
  }, [service, existing]);

  const isExisting = existing != null;

  const update = (key: keyof FormState, value: string): void => {
    setForm((prev) => ({ ...prev, [key]: value }));
  };

  // --- Save -----------------------------------------------------------------

  async function handleSave(): Promise<void> {
    if (busy !== "idle") return;
    setError(null);
    setSuccessMsg(null);

    // Client-side required-field guard. Sensitive token has no
    // default fallback — operator must type it explicitly even on
    // update (mirrors StartFormModal sensitivity rule).
    if (form.url.trim().length === 0) {
      setError("URL is required.");
      return;
    }
    if (form.username.trim().length === 0) {
      setError("Username is required.");
      return;
    }
    if (form.personalToken.length === 0) {
      setError(
        isExisting
          ? "Personal token is required to update this credential."
          : "Personal token is required.",
      );
      return;
    }

    setBusy("save");
    try {
      const body: Record<string, string> = {
        url: form.url.trim(),
        username: form.username.trim(),
        personal_token: form.personalToken,
      };
      if (form.accountId.trim().length > 0) {
        body.account_id = form.accountId.trim();
      }
      if (service === "bitbucket" && form.deployment.length > 0) {
        body.deployment = form.deployment;
      }
      if (service === "bitbucket") {
        if (form.bitbucketWorkspace.trim().length > 0) {
          body.bitbucket_workspace = form.bitbucketWorkspace.trim();
        }
        if (form.bitbucketRepo.trim().length > 0) {
          body.bitbucket_repo = form.bitbucketRepo.trim();
        }
      }
      if (
        service === "confluence" &&
        form.confluenceSpaceKey.trim().length > 0
      ) {
        body.confluence_space_key = form.confluenceSpaceKey.trim();
      }

      const res = await apiFetch(
        `/admin/departments/${encodeURIComponent(deptId)}/credentials/${encodeURIComponent(service)}`,
        { method: "POST", body: JSON.stringify(body) },
      );

      if (!res.ok) {
        const detail = await safeReadDetail(res);
        setError(`Save failed (HTTP ${res.status}): ${detail}`);
        return;
      }

      const ok = (await res.json()) as AddCredentialSuccess;
      setSuccessMsg(
        `${ok.outcome === "created" ? "Created" : "Updated"} ${service} credential at ${ok.vault_path}.`,
      );
      setProbeBadge({
        kind: "ok",
        account_id: ok.account_id,
        probed_at: ok.last_probe_at ?? new Date().toISOString(),
      });
      // Scrub the in-memory token so a stale render cannot leak it.
      setForm((prev) => ({ ...prev, personalToken: "" }));
      onSaved();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("idle");
    }
  }

  // --- Probe ----------------------------------------------------------------

  async function handleProbe(): Promise<void> {
    if (busy !== "idle") return;
    setError(null);
    setSuccessMsg(null);
    setBusy("probe");
    try {
      const res = await apiFetch(
        `/admin/departments/${encodeURIComponent(deptId)}/probe?service=${encodeURIComponent(service)}`,
        {
          method: "POST",
          body: JSON.stringify({
            ...(service === "bitbucket" &&
            form.bitbucketWorkspace.trim().length > 0
              ? { bitbucket_workspace: form.bitbucketWorkspace.trim() }
              : {}),
            ...(service === "bitbucket" && form.bitbucketRepo.trim().length > 0
              ? { bitbucket_repo: form.bitbucketRepo.trim() }
              : {}),
            ...(service === "confluence" &&
            form.confluenceSpaceKey.trim().length > 0
              ? { confluence_space_key: form.confluenceSpaceKey.trim() }
              : {}),
          }),
        },
      );
      if (!res.ok) {
        const detail = await safeReadDetail(res);
        setError(`Probe failed (HTTP ${res.status}): ${detail}`);
        return;
      }
      const body = (await res.json()) as ProbeResponseBody;
      const row = body.results.find((r) => r.service === service);
      if (row == null) {
        // Probe ran for the dept but the requested service was not
        // registered (no bot row yet). Surface as a soft warning so
        // the operator knows to save first.
        setProbeBadge({ kind: "none" });
        setError(
          `No ${service} bot is registered for this department yet — save credentials first, then re-probe.`,
        );
        return;
      }
      if (row.status === "ok") {
        setProbeBadge({
          kind: "ok",
          account_id: row.account_id,
          probed_at: body.probed_at,
        });
        if (row.account_id != null) {
          setForm((prev) => ({ ...prev, accountId: row.account_id ?? "" }));
        }
      } else {
        setProbeBadge({
          kind: "failed",
          error: row.error ?? "unknown",
          probed_at: body.probed_at,
        });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("idle");
    }
  }

  // --- Remove ---------------------------------------------------------------

  async function handleRemove(): Promise<void> {
    if (busy !== "idle") return;
    if (!isExisting) {
      setError(
        `No saved ${service} credential to remove for this department.`,
      );
      return;
    }
    const confirmed = window.confirm(
      `Remove the ${service} credential for department "${deptId}"? ` +
        "Any workflow using this bot will fail until a new credential is saved.",
    );
    if (!confirmed) return;

    setError(null);
    setSuccessMsg(null);
    setBusy("remove");
    try {
      const res = await apiFetch(
        `/admin/departments/${encodeURIComponent(deptId)}/credentials/${encodeURIComponent(service)}`,
        { method: "DELETE" },
      );
      if (!res.ok) {
        const detail = await safeReadDetail(res);
        setError(`Remove failed (HTTP ${res.status}): ${detail}`);
        return;
      }
      const ok = (await res.json()) as RemoveCredentialSuccess;
      setSuccessMsg(
        ok.existed
          ? `Removed ${service} credential.`
          : `No ${service} credential was registered (idempotent remove).`,
      );
      setForm(emptyFormState(service));
      setProbeBadge({ kind: "none" });
      onRemoved();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy("idle");
    }
  }

  // --- Render ---------------------------------------------------------------

  const tokenPlaceholder = isExisting
    ? "(re-enter to update — never echoed back)"
    : "(required)";

  const probeBadgeNode = useMemo(() => {
    if (probeBadge.kind === "none") {
      return (
        <span
          style={{
            display: "inline-block",
            padding: "0.15rem 0.55rem",
            borderRadius: "0.75rem",
            fontSize: "0.78rem",
            fontWeight: 500,
            background: "#e5e7eb",
            color: "#374151",
          }}
          aria-label="No probe result yet"
        >
          ⚪ Not probed yet
        </span>
      );
    }
    if (probeBadge.kind === "ok") {
      return (
        <span
          style={{
            display: "inline-block",
            padding: "0.15rem 0.55rem",
            borderRadius: "0.75rem",
            fontSize: "0.78rem",
            fontWeight: 600,
            background: "#dcfce7",
            color: "#166534",
            border: "1px solid #86efac",
          }}
          title={`Probed at ${formatTimestamp(probeBadge.probed_at)}`}
          aria-label="Probe succeeded"
          data-testid="probe-badge-ok"
        >
          ✅ Connected
          {probeBadge.account_id ? ` · ${probeBadge.account_id}` : ""}
        </span>
      );
    }
    return (
      <span
        style={{
          display: "inline-block",
          padding: "0.15rem 0.55rem",
          borderRadius: "0.75rem",
          fontSize: "0.78rem",
          fontWeight: 600,
          background: "#fee2e2",
          color: "#7f1d1d",
          border: "1px solid #fecaca",
        }}
        title={`${probeBadge.error} (${formatTimestamp(probeBadge.probed_at)})`}
        aria-label="Probe failed"
        data-testid="probe-badge-failed"
      >
        ❌ Failed
      </span>
    );
  }, [probeBadge]);

  return (
    <section aria-label={`${service} credential form`}>
      <header
        style={{
          display: "flex",
          alignItems: "center",
          gap: "0.75rem",
          marginBottom: "0.75rem",
          flexWrap: "wrap",
        }}
      >
        <h3 style={{ margin: 0, fontSize: "1rem", textTransform: "capitalize" }}>
          {service} credential
        </h3>
        {probeBadgeNode}
        {isExisting && existing?.credential_ref && (
          <code
            style={{
              fontSize: "0.78rem",
              color: "#6b7280",
              background: "#f9fafb",
              padding: "0.1rem 0.4rem",
              borderRadius: 3,
            }}
            title="Vault path (mask edilmiş)"
          >
            {existing.credential_ref}
          </code>
        )}
      </header>

      {error && (
        <div role="alert" style={errorBoxStyle}>
          {error}
        </div>
      )}
      {successMsg && (
        <div role="status" style={successBoxStyle}>
          {successMsg}
        </div>
      )}

      <form
        onSubmit={(ev) => {
          ev.preventDefault();
          void handleSave();
        }}
        noValidate
      >
        <div style={fieldRowStyle}>
          <label style={labelStyle}>
            <span>url</span>
            <input
              type="text"
              value={form.url}
              onChange={(ev) => update("url", ev.target.value)}
              placeholder={
                service === "bitbucket"
                  ? "https://bitbucket.example.com"
                  : "https://example.atlassian.net"
              }
              autoComplete="off"
              required
              style={inputStyle}
            />
          </label>
        </div>
        <div style={fieldRowStyle}>
          <label style={labelStyle}>
            <span>username</span>
            <input
              type="text"
              value={form.username}
              onChange={(ev) => update("username", ev.target.value)}
              placeholder="bot@example.com"
              autoComplete="off"
              required
              style={inputStyle}
            />
          </label>
        </div>
        <div style={fieldRowStyle}>
          <label style={labelStyle}>
            <span>
              personal_token{" "}
              <span
                aria-label="sensitive"
                title="Sensitive — never echoed back from the server"
                style={{ marginLeft: "0.4rem", color: "#b00" }}
              >
                *
              </span>
            </span>
            <input
              type="password"
              value={form.personalToken}
              onChange={(ev) => update("personalToken", ev.target.value)}
              placeholder={tokenPlaceholder}
              autoComplete="new-password"
              required
              style={inputStyle}
            />
          </label>
        </div>
        <div style={fieldRowStyle}>
          <label style={labelStyle}>
            <span>account_id (read-only — populated by probe)</span>
            <input
              type="text"
              value={form.accountId}
              readOnly
              tabIndex={-1}
              aria-readonly="true"
              placeholder="(probe to fetch)"
              style={readOnlyInputStyle}
            />
          </label>
        </div>
        {service === "bitbucket" && (
          <>
            <div style={fieldRowStyle}>
              <label style={labelStyle}>
                <span>bitbucket_workspace</span>
                <input
                  type="text"
                  value={form.bitbucketWorkspace}
                  onChange={(ev) =>
                    update("bitbucketWorkspace", ev.target.value)
                  }
                  placeholder="workspace-slug"
                  autoComplete="off"
                  style={inputStyle}
                />
              </label>
            </div>
            <div style={fieldRowStyle}>
              <label style={labelStyle}>
                <span>bitbucket_repo</span>
                <input
                  type="text"
                  value={form.bitbucketRepo}
                  onChange={(ev) => update("bitbucketRepo", ev.target.value)}
                  placeholder="repo-slug"
                  autoComplete="off"
                  style={inputStyle}
                />
              </label>
            </div>
            <div style={fieldRowStyle}>
              <label style={labelStyle}>
                <span>deployment</span>
                <select
                  value={form.deployment}
                  onChange={(ev) => update("deployment", ev.target.value)}
                  style={inputStyle}
                >
                  <option value="cloud">cloud</option>
                  <option value="server">server (DC)</option>
                  <option value="dc">dc</option>
                </select>
              </label>
            </div>
          </>
        )}

        {service === "confluence" && (
          <div style={fieldRowStyle}>
            <label style={labelStyle}>
              <span>confluence_space_key</span>
              <input
                type="text"
                value={form.confluenceSpaceKey}
                onChange={(ev) => update("confluenceSpaceKey", ev.target.value)}
                placeholder="SPACEKEY"
                autoComplete="off"
                style={inputStyle}
              />
            </label>
          </div>
        )}

        <div style={buttonRowStyle}>
          <button
            type="button"
            onClick={() => void handleProbe()}
            disabled={busy !== "idle"}
            style={{
              padding: "0.4rem 0.9rem",
              border: "1px solid #2563eb",
              color: "#2563eb",
              background: "#ffffff",
              borderRadius: 4,
              cursor: busy === "probe" ? "wait" : "pointer",
              fontSize: "0.9rem",
              fontWeight: 500,
            }}
            title="Run connectivity probe against the saved credential"
          >
            {busy === "probe" ? "Probing…" : "Test (Probe)"}
          </button>
          <button
            type="button"
            onClick={() => void handleRemove()}
            disabled={busy !== "idle" || !isExisting}
            style={{
              padding: "0.4rem 0.9rem",
              border: "1px solid #b91c1c",
              color: isExisting ? "#b91c1c" : "#9ca3af",
              background: "#ffffff",
              borderRadius: 4,
              cursor:
                busy === "remove"
                  ? "wait"
                  : isExisting
                    ? "pointer"
                    : "not-allowed",
              opacity: isExisting ? 1 : 0.6,
              fontSize: "0.9rem",
              fontWeight: 500,
            }}
            title={
              isExisting
                ? "Remove the saved credential and audit dept_credential_removed"
                : "No saved credential to remove"
            }
          >
            {busy === "remove" ? "Removing…" : "Sil"}
          </button>
          <button
            type="submit"
            disabled={busy !== "idle"}
            style={{
              padding: "0.4rem 0.9rem",
              background: "#0b5",
              color: "#fff",
              border: "none",
              borderRadius: 4,
              cursor: busy === "save" ? "wait" : "pointer",
              fontSize: "0.9rem",
              fontWeight: 600,
            }}
          >
            {busy === "save" ? "Kaydediliyor…" : "Kaydet"}
          </button>
        </div>
      </form>
    </section>
  );
}
