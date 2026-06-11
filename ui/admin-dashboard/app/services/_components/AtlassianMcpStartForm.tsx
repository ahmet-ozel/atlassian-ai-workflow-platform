"use client";

import { useEffect, useMemo, useState } from "react";
import { isSensitiveEnvKey } from "@platform/web-shared";

import { apiFetch } from "../../../lib/api-client";
import type { FormSchemaField } from "./StartFormModal";

type DeploymentMode = "cloud" | "dc";

type Props = {
  fields: FormSchemaField[];
  submitting: boolean;
  setSubmitting: (value: boolean) => void;
  onClose: () => void;
  onStarted?: () => void;
  onFeatureFlagDisabled?: (blockingFlag: string) => void;
};

type FieldConfig = {
  key: string;
  label: string;
  kind?: "text" | "password" | "select";
  options?: Array<{ value: string; label: string }>;
};

const runtimeFields: FieldConfig[] = [
  {
    key: "TRANSPORT",
    label: "Runtime Mode",
    kind: "select",
    options: [
      { value: "streamable-http", label: "streamable-http" },
      { value: "sse", label: "sse" },
    ],
  },
  {
    key: "STATELESS",
    label: "Stateless",
    kind: "select",
    options: [
      { value: "true", label: "true" },
      { value: "false", label: "false" },
    ],
  },
  {
    key: "ATLASSIAN_OAUTH_ENABLE",
    label: "Atlassian OAuth Enable",
    kind: "select",
    options: [
      { value: "true", label: "true" },
      { value: "false", label: "false" },
    ],
  },
  { key: "MCP_ALLOWED_URL_DOMAINS", label: "MCP Allowed URL Domains" },
  { key: "TOOLSETS", label: "Toolsets" },
];

const cloudFields: FieldConfig[] = [
  { key: "JIRA_URL", label: "Jira URL" },
  { key: "JIRA_USERNAME", label: "Jira Email" },
  { key: "JIRA_API_TOKEN", label: "Jira API Token", kind: "password" },
  { key: "CONFLUENCE_URL", label: "Confluence URL" },
  { key: "CONFLUENCE_USERNAME", label: "Confluence Email" },
  {
    key: "CONFLUENCE_API_TOKEN",
    label: "Confluence API Token",
    kind: "password",
  },
  { key: "BITBUCKET_URL", label: "Bitbucket URL" },
  { key: "BITBUCKET_USERNAME", label: "Bitbucket Email" },
  {
    key: "BITBUCKET_API_TOKEN",
    label: "Bitbucket API Token",
    kind: "password",
  },
  {
    key: "BITBUCKET_APP_PASSWORD",
    label: "Bitbucket App Password",
    kind: "password",
  },
  { key: "BITBUCKET_WORKSPACE", label: "Bitbucket Workspace" },
];

const dcFields: FieldConfig[] = [
  { key: "JIRA_URL", label: "Jira URL" },
  { key: "JIRA_PERSONAL_TOKEN", label: "Jira Personal Token", kind: "password" },
  { key: "CONFLUENCE_URL", label: "Confluence URL" },
  {
    key: "CONFLUENCE_PERSONAL_TOKEN",
    label: "Confluence Personal Token",
    kind: "password",
  },
  { key: "BITBUCKET_URL", label: "Bitbucket URL" },
  {
    key: "BITBUCKET_PERSONAL_TOKEN",
    label: "Bitbucket Personal Token",
    kind: "password",
  },
  { key: "BITBUCKET_PROJECT_KEY", label: "Bitbucket Project Key" },
  {
    key: "BITBUCKET_SSL_VERIFY",
    label: "Bitbucket SSL Verify",
    kind: "select",
    options: [
      { value: "true", label: "true" },
      { value: "false", label: "false" },
    ],
  },
  { key: "BITBUCKET_TIMEOUT", label: "Bitbucket Timeout" },
];

const cloudOnlyKeys = new Set([
  "JIRA_USERNAME",
  "JIRA_API_TOKEN",
  "CONFLUENCE_USERNAME",
  "CONFLUENCE_API_TOKEN",
  "BITBUCKET_USERNAME",
  "BITBUCKET_API_TOKEN",
  "BITBUCKET_APP_PASSWORD",
  "BITBUCKET_WORKSPACE",
]);

const dcOnlyKeys = new Set([
  "JIRA_PERSONAL_TOKEN",
  "CONFLUENCE_PERSONAL_TOKEN",
  "BITBUCKET_PERSONAL_TOKEN",
  "BITBUCKET_PROJECT_KEY",
  "BITBUCKET_SSL_VERIFY",
  "BITBUCKET_TIMEOUT",
]);

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
  fontWeight: 600,
};

const inputStyle: React.CSSProperties = {
  padding: "0.45rem 0.55rem",
  border: "1px solid #ccc",
  borderRadius: 4,
  fontFamily: "monospace",
};

const sectionTitleStyle: React.CSSProperties = {
  margin: "1rem 0 0.65rem",
  fontSize: "0.86rem",
  letterSpacing: 0,
  textTransform: "uppercase",
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

const buttonRowStyle: React.CSSProperties = {
  display: "flex",
  justifyContent: "flex-end",
  gap: "0.5rem",
  marginTop: "1rem",
};

export default function AtlassianMcpStartForm({
  fields,
  submitting,
  setSubmitting,
  onClose,
  onStarted,
  onFeatureFlagDisabled,
}: Props): JSX.Element {
  const availableKeys = useMemo(
    () => new Set(fields.map((field) => field.key)),
    [fields],
  );
  const [mode, setMode] = useState<DeploymentMode>("cloud");
  const [values, setValues] = useState<Record<string, string>>(() =>
    buildInitialValues(fields),
  );
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitCorrelationId, setSubmitCorrelationId] = useState<string | null>(
    null,
  );

  useEffect(() => {
    setValues(buildInitialValues(fields));
  }, [fields]);

  function updateValue(key: string, value: string): void {
    setValues((current) => ({ ...current, [key]: value }));
  }

  async function submitForm(ev: React.FormEvent<HTMLFormElement>): Promise<void> {
    ev.preventDefault();
    if (submitting) return;

    const envOverrides = buildEnvOverrides(fields, values, mode);
    setSubmitting(true);
    setSubmitError(null);
    setSubmitCorrelationId(null);

    try {
      const res = await apiFetch("/admin/services/atlassian-mcp/start", {
        method: "POST",
        body: JSON.stringify({ env_overrides: envOverrides }),
      });

      if (res.ok) {
        onStarted?.();
        onClose();
        return;
      }

      if (res.status === 409 && (await handleFeatureFlag(res, onClose, onFeatureFlagDisabled))) {
        return;
      }

      await renderErrorFromResponse(res, setSubmitError, setSubmitCorrelationId);
    } catch (err: unknown) {
      setSubmitError(
        err instanceof Error
          ? `Network error: ${err.message}`
          : "Network error during submit.",
      );
    } finally {
      setSubmitting(false);
    }
  }

  const authFields = mode === "cloud" ? cloudFields : dcFields;

  return (
    <form onSubmit={submitForm} noValidate>
      {submitError != null && (
        <div style={errorBoxStyle} role="alert">
          <div>{submitError}</div>
          {submitCorrelationId != null && (
            <div style={{ marginTop: "0.25rem", fontFamily: "monospace" }}>
              correlation_id: <code>{submitCorrelationId}</code>
            </div>
          )}
        </div>
      )}

      <h3 style={sectionTitleStyle}>Transport</h3>
      {runtimeFields
        .filter((field) => availableKeys.has(field.key))
        .map((field) => (
          <FieldInput
            key={field.key}
            field={field}
            value={values[field.key] ?? ""}
            onChange={updateValue}
          />
        ))}

      <h3 style={sectionTitleStyle}>Atlassian Type</h3>
      <div style={fieldRowStyle}>
        <label style={labelStyle}>
          <span>Atlassian Deployment</span>
          <select
            value={mode}
            onChange={(ev) => setMode(ev.currentTarget.value as DeploymentMode)}
            style={inputStyle}
          >
            <option value="cloud">Atlassian Cloud</option>
            <option value="dc">Atlassian Local/DC</option>
          </select>
        </label>
      </div>

      <h3 style={sectionTitleStyle}>
        {mode === "cloud" ? "Cloud Credentials" : "Local/DC Credentials"}
      </h3>
      {authFields
        .filter((field) => availableKeys.has(field.key))
        .map((field) => (
          <FieldInput
            key={field.key}
            field={field}
            value={values[field.key] ?? ""}
            onChange={updateValue}
          />
        ))}

      <div style={buttonRowStyle}>
        <button
          type="button"
          onClick={onClose}
          disabled={submitting}
          style={{ padding: "0.4rem 0.9rem" }}
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={submitting}
          style={{
            padding: "0.4rem 0.9rem",
            background: "#0b5",
            color: "#fff",
            border: "none",
            borderRadius: 4,
            cursor: submitting ? "wait" : "pointer",
          }}
        >
          {submitting ? "Starting..." : "Start"}
        </button>
      </div>
    </form>
  );
}

function FieldInput({
  field,
  value,
  onChange,
}: {
  field: FieldConfig;
  value: string;
  onChange: (key: string, value: string) => void;
}): JSX.Element {
  const sensitive = field.kind === "password" || isSensitiveEnvKey(field.key);

  return (
    <div style={fieldRowStyle}>
      <label style={labelStyle}>
        <span>{field.label}</span>
        {field.kind === "select" ? (
          <select
            value={value}
            onChange={(ev) => onChange(field.key, ev.currentTarget.value)}
            style={inputStyle}
          >
            {(field.options ?? []).map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        ) : (
          <input
            value={value}
            onChange={(ev) => onChange(field.key, ev.currentTarget.value)}
            type={sensitive ? "password" : "text"}
            autoComplete={sensitive ? "new-password" : "off"}
            style={inputStyle}
          />
        )}
      </label>
    </div>
  );
}

function buildInitialValues(fields: FormSchemaField[]): Record<string, string> {
  const values = Object.fromEntries(
    fields.map((field) => [field.key, field.default_value]),
  ) as Record<string, string>;

  values.TRANSPORT ||= "streamable-http";
  values.STATELESS ||= "true";
  values.ATLASSIAN_OAUTH_ENABLE ||= "true";
  values.MCP_ALLOWED_URL_DOMAINS ||= "true";
  values.TOOLSETS ||= "all";
  values.BITBUCKET_SSL_VERIFY ||= "true";

  return values;
}

function buildEnvOverrides(
  fields: FormSchemaField[],
  values: Record<string, string>,
  mode: DeploymentMode,
): Record<string, string> {
  const envOverrides: Record<string, string> = {};
  for (const field of fields) {
    if (mode === "cloud" && dcOnlyKeys.has(field.key)) {
      envOverrides[field.key] = "";
      continue;
    }
    if (mode === "dc" && cloudOnlyKeys.has(field.key)) {
      envOverrides[field.key] = "";
      continue;
    }
    envOverrides[field.key] = values[field.key] ?? field.default_value ?? "";
  }
  return envOverrides;
}

async function handleFeatureFlag(
  res: Response,
  onClose: () => void,
  onFeatureFlagDisabled?: (blockingFlag: string) => void,
): Promise<boolean> {
  try {
    const ct = res.headers.get("content-type") ?? "";
    if (!ct.includes("application/json")) return false;
    const body = (await res.json()) as {
      error?: string;
      blocking_flag?: string;
    };
    if (
      body.error === "feature_flag_disabled" &&
      typeof body.blocking_flag === "string"
    ) {
      onClose();
      onFeatureFlagDisabled?.(body.blocking_flag);
      return true;
    }
  } catch {
    return false;
  }
  return false;
}

async function renderErrorFromResponse(
  res: Response,
  setError: (msg: string) => void,
  setCorrelationId: (id: string | null) => void,
): Promise<void> {
  const ct = res.headers.get("content-type") ?? "";
  if (ct.includes("application/json")) {
    try {
      const body = (await res.json()) as {
        detail?: unknown;
        correlation_id?: unknown;
      };
      const detail =
        typeof body.detail === "string"
          ? body.detail
          : Array.isArray(body.detail)
            ? JSON.stringify(body.detail)
            : `HTTP ${res.status}`;
      setError(`Start failed (HTTP ${res.status}): ${detail}`);
      setCorrelationId(
        typeof body.correlation_id === "string" ? body.correlation_id : null,
      );
      return;
    } catch {
      // Fall through to text branch.
    }
  }
  const text = await res.text().catch(() => "");
  setError(`Start failed (HTTP ${res.status})${text ? `: ${text}` : ""}`);
  setCorrelationId(null);
}
