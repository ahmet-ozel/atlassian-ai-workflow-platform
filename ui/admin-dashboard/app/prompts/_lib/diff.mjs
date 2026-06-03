// @ts-check

/**
 * Lightweight line-by-line diff helper for the prompt editor
 * used by the prompt editor.
 *
 * The admin-dashboard ships **no** runtime dependencies beyond
 * Next.js itself (see `package.json`); pulling in the `diff` npm
 * package just to render an "old vs new" Markdown view felt heavy
 * for what is, at worst, a few hundred lines of prompt body. This
 * module instead emits a coarse per-line diff that is good enough
 * for a "what changed" preview — every line is classified as one
 * of:
 *
 *   * `equal`   — line is present in both sides.
 *   * `add`     — line is only in the new body.
 *   * `remove`  — line is only in the old body.
 *
 * The implementation is the textbook longest-common-subsequence
 * algorithm operating on string arrays. Quadratic in the number of
 * lines (O(n*m) memory) — for ~64 KiB prompts (the router cap)
 * worst-case ~1500 lines per side, ~2.25M cells, fine for a
 * client-side preview that runs at most every keystroke.
 *
 * Co-located in `_lib/` so the catalogue page can import it without
 * pulling in React, and so a Node `--test` runner can pin the
 * contract without spinning up a renderer (mirrors the pattern in
 * `app/capabilities/_lib/matrix.mjs`).
 *
 * @typedef {{ kind: "equal" | "add" | "remove", text: string }} DiffLine
 */

/**
 * Split `body` into lines, preserving empty trailing lines so a
 * paste-with-newline still shows up in the diff. Splits on `\r\n`
 * and `\n` only — `\r` standalone is treated as content because
 * stale macOS-classic input is not worth handling here.
 *
 * @param {string} body
 * @returns {string[]}
 */
export function splitLines(body) {
  if (body === "") {
    return [];
  }
  return body.split(/\r\n|\n/);
}

/**
 * Compute a per-line diff between `oldBody` and `newBody`.
 *
 * @param {string} oldBody
 * @param {string} newBody
 * @returns {DiffLine[]}
 */
export function diffLines(oldBody, newBody) {
  const a = splitLines(oldBody);
  const b = splitLines(newBody);

  // Build the LCS length table.
  const n = a.length;
  const m = b.length;
  /** @type {number[][]} */
  const lcs = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      if (a[i] === b[j]) {
        lcs[i][j] = lcs[i + 1][j + 1] + 1;
      } else {
        lcs[i][j] = Math.max(lcs[i + 1][j], lcs[i][j + 1]);
      }
    }
  }

  // Walk the table to reconstruct the diff.
  /** @type {DiffLine[]} */
  const out = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      out.push({ kind: "equal", text: a[i] });
      i++;
      j++;
    } else if (lcs[i + 1][j] >= lcs[i][j + 1]) {
      out.push({ kind: "remove", text: a[i] });
      i++;
    } else {
      out.push({ kind: "add", text: b[j] });
      j++;
    }
  }
  while (i < n) {
    out.push({ kind: "remove", text: a[i] });
    i++;
  }
  while (j < m) {
    out.push({ kind: "add", text: b[j] });
    j++;
  }
  return out;
}

/**
 * Quick predicate — returns `true` when the two bodies differ in any
 * way. Cheaper than computing the full diff; used to enable/disable
 * the "Sandbox Test" / "Commit" buttons.
 *
 * @param {string} oldBody
 * @param {string} newBody
 * @returns {boolean}
 */
export function hasChanges(oldBody, newBody) {
  return oldBody !== newBody;
}
