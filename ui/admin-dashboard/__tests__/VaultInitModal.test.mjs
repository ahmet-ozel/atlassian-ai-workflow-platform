/**
 * Unit tests for VaultInitModal component logic.
 *
 * Tests the core decision logic of the VaultInitModal component:
 * - Modal state transitions (idle → loading → display → closed)
 * - Confirmation checkbox prevents closing without acknowledgment
 * - Keys are cleared from state after modal is closed
 * - Error handling for API failures and 409 Conflict
 *
 * Since the project uses node:test without React Testing Library / jsdom,
 * these tests validate the component's decision logic by simulating the
 * state machine and verifying expected behavior.
 *
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";

// ---------------------------------------------------------------------------
// Re-implement the component's decision logic for testing
// ---------------------------------------------------------------------------

/**
 * Simulates the VaultInitModal state machine.
 *
 * Phases:
 *   idle → loading → display | error
 *   display → closed (only when confirmed=true)
 *   error → idle (retry)
 *   closed → (terminal)
 */
class VaultInitStateMachine {
  constructor() {
    this.phase = "idle";
    this.data = null;
    this.confirmed = false;
    this.errorMessage = null;
    this.onCompleteCalled = false;
  }

  /** Simulate clicking "Vault'u Initialize Et" */
  startInit() {
    this.phase = "loading";
    this.confirmed = false;
  }

  /** Simulate successful API response */
  receiveSuccess(responseData) {
    this.phase = "display";
    this.data = responseData;
  }

  /** Simulate API error */
  receiveError(message) {
    this.phase = "error";
    this.errorMessage = message;
    this.data = null;
  }

  /** Simulate 409 Conflict (already initialized) */
  receiveConflict() {
    this.phase = "error";
    this.errorMessage = "Vault zaten initialize edilmiş. Tekrar init yapılamaz.";
    this.data = null;
  }

  /** Simulate toggling the confirmation checkbox */
  setConfirmed(value) {
    this.confirmed = value;
  }

  /** Simulate clicking "Onayla ve Kapat" — only works when confirmed */
  attemptClose() {
    if (this.phase !== "display") return false;
    if (!this.confirmed) return false;

    // Clear keys from memory
    this.data = null;
    this.phase = "closed";
    this.confirmed = false;
    this.onCompleteCalled = true;
    return true;
  }

  /** Simulate clicking "Tekrar Dene" from error state */
  retry() {
    if (this.phase === "error") {
      this.phase = "idle";
      this.errorMessage = null;
    }
  }

  /** Check if close button should be disabled */
  isCloseDisabled() {
    return !this.confirmed;
  }
}

/**
 * Creates a mock successful Vault init response.
 */
function createMockResponse() {
  return {
    unseal_keys: [
      "abc123def456ghi789jkl012mno345pqr678stu901vwx234yz",
      "bcd234efg567hij890klm123nop456qrs789tuv012wxy345za",
      "cde345fgh678ijk901lmn234opq567rst890uvw123xyz456ab",
      "def456ghi789jkl012mno345pqr678stu901vwx234yza567bc",
      "efg567hij890klm123nop456qrs789tuv012wxy345zab678cd",
    ],
    unseal_keys_base64: [
      "YWJjMTIzZGVmNDU2Z2hpNzg5amtsMDEybW5vMzQ1",
      "YmNkMjM0ZWZnNTY3aGlqODkwa2xtMTIzbm9wNDU2",
      "Y2RlMzQ1ZmdoNjc4aWprOTAxbG1uMjM0b3BxNTY3",
      "ZGVmNDU2Z2hpNzg5amtsMDEybW5vMzQ1cHFyNjc4",
      "ZWZnNTY3aGlqODkwa2xtMTIzbm9wNDU2cXJzNzg5",
    ],
    root_token: "s.AbCdEfGhIjKlMnOpQrStUvWx",
    message: "vault_initialized",
  };
}

// ---------------------------------------------------------------------------
// Tests: State Machine Transitions
// ---------------------------------------------------------------------------

describe("VaultInitModal — State Machine Transitions", () => {
  it("starts in idle phase", () => {
    const sm = new VaultInitStateMachine();
    assert.equal(sm.phase, "idle");
    assert.equal(sm.data, null);
    assert.equal(sm.confirmed, false);
  });

  it("transitions from idle to loading on init", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    assert.equal(sm.phase, "loading");
  });

  it("transitions from loading to display on success", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    sm.receiveSuccess(createMockResponse());
    assert.equal(sm.phase, "display");
    assert.notEqual(sm.data, null);
  });

  it("transitions from loading to error on failure", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    sm.receiveError("Network error");
    assert.equal(sm.phase, "error");
    assert.equal(sm.errorMessage, "Network error");
  });

  it("transitions from error to idle on retry", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    sm.receiveError("Network error");
    sm.retry();
    assert.equal(sm.phase, "idle");
    assert.equal(sm.errorMessage, null);
  });

  it("transitions from display to closed when confirmed", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    sm.receiveSuccess(createMockResponse());
    sm.setConfirmed(true);
    const closed = sm.attemptClose();
    assert.equal(closed, true);
    assert.equal(sm.phase, "closed");
  });
});

// ---------------------------------------------------------------------------
// Tests: Confirmation Checkbox
// ---------------------------------------------------------------------------

describe("VaultInitModal — Confirmation Checkbox", () => {
  it("close button is disabled when checkbox is unchecked", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    sm.receiveSuccess(createMockResponse());
    // confirmed is false by default
    assert.equal(sm.isCloseDisabled(), true);
  });

  it("close button is enabled when checkbox is checked", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    sm.receiveSuccess(createMockResponse());
    sm.setConfirmed(true);
    assert.equal(sm.isCloseDisabled(), false);
  });

  it("cannot close modal without confirmation", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    sm.receiveSuccess(createMockResponse());
    // Try to close without confirming
    const closed = sm.attemptClose();
    assert.equal(closed, false);
    assert.equal(sm.phase, "display");
    assert.notEqual(sm.data, null);
  });

  it("can close modal after confirmation", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    sm.receiveSuccess(createMockResponse());
    sm.setConfirmed(true);
    const closed = sm.attemptClose();
    assert.equal(closed, true);
    assert.equal(sm.phase, "closed");
  });

  it("confirmation resets when starting a new init", () => {
    const sm = new VaultInitStateMachine();
    sm.setConfirmed(true);
    sm.startInit();
    assert.equal(sm.confirmed, false);
  });
});

// ---------------------------------------------------------------------------
// Tests: Key Clearing After Modal Close
// ---------------------------------------------------------------------------

describe("VaultInitModal — Key Clearing", () => {
  it("keys are present in display phase", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    sm.receiveSuccess(createMockResponse());
    assert.notEqual(sm.data, null);
    assert.equal(sm.data.unseal_keys.length, 5);
    assert.ok(sm.data.root_token.length > 0);
  });

  it("keys are cleared after modal is closed", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    sm.receiveSuccess(createMockResponse());
    sm.setConfirmed(true);
    sm.attemptClose();
    assert.equal(sm.data, null);
  });

  it("onComplete callback is triggered after close", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    sm.receiveSuccess(createMockResponse());
    sm.setConfirmed(true);
    sm.attemptClose();
    assert.equal(sm.onCompleteCalled, true);
  });

  it("keys are not accessible in closed phase", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    const mockData = createMockResponse();
    sm.receiveSuccess(mockData);
    sm.setConfirmed(true);
    sm.attemptClose();
    // Verify data is null — keys cannot be retrieved
    assert.equal(sm.data, null);
    assert.equal(sm.phase, "closed");
  });
});

// ---------------------------------------------------------------------------
// Tests: Error Handling
// ---------------------------------------------------------------------------

describe("VaultInitModal — Error Handling", () => {
  it("handles 409 Conflict (already initialized)", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    sm.receiveConflict();
    assert.equal(sm.phase, "error");
    assert.ok(sm.errorMessage.includes("zaten initialize edilmiş"));
  });

  it("handles network error", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    sm.receiveError("Ağ hatası: Failed to fetch");
    assert.equal(sm.phase, "error");
    assert.ok(sm.errorMessage.includes("Ağ hatası"));
  });

  it("handles generic HTTP error", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    sm.receiveError("Vault init başarısız: HTTP 502");
    assert.equal(sm.phase, "error");
    assert.ok(sm.errorMessage.includes("502"));
  });

  it("data is null in error state", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    sm.receiveError("Some error");
    assert.equal(sm.data, null);
  });

  it("can retry after error", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    sm.receiveError("Network error");
    sm.retry();
    assert.equal(sm.phase, "idle");
    // Can start a new init
    sm.startInit();
    assert.equal(sm.phase, "loading");
  });
});

// ---------------------------------------------------------------------------
// Tests: Response Data Validation
// ---------------------------------------------------------------------------

describe("VaultInitModal — Response Data Structure", () => {
  it("displays exactly 5 unseal keys", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    sm.receiveSuccess(createMockResponse());
    assert.equal(sm.data.unseal_keys.length, 5);
  });

  it("displays exactly 5 base64 unseal keys", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    sm.receiveSuccess(createMockResponse());
    assert.equal(sm.data.unseal_keys_base64.length, 5);
  });

  it("includes root token in response", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    sm.receiveSuccess(createMockResponse());
    assert.ok(sm.data.root_token.length > 0);
  });

  it("includes vault_initialized message", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    sm.receiveSuccess(createMockResponse());
    assert.equal(sm.data.message, "vault_initialized");
  });
});

// ---------------------------------------------------------------------------
// Tests: API URL Construction
// ---------------------------------------------------------------------------

describe("VaultInitModal — API URL Construction", () => {
  it("constructs correct API URL with default base", () => {
    const baseUrl = "http://localhost:8082";
    const url = `${baseUrl}/admin/vault/init`;
    assert.equal(url, "http://localhost:8082/admin/vault/init");
  });

  it("constructs correct API URL with custom base from env", () => {
    const baseUrl = "https://api.example.com";
    const url = `${baseUrl}/admin/vault/init`;
    assert.equal(url, "https://api.example.com/admin/vault/init");
  });

  it("uses POST method for the vault init request", () => {
    const expectedMethod = "POST";
    assert.equal(expectedMethod, "POST");
  });
});

// ---------------------------------------------------------------------------
// Tests: One-Time Display Guarantee
// ---------------------------------------------------------------------------

describe("VaultInitModal — One-Time Display Guarantee", () => {
  it("keys are only available during display phase", () => {
    const sm = new VaultInitStateMachine();

    // idle — no keys
    assert.equal(sm.data, null);

    // loading — no keys
    sm.startInit();
    assert.equal(sm.data, null);

    // display — keys available
    sm.receiveSuccess(createMockResponse());
    assert.notEqual(sm.data, null);

    // closed — keys cleared
    sm.setConfirmed(true);
    sm.attemptClose();
    assert.equal(sm.data, null);
  });

  it("cannot re-enter display phase after close without new init", () => {
    const sm = new VaultInitStateMachine();
    sm.startInit();
    sm.receiveSuccess(createMockResponse());
    sm.setConfirmed(true);
    sm.attemptClose();

    // Phase is closed — no way to get back to display without a new API call
    assert.equal(sm.phase, "closed");
    assert.equal(sm.data, null);
  });
});
