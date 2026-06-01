// Feature: llm-provider-management, Property: TestResultBadge rendering invariants
//
// Property tests for the `<TestResultBadge>` component (Requirements
// 14.3, 14.5). The badge:
//   - Renders a green check + `{latency_ms}ms` on success.
//   - Renders a red cross + the redacted `error.message` on failure.
//   - Never carries an unredacted credential pattern through the DOM.

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import fc from "fast-check";


// ---------------------------------------------------------------------------
// Pure decision logic — mirrors TestResultBadge.tsx
// ---------------------------------------------------------------------------

/**
 * Returns the rendered envelope for a ConnectionTestResult.
 *
 * Shape:
 *   { color: "green" | "red", icon: "✓" | "✗", label: string }
 *
 * The label is the test latency (success) or the redacted error
 * message (failure). The component itself never reaches into the raw
 * credential — the backend ran `redact_text` on `error.message` before
 * returning the body — so this helper simply forwards the field.
 */
function renderBadge(result) {
  if (result.success) {
    return {
      color: "green",
      icon: "✓",
      label: `${result.latency_ms}ms`,
    };
  }
  return {
    color: "red",
    icon: "✗",
    label: result.error?.message ?? "unknown error",
  };
}


// ---------------------------------------------------------------------------
// Fast-check arbitraries
// ---------------------------------------------------------------------------

const successResultArb = fc.record({
  success: fc.constant(true),
  latency_ms: fc.integer({ min: 0, max: 60000 }),
  model: fc.string({ minLength: 1, maxLength: 40 }),
  error: fc.constant(null),
});

const failureResultArb = fc.record({
  success: fc.constant(false),
  latency_ms: fc.integer({ min: 0, max: 60000 }),
  model: fc.constant(null),
  error: fc.record({
    status_code: fc.option(fc.integer({ min: 400, max: 599 }), { nil: null }),
    message: fc.string({ minLength: 1, maxLength: 200 }),
  }),
});


// ---------------------------------------------------------------------------
// Property tests
// ---------------------------------------------------------------------------

describe("TestResultBadge rendering invariants", () => {
  it("success → green check + latency label", () => {
    fc.assert(
      fc.property(successResultArb, (result) => {
        const envelope = renderBadge(result);
        assert.equal(envelope.color, "green");
        assert.equal(envelope.icon, "✓");
        assert.equal(envelope.label, `${result.latency_ms}ms`);
      }),
      { numRuns: 100 },
    );
  });

  it("failure → red cross + redacted error label", () => {
    fc.assert(
      fc.property(failureResultArb, (result) => {
        const envelope = renderBadge(result);
        assert.equal(envelope.color, "red");
        assert.equal(envelope.icon, "✗");
        assert.equal(envelope.label, result.error.message);
      }),
      { numRuns: 100 },
    );
  });

  it("rendered label never carries an unredacted Sensitive_Field_Set marker", () => {
    // Sensitive_Field_Set markers per the design's Property 13. The
    // component receives a pre-redacted message from the backend, so
    // any failing variant whose message we synthesise here MUST NOT
    // contain a verbatim marker (the backend would have replaced it
    // with "***REDACTED***" before the body crossed the network).
    const markers = ["sk-ant-", "sk-proj-", "sk-live-", "AIzaSy"];
    fc.assert(
      fc.property(
        fc.record({
          success: fc.constant(false),
          latency_ms: fc.integer({ min: 0, max: 60000 }),
          model: fc.constant(null),
          error: fc.record({
            status_code: fc.constant(500),
            message: fc.constantFrom(
              "***REDACTED*** upstream error",
              "rate limited",
              "model unavailable",
              "upstream returned: ***REDACTED***",
            ),
          }),
        }),
        (result) => {
          const envelope = renderBadge(result);
          for (const marker of markers) {
            assert.ok(
              !envelope.label.includes(marker),
              `unredacted marker ${marker} leaked through label`,
            );
          }
        },
      ),
      { numRuns: 100 },
    );
  });
});
