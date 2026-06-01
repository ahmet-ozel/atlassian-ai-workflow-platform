/**
 * Unit tests for the capability matrix data layer (`platform-gap-fill`
 * task 9.2 / Requirement 10.6).
 *
 * The admin-dashboard UI does not currently bundle vitest / jest, so
 * these tests use Node's built-in :mod:`node:test` runner. They are
 * runnable from the package root with::
 *
 *     npm test
 *
 * which the package.json wires up to ``node --test app/**\/*.test.mjs``.
 *
 * The fixture below is a hand-rolled sample that mirrors the JSON
 * shape of ``GET /api/v1/departments/capabilities`` (validated by
 * the Python-side integration tests under
 * ``platform/services/admin-dashboard-api/tests/unit/test_capabilities_router.py``).
 * Keeping the contract pinned in both languages catches breakage
 * fast — if the router renames a field, one of these tests fails.
 */

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import {
  AUTO_REFRESH_INTERVAL_MS,
  STATUS_LABEL,
  STATUS_TO_COLOR,
  SUPPORTED_SERVICES,
  applyCellUpdate,
  formatLatency,
  parseMatrix,
  parseProbeCell,
  statusColor,
} from "./matrix.mjs";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

/**
 * Representative payload — three departments, every service cell
 * populated, exercising healthy / unhealthy / not_configured /
 * unknown branches simultaneously.
 */
const SAMPLE_PAYLOAD = Object.freeze({
  supported_services: ["jira", "bitbucket", "confluence", "llm", "ssh", "docker"],
  departments: [
    {
      dept_id: "payment",
      display_name: "Payment",
      services: {
        jira: {
          dept_id: "payment",
          service: "jira",
          status: "healthy",
          error: null,
          latency_ms: 42,
          probed_at: "2025-01-02T03:04:00+00:00",
        },
        bitbucket: {
          dept_id: "payment",
          service: "bitbucket",
          status: "unhealthy",
          error: "401 Unauthorized",
          latency_ms: 180,
          probed_at: "2025-01-02T03:04:05+00:00",
        },
        confluence: {
          dept_id: "payment",
          service: "confluence",
          status: "healthy",
          error: null,
          latency_ms: 210,
          probed_at: "2025-01-02T03:04:10+00:00",
        },
        llm: {
          dept_id: "payment",
          service: "llm",
          status: "unknown",
          error: null,
          latency_ms: null,
          probed_at: null,
        },
        ssh: {
          dept_id: "payment",
          service: "ssh",
          status: "not_configured",
          error: null,
          latency_ms: null,
          probed_at: null,
        },
        docker: {
          dept_id: "payment",
          service: "docker",
          status: "not_configured",
          error: null,
          latency_ms: null,
          probed_at: null,
        },
      },
    },
    {
      dept_id: "hr",
      display_name: "HR",
      services: {
        jira: {
          dept_id: "hr",
          service: "jira",
          status: "healthy",
          error: null,
          latency_ms: 88,
          probed_at: "2025-01-02T03:05:00+00:00",
        },
        bitbucket: {
          dept_id: "hr",
          service: "bitbucket",
          status: "not_configured",
          error: null,
          latency_ms: null,
          probed_at: null,
        },
        confluence: {
          dept_id: "hr",
          service: "confluence",
          status: "healthy",
          error: null,
          latency_ms: 150,
          probed_at: "2025-01-02T03:05:05+00:00",
        },
        llm: {
          dept_id: "hr",
          service: "llm",
          status: "not_configured",
          error: null,
          latency_ms: null,
          probed_at: null,
        },
        ssh: {
          dept_id: "hr",
          service: "ssh",
          status: "not_configured",
          error: null,
          latency_ms: null,
          probed_at: null,
        },
        docker: {
          dept_id: "hr",
          service: "docker",
          status: "not_configured",
          error: null,
          latency_ms: null,
          probed_at: null,
        },
      },
    },
    {
      dept_id: "legal",
      display_name: "Legal",
      services: {
        jira: {
          dept_id: "legal",
          service: "jira",
          status: "unknown",
          error: null,
          latency_ms: null,
          probed_at: null,
        },
        bitbucket: {
          dept_id: "legal",
          service: "bitbucket",
          status: "not_configured",
          error: null,
          latency_ms: null,
          probed_at: null,
        },
        confluence: {
          dept_id: "legal",
          service: "confluence",
          status: "unknown",
          error: null,
          latency_ms: null,
          probed_at: null,
        },
        llm: {
          dept_id: "legal",
          service: "llm",
          status: "not_configured",
          error: null,
          latency_ms: null,
          probed_at: null,
        },
        ssh: {
          dept_id: "legal",
          service: "ssh",
          status: "not_configured",
          error: null,
          latency_ms: null,
          probed_at: null,
        },
        docker: {
          dept_id: "legal",
          service: "docker",
          status: "not_configured",
          error: null,
          latency_ms: null,
          probed_at: null,
        },
      },
    },
  ],
});

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

describe("matrix constants", () => {
  it("SUPPORTED_SERVICES matches the API contract", () => {
    assert.deepEqual(
      [...SUPPORTED_SERVICES],
      ["jira", "bitbucket", "confluence", "llm", "ssh", "docker"],
    );
  });

  it("STATUS_TO_COLOR collapses not_configured + unknown onto grey", () => {
    assert.equal(STATUS_TO_COLOR.healthy, "green");
    assert.equal(STATUS_TO_COLOR.unhealthy, "red");
    assert.equal(STATUS_TO_COLOR.not_configured, "grey");
    assert.equal(STATUS_TO_COLOR.unknown, "grey");
  });

  it("STATUS_LABEL provides Turkish labels for every status", () => {
    for (const status of ["healthy", "unhealthy", "not_configured", "unknown"]) {
      const label = STATUS_LABEL[status];
      assert.equal(typeof label, "string");
      assert.ok(label.length > 0, `missing label for status=${status}`);
    }
  });

  it("AUTO_REFRESH_INTERVAL_MS is 10 minutes", () => {
    assert.equal(AUTO_REFRESH_INTERVAL_MS, 10 * 60 * 1000);
  });
});

// ---------------------------------------------------------------------------
// parseMatrix
// ---------------------------------------------------------------------------

describe("parseMatrix", () => {
  it("returns the full matrix for a valid payload", () => {
    const matrix = parseMatrix(SAMPLE_PAYLOAD);

    assert.deepEqual(matrix.supported_services, [
      "jira",
      "bitbucket",
      "confluence",
      "llm",
      "ssh",
      "docker",
    ]);

    assert.equal(matrix.departments.length, 3);
    const ids = matrix.departments.map((d) => d.dept_id);
    assert.deepEqual(ids.sort(), ["hr", "legal", "payment"]);

    const payment = matrix.departments.find((d) => d.dept_id === "payment");
    assert.ok(payment, "payment department missing");
    assert.equal(payment.display_name, "Payment");

    // Every service from supported_services is present.
    for (const svc of SUPPORTED_SERVICES) {
      assert.ok(svc in payment.services, `payment missing service=${svc}`);
    }

    // Spot-check translated cells.
    assert.equal(payment.services.jira.status, "healthy");
    assert.equal(payment.services.bitbucket.status, "unhealthy");
    assert.equal(payment.services.bitbucket.error, "401 Unauthorized");
    assert.equal(payment.services.bitbucket.latency_ms, 180);
    assert.equal(payment.services.ssh.status, "not_configured");
    assert.equal(payment.services.llm.status, "unknown");
  });

  it("rejects a non-object payload", () => {
    assert.throws(() => parseMatrix(null), /must be an object/);
    assert.throws(() => parseMatrix("nope"), /must be an object/);
    assert.throws(() => parseMatrix(42), /must be an object/);
  });

  it("rejects a payload missing supported_services", () => {
    const bad = { departments: [] };
    assert.throws(() => parseMatrix(bad), /supported_services/);
  });

  it("rejects a payload missing departments", () => {
    const bad = { supported_services: ["jira"] };
    assert.throws(() => parseMatrix(bad), /departments/);
  });

  it("rejects a department missing a declared service cell", () => {
    const bad = {
      supported_services: ["jira", "bitbucket"],
      departments: [
        {
          dept_id: "payment",
          display_name: "Payment",
          services: {
            jira: {
              dept_id: "payment",
              service: "jira",
              status: "healthy",
              error: null,
              latency_ms: null,
              probed_at: null,
            },
            // 'bitbucket' missing on purpose.
          },
        },
      ],
    };
    assert.throws(
      () => parseMatrix(bad),
      /missing service cell for 'bitbucket'/,
    );
  });

  it("rejects a probe cell with an unknown status", () => {
    const bad = {
      supported_services: ["jira"],
      departments: [
        {
          dept_id: "payment",
          display_name: "Payment",
          services: {
            jira: {
              dept_id: "payment",
              service: "jira",
              status: "wat",
              error: null,
              latency_ms: null,
              probed_at: null,
            },
          },
        },
      ],
    };
    assert.throws(() => parseMatrix(bad), /unknown status/);
  });

  it("rejects a department missing dept_id", () => {
    const bad = {
      supported_services: ["jira"],
      departments: [
        {
          // dept_id missing
          display_name: "Payment",
          services: {},
        },
      ],
    };
    assert.throws(() => parseMatrix(bad), /dept_id/);
  });

  it("ignores unexpected top-level keys (forward compat)", () => {
    const extra = {
      ...SAMPLE_PAYLOAD,
      future_key: "ignore me",
    };
    // Should not throw — extra keys are tolerated.
    const matrix = parseMatrix(extra);
    assert.equal(matrix.departments.length, 3);
  });
});

// ---------------------------------------------------------------------------
// parseProbeCell
// ---------------------------------------------------------------------------

describe("parseProbeCell", () => {
  it("parses a fresh single-probe response", () => {
    const raw = {
      dept_id: "payment",
      service: "jira",
      status: "healthy",
      error: null,
      latency_ms: 42,
      probed_at: "2025-02-01T00:00:00+00:00",
    };
    const cell = parseProbeCell(raw, "payment", "jira");
    assert.equal(cell.dept_id, "payment");
    assert.equal(cell.service, "jira");
    assert.equal(cell.status, "healthy");
    assert.equal(cell.latency_ms, 42);
    assert.equal(cell.probed_at, "2025-02-01T00:00:00+00:00");
  });

  it("falls back to expected dept_id / service when missing", () => {
    const raw = {
      status: "unhealthy",
      error: "boom",
    };
    const cell = parseProbeCell(raw, "hr", "ssh");
    assert.equal(cell.dept_id, "hr");
    assert.equal(cell.service, "ssh");
    assert.equal(cell.status, "unhealthy");
    assert.equal(cell.error, "boom");
    assert.equal(cell.latency_ms, null);
    assert.equal(cell.probed_at, null);
  });

  it("rejects an unknown status", () => {
    assert.throws(
      () => parseProbeCell({ status: "fubar" }, "hr", "ssh"),
      /unknown status/,
    );
  });
});

// ---------------------------------------------------------------------------
// statusColor / formatLatency
// ---------------------------------------------------------------------------

describe("statusColor", () => {
  it("maps every known status to its colour bucket", () => {
    assert.equal(statusColor("healthy"), "green");
    assert.equal(statusColor("unhealthy"), "red");
    assert.equal(statusColor("not_configured"), "grey");
    assert.equal(statusColor("unknown"), "grey");
  });

  it("falls back to grey for unexpected values", () => {
    assert.equal(statusColor("anything-else"), "grey");
  });
});

describe("formatLatency", () => {
  it("renders sub-second latency in ms", () => {
    assert.equal(formatLatency(0), "0ms");
    assert.equal(formatLatency(42), "42ms");
    assert.equal(formatLatency(999), "999ms");
  });

  it("renders ≥1s latency as seconds with one decimal", () => {
    assert.equal(formatLatency(1000), "1.0s");
    assert.equal(formatLatency(1234), "1.2s");
    assert.equal(formatLatency(12500), "12.5s");
  });

  it("renders missing latency as em-dash", () => {
    assert.equal(formatLatency(null), "—");
    assert.equal(formatLatency(undefined), "—");
    assert.equal(formatLatency(Number.NaN), "—");
  });
});

// ---------------------------------------------------------------------------
// applyCellUpdate
// ---------------------------------------------------------------------------

describe("applyCellUpdate", () => {
  it("replaces a single cell without mutating input", () => {
    const matrix = parseMatrix(SAMPLE_PAYLOAD);

    const updated = parseProbeCell(
      {
        dept_id: "payment",
        service: "bitbucket",
        status: "healthy",
        error: null,
        latency_ms: 50,
        probed_at: "2025-03-01T00:00:00+00:00",
      },
      "payment",
      "bitbucket",
    );

    const next = applyCellUpdate(matrix, updated);

    // Returned matrix has the new cell.
    const payment = next.departments.find((d) => d.dept_id === "payment");
    assert.ok(payment, "payment dept missing");
    assert.equal(payment.services.bitbucket.status, "healthy");
    assert.equal(payment.services.bitbucket.latency_ms, 50);

    // Other cells unchanged.
    assert.equal(payment.services.jira.status, "healthy");
    const hr = next.departments.find((d) => d.dept_id === "hr");
    assert.ok(hr, "hr dept missing");
    assert.equal(hr.services.jira.status, "healthy");

    // Original matrix is untouched (immutable update).
    const original = matrix.departments.find((d) => d.dept_id === "payment");
    assert.ok(original, "original payment dept missing");
    assert.equal(original.services.bitbucket.status, "unhealthy");
  });

  it("returns the input verbatim when the dept does not exist", () => {
    const matrix = parseMatrix(SAMPLE_PAYLOAD);
    const updated = parseProbeCell(
      {
        dept_id: "no-such-dept",
        service: "jira",
        status: "healthy",
        error: null,
        latency_ms: 10,
        probed_at: null,
      },
      "no-such-dept",
      "jira",
    );
    const next = applyCellUpdate(matrix, updated);
    assert.equal(next, matrix);
  });

  it("returns the input verbatim when the service is not in the matrix", () => {
    const matrix = parseMatrix(SAMPLE_PAYLOAD);
    const updated = parseProbeCell(
      {
        dept_id: "payment",
        service: "made-up-service",
        status: "healthy",
        error: null,
        latency_ms: 10,
        probed_at: null,
      },
      "payment",
      "made-up-service",
    );
    const next = applyCellUpdate(matrix, updated);
    assert.equal(next, matrix);
  });
});
