"use client";

/**
 * Add/Edit provider modal (Requirements 4.5, 4.6, 14.2 — 14.6).
 *
 * Visible fields depend on `provider_type`:
 *
 * - **vllm**:      base_url (required), api_key (optional)
 * - **openai**:    api_key (required), org_id (optional), base_url (optional)
 * - **anthropic**: api_key (required)
 * - **gemini**:    api_key (required)
 *
 * Edit mode keeps the `api_key` input empty and shows a helper line
 * with the masked existing value; on submit, an empty input means
 * "preserve" (R4.6) — we omit the `api_key` key from the PUT body so
 * the backend service merges only the fields the operator actually
 * changed.
 *
 * The inline **Test Connection** button calls
 * `POST /admin/llm-providers/test` with the current form values
 * (without saving) and renders the result through `<TestResultBadge>`.
 */

import { useEffect, useState } from "react";

import TestResultBadge from "./TestResultBadge";
import { useProviderApi, ApiError } from "./useProviderApi";
import type {
  ConnectionTestResult,
  ProviderCreatePayload,
  ProviderRow,
  ProviderType,
  ProviderUpdatePayload,
} from "./types";

interface ProviderModalProps {
  /** When set, the modal opens in edit mode for this row. */
  initial?: ProviderRow;
  onClose: () => void;
  onSaved: () => void;
}

interface FormState {
  provider_type: ProviderType;
  name: string;
  model: string;
  context_length: string; // string in form, parsed to int on submit
  base_url: string;
  api_key: string;
  org_id: string;
}

const EMPTY_FORM: FormState = {
  provider_type: "openai",
  name: "",
  model: "",
  context_length: "",
  base_url: "",
  api_key: "",
  org_id: "",
};

export default function ProviderModal({
  initial,
  onClose,
  onSaved,
}: ProviderModalProps): JSX.Element {
  const api = useProviderApi();
  const isEdit = Boolean(initial);

  const [form, setForm] = useState<FormState>(() =>
    initial
      ? {
          provider_type: initial.provider_type,
          name: initial.name,
          model: initial.model,
          context_length: String(initial.context_length),
          base_url: initial.base_url ?? "",
          api_key: "",
          org_id: "",
        }
      : EMPTY_FORM,
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] =
    useState<ConnectionTestResult | null>(null);
  const [testing, setTesting] = useState(false);

  useEffect(() => {
    setTestResult(null);
    setError(null);
  }, [form.provider_type]);

  const showApiKey = form.provider_type !== "vllm" || isEdit || true;
  const showBaseUrl =
    form.provider_type === "vllm" || form.provider_type === "openai";
  const showOrgId = form.provider_type === "openai";

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));

  const buildCreatePayload = (): ProviderCreatePayload => {
    const base: ProviderCreatePayload = {
      provider_type: form.provider_type,
      name: form.name.trim(),
      model: form.model.trim(),
      context_length: Number.parseInt(form.context_length, 10),
    };
    if (showBaseUrl && form.base_url.trim()) {
      base.base_url = form.base_url.trim();
    }
    if (form.api_key.trim()) {
      base.api_key = form.api_key.trim();
    }
    if (showOrgId && form.org_id.trim()) {
      base.org_id = form.org_id.trim();
    }
    return base;
  };

  const buildUpdatePayload = (): ProviderUpdatePayload => {
    if (!initial) return {};
    const patch: ProviderUpdatePayload = {};
    if (form.name.trim() !== initial.name) patch.name = form.name.trim();
    if (form.model.trim() !== initial.model) patch.model = form.model.trim();
    const ctx = Number.parseInt(form.context_length, 10);
    if (Number.isFinite(ctx) && ctx !== initial.context_length) {
      patch.context_length = ctx;
    }
    if (
      showBaseUrl &&
      form.base_url.trim() !== (initial.base_url ?? "")
    ) {
      patch.base_url = form.base_url.trim();
    }
    // R4.6 — only include api_key when the operator typed a fresh value.
    if (form.api_key.trim()) {
      patch.api_key = form.api_key.trim();
    }
    if (showOrgId && form.org_id.trim()) {
      patch.org_id = form.org_id.trim();
    }
    return patch;
  };

  const submit = async () => {
    setError(null);
    setSubmitting(true);
    try {
      if (initial) {
        await api.update(initial.id, buildUpdatePayload());
      } else {
        await api.create(buildCreatePayload());
      }
      onSaved();
      onClose();
    } catch (exc) {
      setError(formatError(exc));
    } finally {
      setSubmitting(false);
    }
  };

  const runTest = async () => {
    setError(null);
    setTesting(true);
    try {
      const result = await api.testUnsaved(buildCreatePayload());
      setTestResult(result);
    } catch (exc) {
      setError(formatError(exc));
    } finally {
      setTesting(false);
    }
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/40"
      data-testid="llm-provider-modal"
    >
      <div className="w-full max-w-lg rounded bg-white p-6 shadow-lg">
        <header className="mb-4 flex items-start justify-between">
          <h2 className="text-lg font-semibold">
            {isEdit ? `Edit provider — ${initial?.name}` : "Add provider"}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="text-gray-500 hover:text-gray-900"
            aria-label="Close"
          >
            ×
          </button>
        </header>

        <div className="grid grid-cols-1 gap-3">
          <label className="grid grid-cols-3 items-center gap-3">
            <span className="text-sm font-medium">Provider type</span>
            <select
              className="col-span-2 rounded border border-gray-300 px-2 py-1"
              value={form.provider_type}
              onChange={(e) =>
                set("provider_type", e.target.value as ProviderType)
              }
              disabled={isEdit}
              data-testid="llm-provider-type"
            >
              <option value="vllm">vLLM (self-hosted)</option>
              <option value="openai">OpenAI</option>
              <option value="anthropic">Anthropic</option>
              <option value="gemini">Google Gemini</option>
            </select>
          </label>

          <label className="grid grid-cols-3 items-center gap-3">
            <span className="text-sm font-medium">Name</span>
            <input
              type="text"
              className="col-span-2 rounded border border-gray-300 px-2 py-1"
              value={form.name}
              onChange={(e) => set("name", e.target.value)}
              data-testid="llm-provider-name"
            />
          </label>

          <label className="grid grid-cols-3 items-center gap-3">
            <span className="text-sm font-medium">Model</span>
            <input
              type="text"
              className="col-span-2 rounded border border-gray-300 px-2 py-1"
              value={form.model}
              onChange={(e) => set("model", e.target.value)}
              data-testid="llm-provider-model"
            />
          </label>

          <label className="grid grid-cols-3 items-center gap-3">
            <span className="text-sm font-medium">Context length</span>
            <input
              type="number"
              className="col-span-2 rounded border border-gray-300 px-2 py-1"
              value={form.context_length}
              min={1}
              onChange={(e) => set("context_length", e.target.value)}
              data-testid="llm-provider-context-length"
            />
          </label>

          {showBaseUrl ? (
            <label className="grid grid-cols-3 items-center gap-3">
              <span className="text-sm font-medium">Base URL</span>
              <input
                type="url"
                className="col-span-2 rounded border border-gray-300 px-2 py-1"
                value={form.base_url}
                placeholder={
                  form.provider_type === "vllm"
                    ? "http://vllm:8000"
                    : "https://api.openai.com (default)"
                }
                onChange={(e) => set("base_url", e.target.value)}
                data-testid="llm-provider-base-url"
              />
            </label>
          ) : null}

          {showApiKey ? (
            <label className="grid grid-cols-3 items-start gap-3">
              <span className="text-sm font-medium">API key</span>
              <div className="col-span-2">
                <input
                  type="password"
                  className="w-full rounded border border-gray-300 px-2 py-1"
                  value={form.api_key}
                  onChange={(e) => set("api_key", e.target.value)}
                  data-testid="llm-provider-api-key"
                />
                {isEdit ? (
                  <p
                    className="mt-1 text-xs text-gray-500"
                    data-testid="llm-provider-api-key-helper"
                  >
                    Mevcut anahtar: {initial?.api_key_masked} — değiştirmek
                    için yeni değer girin
                  </p>
                ) : null}
              </div>
            </label>
          ) : null}

          {showOrgId ? (
            <label className="grid grid-cols-3 items-center gap-3">
              <span className="text-sm font-medium">Organization</span>
              <input
                type="text"
                className="col-span-2 rounded border border-gray-300 px-2 py-1"
                value={form.org_id}
                onChange={(e) => set("org_id", e.target.value)}
                data-testid="llm-provider-org-id"
              />
            </label>
          ) : null}
        </div>

        <div className="mt-5 flex items-center gap-3">
          <button
            type="button"
            className={
              "rounded border border-blue-500 bg-blue-50 px-3 py-1 text-sm " +
              "text-blue-700 hover:bg-blue-100"
            }
            onClick={runTest}
            disabled={testing || submitting}
            data-testid="llm-provider-test-button"
          >
            {testing ? "Testing…" : "Test Connection"}
          </button>
          {testResult ? <TestResultBadge result={testResult} /> : null}
        </div>

        {error ? (
          <p
            className="mt-3 rounded bg-red-50 px-3 py-2 text-sm text-red-700"
            data-testid="llm-provider-modal-error"
          >
            {error}
          </p>
        ) : null}

        <footer className="mt-6 flex justify-end gap-2">
          <button
            type="button"
            className="rounded border border-gray-300 px-3 py-1 text-sm"
            onClick={onClose}
            disabled={submitting}
          >
            Cancel
          </button>
          <button
            type="button"
            className={
              "rounded bg-blue-600 px-3 py-1 text-sm text-white " +
              "hover:bg-blue-700 disabled:opacity-50"
            }
            onClick={submit}
            disabled={submitting}
            data-testid="llm-provider-save-button"
          >
            {submitting ? "Saving…" : "Save"}
          </button>
        </footer>
      </div>
    </div>
  );
}

function formatError(exc: unknown): string {
  if (exc instanceof ApiError) {
    const body = exc.body as { error?: string; detail?: unknown };
    if (body && typeof body === "object" && body.error) {
      return `${body.error} (HTTP ${exc.status})`;
    }
    return `HTTP ${exc.status}`;
  }
  if (exc instanceof Error) return exc.message;
  return String(exc);
}
