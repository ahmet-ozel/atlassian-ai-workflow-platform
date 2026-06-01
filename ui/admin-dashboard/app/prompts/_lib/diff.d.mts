/**
 * TypeScript companion declaration for `diff.mjs`. Mirrors the
 * JSDoc typedefs verbatim so the editor sees real types when
 * importing the helper from the React page.
 */

export type DiffLine = {
  kind: "equal" | "add" | "remove";
  text: string;
};

export function splitLines(body: string): string[];
export function diffLines(oldBody: string, newBody: string): DiffLine[];
export function hasChanges(oldBody: string, newBody: string): boolean;
