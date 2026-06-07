/**
 * Per-model tuning-capability lookup (client mirror of the backend
 * `llm_providers/model_capabilities.py`).
 * * Decides which tuning inputs the provider form should reveal for the
 * model the operator typed:
 * *   reasoning_effort - OpenAI o-series + gpt-5 family, Claude 4 /
 * `-thinking` snapshots.
 * verbosity        - OpenAI gpt-5 family only.
 * * The matching is prefix / substring based so dated snapshots
 * (`gpt-5.1-2025-11-01`, `o3-mini-2025-01-31`) resolve to the same
 * profile as their base model.
 */

import type { ProviderType } from "./types";

function normalise(model: string): string {
  return (model ?? "").trim().toLowerCase();
}

function isOpenAiReasoning(model: string): boolean {
  for (const prefix of ["o1", "o3", "o4"]) {
    if (model === prefix || model.startsWith(`${prefix}-`)) {
      return true;
    }
  }
  return model.startsWith("gpt-5");
}

function isAnthropicReasoning(model: string): boolean {
  if (model.includes("thinking")) return true;
  return (
    model.startsWith("claude-opus-4") || model.startsWith("claude-sonnet-4")
  );
}

/**
 * Whether *model* (on *providerType*) accepts a `reasoning_effort` knob.
 * * Only OpenAI and Anthropic expose reasoning effort; vLLM / Gemini
 * never surface the input here.
 */
export function supportsReasoningEffort(
  providerType: ProviderType,
  model: string,
): boolean {
  const norm = normalise(model);
  if (!norm) return false;
  if (providerType === "openai") return isOpenAiReasoning(norm);
  if (providerType === "anthropic") return isAnthropicReasoning(norm);
  return false;
}

/**
 * Whether *model* (on *providerType*) accepts an output `verbosity` knob.
 * * Only the OpenAI gpt-5 family ships the `text.verbosity` control.
 */
export function supportsVerbosity(
  providerType: ProviderType,
  model: string,
): boolean {
  if (providerType !== "openai") return false;
  return normalise(model).startsWith("gpt-5");
}
