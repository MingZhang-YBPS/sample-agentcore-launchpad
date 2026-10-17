import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

// A <button> does not inherit color from its table row, so a button styled as an inline
// link rendered UA "buttontext" (near-black) on the dark theme (workshop walk BUG-5).
const css = readFileSync(join(__dirname, "v3.css"), "utf-8");

describe("v3.css inline links", () => {
  it("lets a button inline link inherit the row's color and font", () => {
    const rule = css.match(/\.v3 button\.v3-inline-link\s*\{([^}]*)\}/);
    expect(rule?.[1]).toMatch(/color:\s*inherit/);
    expect(rule?.[1]).toMatch(/font:\s*inherit/);
  });

  it("gives long key-value labels a column that fits them", () => {
    expect(css).toMatch(/\.v3 \.v3-kv\.wide\s*\{[^}]*grid-template-columns:\s*max-content/);
  });
});
