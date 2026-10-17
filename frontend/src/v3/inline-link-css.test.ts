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
    // a <button> centers its text by default; a wrapped name must stay left-aligned
    expect(rule?.[1]).toMatch(/text-align:\s*inherit/);
  });

  it("gives long key-value labels a column that fits them", () => {
    expect(css).toMatch(/\.v3 \.v3-kv\.wide\s*\{[^}]*grid-template-columns:\s*max-content/);
  });

  it("does not reuse the global .search class in project filters (double border)", () => {
    // theme/app.css styles `.search` with its own border and padding; inside the
    // filters the wrapped v3-input then showed two borders.
    for (const f of ["MembersTab.tsx", "ProjectList.tsx"]) {
      const src = readFileSync(join(__dirname, "pages/projects", f), "utf-8");
      expect(src).not.toMatch(/className="search"/);
      expect(src).toMatch(/className="v3-prj-search"/);
    }
  });
});

describe("v3 page labels", () => {
  for (const lang of ["en", "zh-CN"]) {
    const d = JSON.parse(readFileSync(join(__dirname, `../locales/${lang}/common.json`), "utf-8"));
    it(`${lang}: the fleet page title matches its nav label`, () => {
      expect(d.v3.fleet.title).toBe(d.v2.nav.fleet);
    });
    it(`${lang}: the SSO subtitle names both protocols`, () => {
      expect(d.v3.sso.sub).toMatch(/SAML/);
      expect(d.v3.sso.sub).toMatch(/OpenID Connect/);
    });
  }
});
