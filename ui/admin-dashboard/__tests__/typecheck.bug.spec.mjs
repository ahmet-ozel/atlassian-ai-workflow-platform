/**
 * Bug Condition Exploration Test
 *
 * This property-based test encodes the expected behavior: for all (file, line, code)
 * in the verified counterexample table from the design document, `tsc --noEmit`
 * from `platform/ui/admin-dashboard` emits NO diagnostic with that code at that
 * location.
 *
 * On UNFIXED code this test is EXPECTED TO FAIL — failure confirms the bug exists.
 * On FIXED code this test PASSES — confirming the fix resolves all 14 counterexamples.
 *
 * Checks the known counterexample conditions.
 */

import { describe, it, before } from "node:test";
import assert from "node:assert/strict";
import { execSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import fc from "fast-check";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const PROJECT_ROOT = path.resolve(__dirname, "..");

// ---------------------------------------------------------------------------
// The 14 verified counterexamples from the design document's Bug Details table
// ---------------------------------------------------------------------------

/**
 * Each row represents a concrete (file, line, TS error code) triple that
 * demonstrates the bug. The generator enumerates these 14 rows.
 */
const COUNTEREXAMPLES = [
  // Bug Family 1: apiFetch lacks generic type parameter (13 errors)
  { file: "app/audit/page.tsx", line: 48, code: "TS2558", description: "apiFetch<{ results: Hit[] }>(...) — expected 0 type arguments" },
  { file: "app/audit/page.tsx", line: 51, code: "TS2339", description: "res.results — Property 'results' does not exist on type 'Response'" },
  { file: "app/notifications/page.tsx", line: 31, code: "TS2558", description: "apiFetch<{ items: DeptNotifyRow[] }>(...)" },
  { file: "app/notifications/page.tsx", line: 34, code: "TS2339", description: "res.items" },
  { file: "app/security/page.tsx", line: 40, code: "TS2558", description: "apiFetch<{ items: ProbeArtifact[] }>(...)" },
  { file: "app/security/page.tsx", line: 43, code: "TS2558", description: "apiFetch<{ depts: RotateBannerEntry[] }>(...)" },
  { file: "app/security/page.tsx", line: 47, code: "TS2339", description: "probeRes.items" },
  { file: "app/security/page.tsx", line: 48, code: "TS2339", description: "banner.depts" },
  { file: "app/workflows/[id]/page.tsx", line: 78, code: "TS2558", description: "apiFetch<WorkflowDetail>(...)" },
  { file: "app/workflows/[id]/page.tsx", line: 79, code: "TS2345", description: "setDetail(data) — Response not assignable to SetStateAction<WorkflowDetail | null>" },
  { file: "app/workflows/page.tsx", line: 50, code: "TS2558", description: "apiFetch<ListResponse>(...)" },
  { file: "app/workflows/page.tsx", line: 53, code: "TS2339", description: "res.items" },
  { file: "app/workflows/page.tsx", line: 53, code: "TS2339", description: "res.workflows" },
  // Bug Family 2: web-shared module declaration missing (1 error)
  { file: "app/services/_components/StartFormModal.tsx", line: 55, code: "TS2307", description: "import { isSensitiveEnvKey } from \"web-shared\" — module not found" },
];

// ---------------------------------------------------------------------------
// Run tsc --noEmit once and cache the result
// ---------------------------------------------------------------------------

/** @type {{ exitCode: number; stdout: string; diagnostics: Array<{ file: string; line: number; code: string; message: string }> }} */
let tscResult;

/**
 * Parse tsc diagnostic output into structured records.
 * tsc outputs lines like:
 *   app/audit/page.tsx(48,34): error TS2558: Expected 0 type arguments, but got 1.
 */
function parseTscOutput(output) {
  const diagnostics = [];
  const diagnosticPattern = /^(.+?)\((\d+),\d+\):\s+error\s+(TS\d+):\s+(.+)$/gm;
  let match;
  while ((match = diagnosticPattern.exec(output)) !== null) {
    diagnostics.push({
      file: match[1].replace(/\\/g, "/"),
      line: parseInt(match[2], 10),
      code: match[3],
      message: match[4],
    });
  }
  return diagnostics;
}

before(() => {
  let stdout = "";
  let exitCode = 0;
  try {
    stdout = execSync("npx tsc --noEmit -p .", {
      cwd: PROJECT_ROOT,
      encoding: "utf-8",
      stdio: ["pipe", "pipe", "pipe"],
    });
    exitCode = 0;
  } catch (err) {
    // tsc exits non-zero when there are errors
    stdout = (err.stdout || "") + (err.stderr || "");
    exitCode = err.status ?? 1;
  }

  const diagnostics = parseTscOutput(stdout);
  tscResult = { exitCode, stdout, diagnostics };
});

// ---------------------------------------------------------------------------
// Bug Condition: For all counterexamples, tsc emits NO diagnostic
// ---------------------------------------------------------------------------

describe("Bug Condition Exploration", () => {
  it("for all (file, line, code) in the verified counterexample table, tsc --noEmit emits no diagnostic with that code at that location", () => {
    /**
     * Checks the known counterexample conditions.
     *
     * Generator: enumerates the 14 rows from the Bug Details table.
     * Property: for each row, assert that NO diagnostic with the listed
     * error code appears at the listed (file, line) location.
     */
    const counterexampleArb = fc.constantFrom(...COUNTEREXAMPLES);

    fc.assert(
      fc.property(counterexampleArb, (example) => {
        const matchingDiagnostic = tscResult.diagnostics.find(
          (d) =>
            d.file === example.file &&
            d.line === example.line &&
            d.code === example.code
        );

        // The property asserts NO diagnostic exists at this location.
        // If a matching diagnostic IS found, the property is violated
        // (which on unfixed code confirms the bug exists).
        if (matchingDiagnostic) {
          throw new Error(
            `Bug confirmed at ${example.file}:${example.line} — ` +
            `${example.code}: ${matchingDiagnostic.message}\n` +
            `  Description: ${example.description}`
          );
        }
      }),
      { numRuns: COUNTEREXAMPLES.length }
    );
  });

  it("aggregate post-condition: tsc --noEmit exits with code 0 and reports 0 errors", () => {
    /**
     * Checks that the final counterexample remains undiagnosed.
     *
     * This asserts the aggregate condition for the counterexample set:
     * tsc --noEmit exits 0 with 0 errors.
     */
    assert.equal(
      tscResult.exitCode,
      0,
      `tsc exited with code ${tscResult.exitCode} (expected 0).\n` +
      `Found ${tscResult.diagnostics.length} diagnostic(s) in output.`
    );
    assert.equal(
      tscResult.diagnostics.length,
      0,
      `Expected 0 diagnostics but found ${tscResult.diagnostics.length}:\n` +
      tscResult.diagnostics
        .map((d) => `  ${d.file}(${d.line}): ${d.code} ${d.message}`)
        .join("\n")
    );
  });
});

// ---------------------------------------------------------------------------
// Detailed counterexample documentation (for exploration output)
// ---------------------------------------------------------------------------

describe("Bug Family 1: apiFetch lacks generic type parameter (13 errors)", () => {
  const family1 = COUNTEREXAMPLES.filter((e) => e.code !== "TS2307");

  for (const example of family1) {
    it(`${example.file}:${example.line} — ${example.code} (${example.description})`, () => {
      const matchingDiagnostic = tscResult.diagnostics.find(
        (d) =>
          d.file === example.file &&
          d.line === example.line &&
          d.code === example.code
      );

      // On fixed code: no diagnostic should exist
      assert.equal(
        matchingDiagnostic,
        undefined,
        `Expected no ${example.code} at ${example.file}:${example.line} but found: ${matchingDiagnostic?.message}`
      );
    });
  }
});

describe("Bug Family 2: web-shared module declaration missing (1 error)", () => {
  const family2 = COUNTEREXAMPLES.filter((e) => e.code === "TS2307");

  for (const example of family2) {
    it(`${example.file}:${example.line} — ${example.code} (${example.description})`, () => {
      const matchingDiagnostic = tscResult.diagnostics.find(
        (d) =>
          d.file === example.file &&
          d.line === example.line &&
          d.code === example.code
      );

      // On fixed code: no diagnostic should exist
      assert.equal(
        matchingDiagnostic,
        undefined,
        `Expected no ${example.code} at ${example.file}:${example.line} but found: ${matchingDiagnostic?.message}`
      );
    });
  }
});
