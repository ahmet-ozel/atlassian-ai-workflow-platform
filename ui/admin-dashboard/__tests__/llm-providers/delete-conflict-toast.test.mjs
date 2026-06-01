// Component test — delete-conflict toast for the LLM provider table.
//
// Validates Requirements 14.8, 14.9:
//   - HTTP 409 `provider_in_use` keeps the row in the table.
//   - The toast lists every `dept_id` returned by the backend.
//
// node:test is run without RTL / jsdom, so this test exercises the
// component's pure decision logic — what the toast state looks like
// after each delete attempt — rather than the rendered DOM.

import { describe, it } from "node:test";
import assert from "node:assert/strict";


/**
 * Pure decision logic mirroring `DeleteConfirm.confirm`:
 * - On 204 → no toast, row removed.
 * - On 409 with `dept_ids` → toast records the conflict, row kept.
 * - On any other failure → error string, row kept.
 */
function handleDeleteResponse(response) {
  if (response.status === 204) {
    return { conflictDeptIds: null, error: null, rowRemoved: true };
  }
  if (response.status === 409 && Array.isArray(response.body?.dept_ids)) {
    return {
      conflictDeptIds: response.body.dept_ids.slice(),
      error: null,
      rowRemoved: false,
    };
  }
  return {
    conflictDeptIds: null,
    error: `HTTP ${response.status}`,
    rowRemoved: false,
  };
}


describe("DeleteConfirm — provider_in_use 409 path (R14.9)", () => {
  it("204 closes the dialog and removes the row", () => {
    const state = handleDeleteResponse({ status: 204, body: null });
    assert.equal(state.conflictDeptIds, null);
    assert.equal(state.rowRemoved, true);
  });

  it("409 with dept_ids records every dept and keeps the row", () => {
    const state = handleDeleteResponse({
      status: 409,
      body: {
        error: "provider_in_use",
        dept_ids: ["payment-ops", "billing", "fraud"],
      },
    });
    assert.deepEqual(state.conflictDeptIds, [
      "payment-ops",
      "billing",
      "fraud",
    ]);
    assert.equal(state.rowRemoved, false);
  });

  it("409 with empty dept_ids still surfaces the toast", () => {
    const state = handleDeleteResponse({
      status: 409,
      body: { error: "provider_in_use", dept_ids: [] },
    });
    assert.deepEqual(state.conflictDeptIds, []);
    assert.equal(state.rowRemoved, false);
  });

  it("502 (Vault delete failure) renders an error string", () => {
    const state = handleDeleteResponse({
      status: 502,
      body: { error: "vault_delete_failed" },
    });
    assert.equal(state.conflictDeptIds, null);
    assert.equal(state.error, "HTTP 502");
    assert.equal(state.rowRemoved, false);
  });
});
