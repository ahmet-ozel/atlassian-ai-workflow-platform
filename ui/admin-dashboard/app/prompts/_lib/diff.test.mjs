// @ts-check
import { strict as assert } from "node:assert";
import { test } from "node:test";

import { diffLines, hasChanges, splitLines } from "./diff.mjs";

test("splitLines preserves empty input", () => {
  assert.deepEqual(splitLines(""), []);
});

test("splitLines handles unix newlines", () => {
  assert.deepEqual(splitLines("a\nb\nc"), ["a", "b", "c"]);
});

test("splitLines handles windows newlines", () => {
  assert.deepEqual(splitLines("a\r\nb\r\nc"), ["a", "b", "c"]);
});

test("hasChanges identical bodies", () => {
  assert.equal(hasChanges("hello", "hello"), false);
});

test("hasChanges different bodies", () => {
  assert.equal(hasChanges("hello", "world"), true);
});

test("diffLines reports unchanged content", () => {
  const out = diffLines("a\nb\nc", "a\nb\nc");
  assert.deepEqual(out, [
    { kind: "equal", text: "a" },
    { kind: "equal", text: "b" },
    { kind: "equal", text: "c" },
  ]);
});

test("diffLines flags a single line addition", () => {
  const out = diffLines("a\nb", "a\nb\nc");
  assert.deepEqual(out, [
    { kind: "equal", text: "a" },
    { kind: "equal", text: "b" },
    { kind: "add", text: "c" },
  ]);
});

test("diffLines flags a single line removal", () => {
  const out = diffLines("a\nb\nc", "a\nc");
  assert.deepEqual(out, [
    { kind: "equal", text: "a" },
    { kind: "remove", text: "b" },
    { kind: "equal", text: "c" },
  ]);
});

test("diffLines flags a replacement as remove + add", () => {
  const out = diffLines("a\nb\nc", "a\nB\nc");
  assert.deepEqual(out, [
    { kind: "equal", text: "a" },
    { kind: "remove", text: "b" },
    { kind: "add", text: "B" },
    { kind: "equal", text: "c" },
  ]);
});

test("diffLines empty old → all adds", () => {
  const out = diffLines("", "a\nb");
  assert.deepEqual(out, [
    { kind: "add", text: "a" },
    { kind: "add", text: "b" },
  ]);
});

test("diffLines empty new → all removes", () => {
  const out = diffLines("a\nb", "");
  assert.deepEqual(out, [
    { kind: "remove", text: "a" },
    { kind: "remove", text: "b" },
  ]);
});
