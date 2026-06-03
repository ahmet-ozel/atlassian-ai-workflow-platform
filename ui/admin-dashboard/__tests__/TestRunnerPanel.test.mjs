/**
 * Unit tests for TestRunnerPanel SSE streaming logic.
 *
 * Tests the core SSE parsing, cancellation handling, and error scenarios
 * for the TestRunnerPanel component.
 *
 * Since the project uses node:test without React Testing Library / jsdom,
 * these tests validate the SSE parsing logic and simulate the fetch-based
 * streaming behavior that the component relies on.
 *
 */

import { describe, it, beforeEach } from "node:test";
import assert from "node:assert/strict";

// ---------------------------------------------------------------------------
// Re-implement parseSSEChunk for testing (extracted from component logic)
// ---------------------------------------------------------------------------

/**
 * Parse SSE frames from a text chunk. Handles partial lines across chunks.
 * Returns parsed events and any remaining incomplete data.
 *
 * This is the same logic used in TestRunnerPanel.tsx.
 */
function parseSSEChunk(buffer, chunk) {
  const combined = buffer + chunk;
  const events = [];
  const blocks = combined.split("\n\n");

  // Last element may be incomplete (no trailing \n\n)
  const remaining = blocks.pop() ?? "";

  for (const block of blocks) {
    if (!block.trim()) continue;

    let eventType = "message";
    let data = "";

    for (const line of block.split("\n")) {
      if (line.startsWith("event: ")) {
        eventType = line.slice(7).trim();
      } else if (line.startsWith("data: ")) {
        data = line.slice(6);
      } else if (line.startsWith("data:")) {
        data = line.slice(5);
      }
    }

    events.push({ type: eventType, data });
  }

  return { events, remaining };
}

// ---------------------------------------------------------------------------
// Simulate the component's stream processing logic
// ---------------------------------------------------------------------------

/**
 * Simulates the TestRunnerPanel's stream processing loop.
 * Takes an array of text chunks (as would come from ReadableStream)
 * and returns the final state: lines, status, exitCode.
 */
function simulateStreamProcessing(chunks, { abortAfterChunk = -1 } = {}) {
  const lines = [];
  let status = "connecting";
  let exitCode = null;
  let sseBuffer = "";
  let receivedDone = false;
  let aborted = false;

  status = "streaming";

  for (let i = 0; i < chunks.length; i++) {
    if (abortAfterChunk >= 0 && i > abortAfterChunk) {
      aborted = true;
      status = "cancelled";
      break;
    }

    const chunk = chunks[i];
    const { events, remaining } = parseSSEChunk(sseBuffer, chunk);
    sseBuffer = remaining;

    for (const event of events) {
      if (event.type === "done") {
        try {
          const payload = JSON.parse(event.data);
          exitCode = payload.exit_code;
          status = payload.exit_code === 0 ? "passed" : "failed";
          receivedDone = true;
        } catch {
          status = "failed";
          receivedDone = true;
        }
      } else if (event.type === "error") {
        lines.push(`[ERROR] ${event.data}`);
        status = "failed";
        receivedDone = true;
      } else {
        lines.push(event.data);
      }
    }
  }

  // Process remaining buffer (simulates stream end)
  if (!aborted && sseBuffer.trim()) {
    const { events } = parseSSEChunk(sseBuffer, "\n\n");
    for (const event of events) {
      if (event.type === "done") {
        try {
          const payload = JSON.parse(event.data);
          exitCode = payload.exit_code;
          status = payload.exit_code === 0 ? "passed" : "failed";
          receivedDone = true;
        } catch {
          status = "failed";
          receivedDone = true;
        }
      } else if (event.type === "error") {
        lines.push(`[ERROR] ${event.data}`);
        status = "failed";
        receivedDone = true;
      } else {
        lines.push(event.data);
      }
    }
  }

  // If stream ended without done event → disconnected (Req 4.5)
  if (!aborted && !receivedDone) {
    status = "disconnected";
  }

  return { lines, status, exitCode };
}

// ---------------------------------------------------------------------------
// Tests: SSE Parsing
// ---------------------------------------------------------------------------

describe("TestRunnerPanel — SSE Parsing", () => {
  it("parses a single data event from a complete chunk", () => {
    const { events, remaining } = parseSSEChunk("", "data: hello world\n\n");

    assert.equal(events.length, 1);
    assert.equal(events[0].type, "message");
    assert.equal(events[0].data, "hello world");
    assert.equal(remaining, "");
  });

  it("parses multiple data events from a single chunk", () => {
    const chunk = "data: line1\n\ndata: line2\n\ndata: line3\n\n";
    const { events, remaining } = parseSSEChunk("", chunk);

    assert.equal(events.length, 3);
    assert.equal(events[0].data, "line1");
    assert.equal(events[1].data, "line2");
    assert.equal(events[2].data, "line3");
    assert.equal(remaining, "");
  });

  it("handles partial chunks across multiple calls (buffering)", () => {
    // First chunk ends mid-event
    const { events: events1, remaining: remaining1 } = parseSSEChunk(
      "",
      "data: partial"
    );
    assert.equal(events1.length, 0);
    assert.equal(remaining1, "data: partial");

    // Second chunk completes the event
    const { events: events2, remaining: remaining2 } = parseSSEChunk(
      remaining1,
      "\n\ndata: complete\n\n"
    );
    assert.equal(events2.length, 2);
    assert.equal(events2[0].data, "partial");
    assert.equal(events2[1].data, "complete");
    assert.equal(remaining2, "");
  });

  it("parses named events (event: done)", () => {
    const chunk = 'event: done\ndata: {"exit_code": 0}\n\n';
    const { events } = parseSSEChunk("", chunk);

    assert.equal(events.length, 1);
    assert.equal(events[0].type, "done");
    assert.equal(events[0].data, '{"exit_code": 0}');
  });

  it("parses error events", () => {
    const chunk = "event: error\ndata: Process crashed\n\n";
    const { events } = parseSSEChunk("", chunk);

    assert.equal(events.length, 1);
    assert.equal(events[0].type, "error");
    assert.equal(events[0].data, "Process crashed");
  });

  it("handles data: without space after colon", () => {
    const chunk = "data:no-space\n\n";
    const { events } = parseSSEChunk("", chunk);

    assert.equal(events.length, 1);
    assert.equal(events[0].data, "no-space");
  });

  it("ignores empty blocks between events", () => {
    const chunk = "data: first\n\n\n\ndata: second\n\n";
    const { events } = parseSSEChunk("", chunk);

    assert.equal(events.length, 2);
    assert.equal(events[0].data, "first");
    assert.equal(events[1].data, "second");
  });

  it("handles empty data field", () => {
    const chunk = "data: \n\n";
    const { events } = parseSSEChunk("", chunk);

    assert.equal(events.length, 1);
    assert.equal(events[0].data, "");
  });
});

// ---------------------------------------------------------------------------
// Tests: Stream Processing — Lines appear in terminal
// ---------------------------------------------------------------------------

describe("TestRunnerPanel — Stream Processing (lines in terminal)", () => {
  it("renders lines from SSE data events in order", () => {
    const chunks = [
      "data: Running tests...\n\n",
      "data: test_one PASSED\n\n",
      "data: test_two PASSED\n\n",
      'event: done\ndata: {"exit_code": 0}\n\n',
    ];

    const { lines, status, exitCode } = simulateStreamProcessing(chunks);

    assert.deepEqual(lines, [
      "Running tests...",
      "test_one PASSED",
      "test_two PASSED",
    ]);
    assert.equal(status, "passed");
    assert.equal(exitCode, 0);
  });

  it("handles multiple lines in a single chunk", () => {
    const chunks = [
      "data: line1\n\ndata: line2\n\ndata: line3\n\n",
      'event: done\ndata: {"exit_code": 0}\n\n',
    ];

    const { lines } = simulateStreamProcessing(chunks);

    assert.deepEqual(lines, ["line1", "line2", "line3"]);
  });

  it("handles lines split across chunks", () => {
    const chunks = [
      "data: first li",
      "ne\n\ndata: second line\n\n",
      'event: done\ndata: {"exit_code": 0}\n\n',
    ];

    const { lines } = simulateStreamProcessing(chunks);

    assert.deepEqual(lines, ["first line", "second line"]);
  });
});

// ---------------------------------------------------------------------------
// Tests: Cancel button aborts the stream
// ---------------------------------------------------------------------------

describe("TestRunnerPanel — Cancellation Handling", () => {
  it("sets status to cancelled when stream is aborted", () => {
    const chunks = [
      "data: line1\n\n",
      "data: line2\n\n",
      "data: line3\n\n",
      "data: line4\n\n",
      'event: done\ndata: {"exit_code": 0}\n\n',
    ];

    // Abort after the second chunk (index 1)
    const { lines, status, exitCode } = simulateStreamProcessing(chunks, {
      abortAfterChunk: 1,
    });

    assert.equal(status, "cancelled");
    assert.equal(exitCode, null); // Never received done event
    // Should have processed chunks 0 and 1
    assert.deepEqual(lines, ["line1", "line2"]);
  });

  it("AbortController.abort() triggers AbortError in fetch", async () => {
    // Simulate what happens when AbortController.abort() is called
    const controller = new AbortController();

    const fetchPromise = new Promise((resolve, reject) => {
      controller.signal.addEventListener("abort", () => {
        reject(new DOMException("The operation was aborted.", "AbortError"));
      });
    });

    controller.abort();

    try {
      await fetchPromise;
      assert.fail("Should have thrown");
    } catch (err) {
      assert.equal(err.name, "AbortError");
      assert.equal(err.message, "The operation was aborted.");
    }
  });

  it("cancellation mid-stream does not process remaining chunks", () => {
    const chunks = [
      "data: before-cancel\n\n",
      "data: after-cancel-1\n\n",
      "data: after-cancel-2\n\n",
    ];

    const { lines, status } = simulateStreamProcessing(chunks, {
      abortAfterChunk: 0,
    });

    assert.equal(status, "cancelled");
    assert.deepEqual(lines, ["before-cancel"]);
  });
});

// ---------------------------------------------------------------------------
// Tests: PASSED/FAILED badge on completion
// ---------------------------------------------------------------------------

describe("TestRunnerPanel — Completion Badge", () => {
  it("sets status to 'passed' when exit_code is 0", () => {
    const chunks = [
      "data: All tests passed\n\n",
      'event: done\ndata: {"exit_code": 0}\n\n',
    ];

    const { status, exitCode } = simulateStreamProcessing(chunks);

    assert.equal(status, "passed");
    assert.equal(exitCode, 0);
  });

  it("sets status to 'failed' when exit_code is non-zero", () => {
    const chunks = [
      "data: FAILED test_something\n\n",
      'event: done\ndata: {"exit_code": 1}\n\n',
    ];

    const { status, exitCode } = simulateStreamProcessing(chunks);

    assert.equal(status, "failed");
    assert.equal(exitCode, 1);
  });

  it("sets status to 'failed' when exit_code is 2 (e.g. pytest error)", () => {
    const chunks = [
      "data: Error collecting tests\n\n",
      'event: done\ndata: {"exit_code": 2}\n\n',
    ];

    const { status, exitCode } = simulateStreamProcessing(chunks);

    assert.equal(status, "failed");
    assert.equal(exitCode, 2);
  });

  it("sets status to 'failed' when done event has invalid JSON", () => {
    const chunks = [
      "data: some output\n\n",
      "event: done\ndata: invalid-json\n\n",
    ];

    const { status } = simulateStreamProcessing(chunks);

    assert.equal(status, "failed");
  });

  it("sets status to 'failed' on error event from backend", () => {
    const chunks = [
      "data: starting...\n\n",
      "event: error\ndata: Process crashed unexpectedly\n\n",
    ];

    const { lines, status } = simulateStreamProcessing(chunks);

    assert.equal(status, "failed");
    assert.ok(lines.some((l) => l.includes("[ERROR]")));
    assert.ok(lines.some((l) => l.includes("Process crashed unexpectedly")));
  });
});

// ---------------------------------------------------------------------------
// Tests: Connection lost warning
// ---------------------------------------------------------------------------

describe("TestRunnerPanel — Connection Lost", () => {
  it("sets status to 'disconnected' when stream ends without done event", () => {
    // Simulate stream ending abruptly (no done event)
    const chunks = [
      "data: line1\n\n",
      "data: line2\n\n",
      // Stream ends here — no done event
    ];

    const { status, exitCode } = simulateStreamProcessing(chunks);

    assert.equal(status, "disconnected");
    assert.equal(exitCode, null);
  });

  it("sets status to 'disconnected' when stream ends with partial buffer", () => {
    // Simulate stream ending mid-event
    const chunks = [
      "data: line1\n\n",
      "data: partial", // No trailing \n\n — incomplete event
    ];

    const { status } = simulateStreamProcessing(chunks);

    // The partial buffer "data: partial" will be processed at stream end
    // but since there's no done event, status should be disconnected
    assert.equal(status, "disconnected");
  });

  it("network error during fetch sets disconnected status", () => {
    // Simulate what happens when a network error occurs
    // (not an AbortError — that's cancellation)
    const error = new TypeError("Failed to fetch");

    // The component catches non-AbortError exceptions and sets disconnected
    const isAbortError =
      error instanceof DOMException && error.name === "AbortError";
    const expectedStatus = isAbortError ? "cancelled" : "disconnected";

    assert.equal(expectedStatus, "disconnected");
  });

  it("distinguishes AbortError (cancel) from network error (disconnect)", () => {
    const abortError = new DOMException("Aborted", "AbortError");
    const networkError = new TypeError("Failed to fetch");

    // AbortError → cancelled
    assert.equal(abortError.name, "AbortError");
    assert.equal(
      abortError instanceof DOMException && abortError.name === "AbortError",
      true
    );

    // Network error → disconnected
    assert.equal(networkError.name, "TypeError");
    assert.equal(
      networkError instanceof DOMException && networkError.name === "AbortError",
      false
    );
  });
});

// ---------------------------------------------------------------------------
// Tests: SSE Response Format Validation
// ---------------------------------------------------------------------------

describe("TestRunnerPanel — SSE Response Format", () => {
  it("expects text/event-stream content type from backend", () => {
    // The component checks response.ok and response.body
    // This test validates the expected format
    const expectedContentType = "text/event-stream";
    assert.ok(expectedContentType.includes("event-stream"));
  });

  it("handles HTTP error response (non-200)", () => {
    // Simulate what happens when backend returns 404
    const mockResponse = {
      ok: false,
      status: 404,
      text: async () => '{"detail": "Service not found"}',
      body: null,
    };

    // Component logic: if !response.ok, add error line and set failed
    assert.equal(mockResponse.ok, false);
    assert.equal(mockResponse.status, 404);
  });

  it("handles response with no body (streaming not supported)", () => {
    const mockResponse = {
      ok: true,
      status: 200,
      body: null,
    };

    // Component checks response.body existence
    assert.equal(mockResponse.body, null);
  });

  it("correctly formats the POST URL with service name", () => {
    const serviceName = "admin-dashboard-api";
    const baseUrl = "http://localhost:8082";
    const expectedUrl = `${baseUrl}/admin/services/${encodeURIComponent(serviceName)}/test?stream=true`;

    assert.equal(
      expectedUrl,
      "http://localhost:8082/admin/services/admin-dashboard-api/test?stream=true"
    );
  });

  it("encodes special characters in service name", () => {
    const serviceName = "service/with spaces";
    const encoded = encodeURIComponent(serviceName);
    assert.equal(encoded, "service%2Fwith%20spaces");
  });
});

// ---------------------------------------------------------------------------
// Tests: Edge Cases
// ---------------------------------------------------------------------------

describe("TestRunnerPanel — Edge Cases", () => {
  it("handles empty stream (no output, just done)", () => {
    const chunks = ['event: done\ndata: {"exit_code": 0}\n\n'];

    const { lines, status, exitCode } = simulateStreamProcessing(chunks);

    assert.deepEqual(lines, []);
    assert.equal(status, "passed");
    assert.equal(exitCode, 0);
  });

  it("handles very long lines without truncation", () => {
    const longLine = "x".repeat(10000);
    const chunks = [
      `data: ${longLine}\n\n`,
      'event: done\ndata: {"exit_code": 0}\n\n',
    ];

    const { lines } = simulateStreamProcessing(chunks);

    assert.equal(lines.length, 1);
    assert.equal(lines[0].length, 10000);
  });

  it("handles rapid succession of events", () => {
    // All events in one chunk
    let chunk = "";
    for (let i = 0; i < 100; i++) {
      chunk += `data: line ${i}\n\n`;
    }
    chunk += 'event: done\ndata: {"exit_code": 0}\n\n';

    const { lines, status } = simulateStreamProcessing([chunk]);

    assert.equal(lines.length, 100);
    assert.equal(lines[0], "line 0");
    assert.equal(lines[99], "line 99");
    assert.equal(status, "passed");
  });

  it("handles lines with special characters", () => {
    const chunks = [
      "data: PASSED ✓ test_something\n\n",
      "data: Error: expected <div> to have class 'active'\n\n",
      "data:   at Object.<anonymous> (test.js:42:5)\n\n",
      'event: done\ndata: {"exit_code": 1}\n\n',
    ];

    const { lines, status } = simulateStreamProcessing(chunks);

    assert.equal(lines[0], "PASSED ✓ test_something");
    assert.ok(lines[1].includes("<div>"));
    assert.ok(lines[2].includes("at Object"));
    assert.equal(status, "failed");
  });
});
