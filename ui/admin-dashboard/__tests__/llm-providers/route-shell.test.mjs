// Component test — /admin/llm-providers route shell.
//
// Validates Requirement 14.1: the page renders the provider table and
// the `Add Provider` button. node:test runs without RTL / jsdom, so
// the test asserts against the page's declared TSX file by parsing
// it as text and confirming the structural anchors exist.

import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";


const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const PAGE_PATH = path.resolve(
  __dirname,
  "../../app/llm-providers/page.tsx",
);


describe("/admin/llm-providers route shell (R14.1)", () => {
  const source = readFileSync(PAGE_PATH, "utf-8");

  it("page file exists", () => {
    assert.ok(source.length > 0);
  });

  it("renders the ProviderTable composition", () => {
    assert.ok(
      source.includes("<ProviderTable"),
      "page must compose <ProviderTable>",
    );
  });

  it("renders the Add Provider button with the canonical testid", () => {
    assert.ok(
      source.includes('data-testid="llm-provider-add-button"'),
      "page must expose llm-provider-add-button testid",
    );
    assert.ok(
      source.includes("Add Provider"),
      "page must render the Add Provider label",
    );
  });

  it("wires Disable to PUT { status: \"inactive\" }", () => {
    // The page calls api.disable(row.id) which uses
    // PUT /admin/llm-providers/{id} body { status: "inactive" } per
    // R14.7. We assert the call-site exists; the API hook test
    // covers the body shape itself.
    assert.ok(
      source.includes("handleDisable"),
      "page must define a handleDisable callback",
    );
    assert.ok(
      source.includes("api.disable"),
      "page must wire Disable to api.disable",
    );
  });

  it("composes ProviderModal + DeleteConfirm dialogs", () => {
    assert.ok(source.includes("<ProviderModal"));
    assert.ok(source.includes("<DeleteConfirm"));
  });
});
