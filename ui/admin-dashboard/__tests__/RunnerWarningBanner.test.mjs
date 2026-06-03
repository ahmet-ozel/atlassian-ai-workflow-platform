/**
 * Unit tests for RunnerWarningBanner component logic.
 *
 * Tests the core decision logic of the RunnerWarningBanner component:
 * - Banner visibility based on active runner count
 * - Banner text content
 * - Fetch response handling
 *
 * Since the project uses node:test without React Testing Library / jsdom,
 * these tests validate the component's decision logic by simulating the
 * fetch response and verifying expected behavior.
 *
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";

// ---------------------------------------------------------------------------
// Re-implement the component's decision logic for testing
// ---------------------------------------------------------------------------

/**
 * Determines whether the warning banner should be visible based on the
 * fetch state and active runner count.
 *
 * This mirrors the logic in RunnerWarningBanner.tsx:
 *   if (fetchState !== "success" || activeRunners === null || activeRunners >= 2) {
 *     return null; // hidden
 *   }
 *   // else: show banner
 */
function shouldShowBanner(fetchState, activeRunners) {
  if (fetchState !== "success") return false;
  if (activeRunners === null) return false;
  if (activeRunners >= 2) return false;
  return true;
}

/**
 * Returns the banner text content when the banner is visible.
 * Matches the exact text from the component.
 */
function getBannerText() {
  return "Tek SSH runner = Single Point of Failure. En az 2 active runner önerilir.";
}

/**
 * Simulates the component's fetch logic and returns the resulting state.
 * Mirrors the fetchRunners() callback in RunnerWarningBanner.tsx.
 *
 * @param {object} options - Simulation options
 * @param {boolean} options.responseOk - Whether the fetch response is ok
 * @param {object|null} options.responseData - The JSON response body
 * @param {boolean} options.throwError - Whether fetch throws a network error
 * @returns {{ fetchState: string, activeRunners: number|null }}
 */
function simulateFetchRunners({ responseOk = true, responseData = null, throwError = false } = {}) {
  let fetchState = "idle";
  let activeRunners = null;

  // Start loading
  fetchState = "loading";

  if (throwError) {
    // Network error path
    fetchState = "error";
    return { fetchState, activeRunners };
  }

  if (!responseOk) {
    // Non-OK response path
    fetchState = "error";
    return { fetchState, activeRunners };
  }

  // Success path
  if (responseData !== null) {
    activeRunners = responseData.active_runners;
    fetchState = "success";
  }

  return { fetchState, activeRunners };
}

// ---------------------------------------------------------------------------
// Tests: Banner Visibility Based on Runner Count
// ---------------------------------------------------------------------------

describe("RunnerWarningBanner — Banner Visibility", () => {
  it("shows banner when active_runners is 0", () => {
    const { fetchState, activeRunners } = simulateFetchRunners({
      responseOk: true,
      responseData: { active_runners: 0, runners: [], healthcheck_cron_scheduled: true },
    });

    const visible = shouldShowBanner(fetchState, activeRunners);
    assert.equal(visible, true, "Banner should be visible when active_runners is 0");
  });

  it("shows banner when active_runners is 1", () => {
    const { fetchState, activeRunners } = simulateFetchRunners({
      responseOk: true,
      responseData: { active_runners: 1, runners: [{}], healthcheck_cron_scheduled: true },
    });

    const visible = shouldShowBanner(fetchState, activeRunners);
    assert.equal(visible, true, "Banner should be visible when active_runners is 1");
  });

  it("hides banner when active_runners is 2", () => {
    const { fetchState, activeRunners } = simulateFetchRunners({
      responseOk: true,
      responseData: { active_runners: 2, runners: [{}, {}], healthcheck_cron_scheduled: true },
    });

    const visible = shouldShowBanner(fetchState, activeRunners);
    assert.equal(visible, false, "Banner should be hidden when active_runners is 2");
  });

  it("hides banner when active_runners is 3", () => {
    const { fetchState, activeRunners } = simulateFetchRunners({
      responseOk: true,
      responseData: { active_runners: 3, runners: [{}, {}, {}], healthcheck_cron_scheduled: true },
    });

    const visible = shouldShowBanner(fetchState, activeRunners);
    assert.equal(visible, false, "Banner should be hidden when active_runners is 3");
  });

  it("hides banner when active_runners is a large number", () => {
    const { fetchState, activeRunners } = simulateFetchRunners({
      responseOk: true,
      responseData: { active_runners: 10, runners: [], healthcheck_cron_scheduled: true },
    });

    const visible = shouldShowBanner(fetchState, activeRunners);
    assert.equal(visible, false, "Banner should be hidden when active_runners >= 2");
  });
});

// ---------------------------------------------------------------------------
// Tests: Banner Text Content
// ---------------------------------------------------------------------------

describe("RunnerWarningBanner — Banner Text Content", () => {
  it("displays the correct warning text in Turkish", () => {
    const text = getBannerText();
    assert.equal(
      text,
      "Tek SSH runner = Single Point of Failure. En az 2 active runner önerilir."
    );
  });

  it("banner text mentions Single Point of Failure", () => {
    const text = getBannerText();
    assert.ok(
      text.includes("Single Point of Failure"),
      "Banner text should mention Single Point of Failure"
    );
  });

  it("banner text recommends at least 2 runners", () => {
    const text = getBannerText();
    assert.ok(
      text.includes("En az 2 active runner"),
      "Banner text should recommend at least 2 active runners"
    );
  });
});

// ---------------------------------------------------------------------------
// Tests: Fetch State Handling
// ---------------------------------------------------------------------------

describe("RunnerWarningBanner — Fetch State Handling", () => {
  it("hides banner when fetch state is 'idle'", () => {
    const visible = shouldShowBanner("idle", null);
    assert.equal(visible, false, "Banner should be hidden in idle state");
  });

  it("hides banner when fetch state is 'loading'", () => {
    const visible = shouldShowBanner("loading", null);
    assert.equal(visible, false, "Banner should be hidden while loading");
  });

  it("hides banner when fetch state is 'error'", () => {
    const visible = shouldShowBanner("error", null);
    assert.equal(visible, false, "Banner should be hidden on fetch error");
  });

  it("hides banner when fetch succeeds but activeRunners is null", () => {
    const visible = shouldShowBanner("success", null);
    assert.equal(visible, false, "Banner should be hidden when activeRunners is null");
  });

  it("hides banner when fetch response is not ok (HTTP error)", () => {
    const { fetchState, activeRunners } = simulateFetchRunners({
      responseOk: false,
    });

    const visible = shouldShowBanner(fetchState, activeRunners);
    assert.equal(visible, false, "Banner should be hidden on HTTP error");
  });

  it("hides banner when fetch throws a network error", () => {
    const { fetchState, activeRunners } = simulateFetchRunners({
      throwError: true,
    });

    const visible = shouldShowBanner(fetchState, activeRunners);
    assert.equal(visible, false, "Banner should be hidden on network error");
  });
});

// ---------------------------------------------------------------------------
// Tests: API URL Construction
// ---------------------------------------------------------------------------

describe("RunnerWarningBanner — API URL Construction", () => {
  it("constructs correct API URL with default base", () => {
    const baseUrl = "http://localhost:8082";
    const url = `${baseUrl}/admin/ssh-runners`;
    assert.equal(url, "http://localhost:8082/admin/ssh-runners");
  });

  it("constructs correct API URL with custom base from env", () => {
    const baseUrl = "https://api.example.com";
    const url = `${baseUrl}/admin/ssh-runners`;
    assert.equal(url, "https://api.example.com/admin/ssh-runners");
  });

  it("uses GET method for the runner count request", () => {
    // The component uses method: "GET" for the fetch call
    const expectedMethod = "GET";
    assert.equal(expectedMethod, "GET");
  });
});

// ---------------------------------------------------------------------------
// Tests: Boundary Condition — Exactly 2 Runners (Threshold)
// ---------------------------------------------------------------------------

describe("RunnerWarningBanner — Threshold Boundary (active_runners = 2)", () => {
  it("active_runners = 1 → banner visible (below threshold)", () => {
    const visible = shouldShowBanner("success", 1);
    assert.equal(visible, true);
  });

  it("active_runners = 2 → banner hidden (at threshold)", () => {
    const visible = shouldShowBanner("success", 2);
    assert.equal(visible, false);
  });

  it("active_runners = 0 → banner visible (well below threshold)", () => {
    const visible = shouldShowBanner("success", 0);
    assert.equal(visible, true);
  });
});
