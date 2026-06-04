"use client";

/**
 * Add/Edit provider modal (Requirements 4.5, 4.6, 14.2 — 14.6).
 *
 * Visible fields depend on `provider_type`:
 *
 * - **vllm**:      base_url (required), api_key (required)
 * - **openai**:    api_key (required), org_id (optional), base_url (optional)
 * - **anthropic**: api_key (required)
 *
 * Model-tuning inputs appear only when the entered `model` advertises
 * support for them (mirrors `modelCapabilities.ts`):
 *
 * - **reasoning_effort** (minimal|low|medium|high): OpenAI o-series +
 *   gpt-5 family, Anthropic Claude 4 / `-thinking` snapshots.
 * - **verbosity** (low|medium|high): OpenAI gpt-5 family only.
 *
 * Both default to "" (use the upstream default) and are omitted from
 * the request body when left blank.
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

import { useEffect, useMemo, useState } from "react";

import TestResultBadge from "./TestResultBadge";
import { supportsReasoningEffort, supportsVerbosity } from "./modelCapabilities";
import { useProviderApi, ApiError } from "./useProviderApi";
import type {
  ConnectionTestResult,
  ProviderCreatePayload,
  ProviderRow,
  ProviderType,
  ProviderUpdatePayload,
  ReasoningEffort,
  Verbosity,
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
  reasoning_effort: string; // "" = use upstream default
  verbosity: string; // "" = use upstream default
}

const EMPTY_FORM: FormState = {
  provider_type: "openai",
  name: "",
  model: "",
  context_length: "",
  base_url: "",
  api_key: "",
  org_id: "",
  reasoning_effort: "",
  verbosity: "",
};

const TEST_REQUIRED_MESSAGE =
  "Kaydetmeden once Test Connection basarili olmali.";

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
          reasoning_effort: initial.reasoning_effort ?? "",
          verbosity: initial.verbosity ?? "",
        }
      : EMPTY_FORM,
  );
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testResult, setTestResult] =
    useState<ConnectionTestResult | null>(null);
  const [testing, setTesting] = useState(false);

  const currentTestSignature = useMemo(() => buildTestSignature(form), [form]);
  const [successfulTestSignature, setSuccessfulTestSignature] =
    useState<string | null>(null);

  useEffect(() => {
    setTestResult(null);
    setSuccessfulTestSignature(null);
    setError(null);
  }, [currentTestSignature]);

  const showApiKey = true;
  const showBaseUrl =
    form.provider_type === "vllm" || form.provider_type === "openai";
  const showOrgId = form.provider_type === "openai";
  const showReasoningEffort = supportsReasoningEffort(
    form.provider_type,
    form.model.trim(),
  );
  const showVerbosity = supportsVerbosity(
    form.provider_type,
    form.model.trim(),
  );
  const connectivityChanged =
    !initial ||
    form.model.trim() !== initial.model ||
    (showBaseUrl && form.base_url.trim() !== (initial.base_url ?? "")) ||
    Boolean(form.api_key.trim());
  const requiresFreshTest = !isEdit || connectivityChanged;
  const formError = validateForm(form, {
    isEdit,
    forTest: false,
    connectivityChanged,
  });
  const saveBlockedReason =
    formError ??
    (requiresFreshTest && successfulTestSignature !== currentTestSignature
      ? TEST_REQUIRED_MESSAGE
      : null);

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
    if (showReasoningEffort && form.reasoning_effort) {
      base.reasoning_effort = form.reasoning_effort as ReasoningEffort;
    }
    if (showVerbosity && form.verbosity) {
      base.verbosity = form.verbosity as Verbosity;
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
    if (showReasoningEffort && form.reasoning_effort !== (initial.reasoning_effort ?? "")) {
      if (form.reasoning_effort) {
        patch.reasoning_effort = form.reasoning_effort as ReasoningEffort;
      }
    }
    if (showVerbosity && form.verbosity !== (initial.verbosity ?? "")) {
      if (form.verbosity) {
        patch.verbosity = form.verbosity as Verbosity;
      }
    }
    return patch;
  };

  const submit = async () => {
    setError(null);
    if (saveBlockedReason) {
      setError(saveBlockedReason);
      return;
    }
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
    const validationMessage = validateForm(form, {
      isEdit,
      forTest: true,
      connectivityChanged,
    });
    if (validationMessage) {
      setError(validationMessage);
      return;
    }
    setTesting(true);
    try {
      const result = await api.testUnsaved(buildCreatePayload());
      setTestResult(result);
      if (result.success) {
        setSuccessfulTestSignature(currentTestSignature);
      } else {
        setSuccessfulTestSignature(null);
        setError(
          `Model cevap vermiyor veya credential reddedildi: ${
            result.error?.message ?? "bilinmeyen hata"
          }`,
        );
      }
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
                    ? "http://vllm:8000/v1"
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

          {showReasoningEffort ? (
            <label className="grid grid-cols-3 items-center gap-3">
              <span className="text-sm font-medium">Reasoning effort</span>
              <select
                className="col-span-2 rounded border border-gray-300 px-2 py-1"
                value={form.reasoning_effort}
                onChange={(e) => set("reasoning_effort", e.target.value)}
                data-testid="llm-provider-reasoning-effort"
              >
                <option value="">Varsayılan (model seçsin)</option>
                <option value="minimal">minimal</option>
                <option value="low">low</option>
                <option value="medium">medium</option>
                <option value="high">high</option>
              </select>
            </label>
          ) : null}

          {showVerbosity ? (
            <label className="grid grid-cols-3 items-center gap-3">
              <span className="text-sm font-medium">Verbosity</span>
              <select
                className="col-span-2 rounded border border-gray-300 px-2 py-1"
                value={form.verbosity}
                onChange={(e) => set("verbosity", e.target.value)}
                data-testid="llm-provider-verbosity"
              >
                <option value="">Varsayılan (model seçsin)</option>
                <option value="low">low</option>
                <option value="medium">medium</option>
                <option value="high">high</option>
              </select>
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

        {saveBlockedReason ? (
          <p className="mt-2 text-xs text-gray-500">
            {saveBlockedReason}
          </p>
        ) : null}

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
            disabled={submitting || Boolean(saveBlockedReason)}
            data-testid="llm-provider-save-button"
          >
            {submitting ? "Saving…" : "Save"}
          </button>
        </footer>
      </div>
    </div>
  );
}

function buildTestSignature(form: FormState): string {
  return JSON.stringify({
    provider_type: form.provider_type,
    model: form.model.trim(),
    base_url: form.base_url.trim(),
    api_key: form.api_key.trim(),
    org_id: form.org_id.trim(),
    reasoning_effort: form.reasoning_effort,
    verbosity: form.verbosity,
  });
}

function validateForm(
  form: FormState,
  options: {
    isEdit: boolean;
    forTest: boolean;
    connectivityChanged: boolean;
  },
): string | null {
  if (!form.name.trim()) return "Provider adi zorunlu.";
  if (!form.model.trim()) return "Model name zorunlu.";
  const contextLength = Number.parseInt(form.context_length, 10);
  if (!Number.isFinite(contextLength) || contextLength <= 0) {
    return "Context length pozitif bir sayi olmali.";
  }
  if (form.provider_type === "vllm" && !form.base_url.trim()) {
    return "vLLM icin Base URL zorunlu.";
  }
  const keyRequired =
    !options.isEdit || options.forTest || options.connectivityChanged;
  if (keyRequired && !form.api_key.trim()) {
    if (options.isEdit && options.forTest) {
      return (
        "Kayitli anahtar UI'ya geri gosterilmez; test etmek icin API key'i " +
        "tekrar girin veya satirdaki Test aksiyonunu kullanin."
      );
    }
    if (form.provider_type === "vllm") {
      return "vLLM icin API key zorunlu.";
    }
    if (form.provider_type === "openai") {
      return "OpenAI icin API key zorunlu.";
    }
    if (form.provider_type === "anthropic") {
      return "Anthropic icin API key zorunlu.";
    }
  }
  return null;
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
