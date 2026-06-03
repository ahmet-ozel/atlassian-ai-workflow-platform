// Feature: llm-provider-management, Property: ProviderModal field visibility + api_key omit
//
// Property tests for the modal's per-provider_type field visibility
// and the credential-omit invariant
// on edit: empty api_key input → PUT body omits the
// field so the backend service preserves the persisted credential).

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import fc from "fast-check";


// ---------------------------------------------------------------------------
// Pure decision logic — mirrors ProviderModal.tsx's visibility table.
// ---------------------------------------------------------------------------

/**
 * Returns the set of input field names visible for *providerType*.
 *
 * The table mirrors the design's "Provider_Schema" rows:
 *   vllm      → base_url (required), api_key (optional)
 *   openai    → api_key (required), org_id (optional), base_url (optional)
 *   anthropic → api_key (required)
 *   gemini    → api_key (required)
 *
 * `name`, `model` and `context_length` are common to every variant
 * and are always visible — they're omitted from the per-type set so
 * the test stays focused on the variants.
 */
function visibleFields(providerType) {
  if (providerType === "vllm") {
    return new Set(["base_url", "api_key"]);
  }
  if (providerType === "openai") {
    return new Set(["api_key", "org_id", "base_url"]);
  }
  // anthropic / gemini
  return new Set(["api_key"]);
}

/**
 * Builds the PUT body for the edit form.
 *
 * The form omits `api_key` when the input is empty so the backend
 * service merges only the fields the operator actually changed.
 */
function buildEditPutBody(form, initial) {
  const patch = {};
  if (form.name.trim() !== initial.name) patch.name = form.name.trim();
  if (form.model.trim() !== initial.model) patch.model = form.model.trim();
  const ctx = Number.parseInt(form.context_length, 10);
  if (Number.isFinite(ctx) && ctx !== initial.context_length) {
    patch.context_length = ctx;
  }
  if (form.api_key.trim()) {
    patch.api_key = form.api_key.trim();
  }
  if (form.org_id.trim()) {
    patch.org_id = form.org_id.trim();
  }
  return patch;
}


// ---------------------------------------------------------------------------
// Property tests
// ---------------------------------------------------------------------------

describe("ProviderModal field visibility", () => {
  it("each provider_type exposes exactly its documented fields", () => {
    fc.assert(
      fc.property(
        fc.constantFrom("vllm", "openai", "anthropic", "gemini"),
        (providerType) => {
          const visible = visibleFields(providerType);
          if (providerType === "vllm") {
            assert.ok(visible.has("base_url"));
            assert.ok(visible.has("api_key"));
            assert.ok(!visible.has("org_id"));
          } else if (providerType === "openai") {
            assert.ok(visible.has("api_key"));
            assert.ok(visible.has("org_id"));
            assert.ok(visible.has("base_url"));
          } else {
            assert.ok(visible.has("api_key"));
            assert.ok(!visible.has("org_id"));
            assert.ok(!visible.has("base_url"));
          }
        },
      ),
      { numRuns: 100 },
    );
  });

  it("empty api_key on edit produces a PUT body WITHOUT api_key", () => {
    const initial = {
      name: "My OpenAI",
      model: "gpt-4o-mini",
      context_length: 128000,
    };
    fc.assert(
      fc.property(
        fc.record({
          name: fc.string({ minLength: 1, maxLength: 20 }),
          model: fc.string({ minLength: 1, maxLength: 40 }),
          context_length: fc.integer({ min: 1, max: 1_000_000 }).map(String),
          // The form always has api_key as an empty string on edit when
          // the operator does NOT rotate it.
          api_key: fc.constant(""),
          org_id: fc.constant(""),
        }),
        (form) => {
          const body = buildEditPutBody(form, initial);
          assert.ok(!("api_key" in body));
        },
      ),
      { numRuns: 100 },
    );
  });

  it("non-empty api_key on edit produces a PUT body WITH api_key", () => {
    const initial = {
      name: "My OpenAI",
      model: "gpt-4o-mini",
      context_length: 128000,
    };
    fc.assert(
      fc.property(
        fc.string({ minLength: 1, maxLength: 60 }),
        (rotated) => {
          const form = {
            name: initial.name,
            model: initial.model,
            context_length: String(initial.context_length),
            api_key: rotated,
            org_id: "",
          };
          const body = buildEditPutBody(form, initial);
          if (rotated.trim()) {
            assert.equal(body.api_key, rotated.trim());
          } else {
            assert.ok(!("api_key" in body));
          }
        },
      ),
      { numRuns: 100 },
    );
  });
});
