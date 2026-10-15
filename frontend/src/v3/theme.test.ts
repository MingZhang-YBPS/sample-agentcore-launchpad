import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { THEME_CHOICES, THEME_KEY, applyTheme, parseChoice, readChoice, resolveTheme } from "./theme";

/** A stand-in for <html>: just the attribute surface applyTheme touches. */
function fakeRoot() {
  const attrs = new Map<string, string>();
  return {
    dataset: {} as Record<string, string>,
    attrs,
    setAttribute: (k: string, v: string) => void attrs.set(k, v),
    removeAttribute: (k: string) => void attrs.delete(k),
  };
}

describe("resolveTheme (TH-1)", () => {
  it("an explicit choice wins over the system", () => {
    expect(resolveTheme("light", false)).toBe("light");
    expect(resolveTheme("dark", true)).toBe("dark");
  });
  it("system follows prefers-color-scheme", () => {
    expect(resolveTheme("system", true)).toBe("light");
    expect(resolveTheme("system", false)).toBe("dark");
  });
});

describe("parseChoice / readChoice", () => {
  it("defaults to dark until someone chooses", () => {
    expect(parseChoice(null)).toBe("dark");
    expect(parseChoice(undefined)).toBe("dark");
    expect(parseChoice("")).toBe("dark");
    expect(parseChoice("sepia")).toBe("dark");
    for (const c of THEME_CHOICES) expect(parseChoice(c)).toBe(c);
  });
  it("reads the stored choice under its key", () => {
    const seen: string[] = [];
    const storage = { getItem: (k: string) => (seen.push(k), "light") };
    expect(readChoice(storage)).toBe("light");
    expect(seen).toEqual([THEME_KEY]);
  });
  it("falls back to dark when storage is unavailable or throws", () => {
    expect(readChoice(undefined)).toBe("dark");
    expect(readChoice({ getItem: () => { throw new Error("SecurityError"); } })).toBe("dark");
  });
});

describe("applyTheme", () => {
  it("sets data-theme on the root and leaves it alone when unchanged", () => {
    const root = fakeRoot();
    applyTheme("light", root as unknown as HTMLElement);
    expect(root.dataset.theme).toBe("light");
    applyTheme("light", root as unknown as HTMLElement, true);
    expect(root.attrs.has("data-theme-switching")).toBe(false);
  });
  it("marks a crossfade only when animating a real change", () => {
    const root = fakeRoot();
    root.dataset.theme = "dark";
    applyTheme("light", root as unknown as HTMLElement, true);
    expect(root.dataset.theme).toBe("light");
    expect(root.attrs.has("data-theme-switching")).toBe(true);
  });
});

describe("index.html pre-paint script", () => {
  const html = readFileSync(new URL("../../index.html", import.meta.url), "utf8");
  const body = /<script>([\s\S]*?)<\/script>/.exec(html)?.[1] ?? "";

  /** Run the inline script against a fake storage / media query and return the attribute it set. */
  function run(stored: string | null | "throw", systemLight: boolean, hasMatchMedia = true): string | undefined {
    let set: string | undefined;
    const storage = {
      getItem: (k: string) => {
        if (stored === "throw") throw new Error("SecurityError");
        return k === THEME_KEY ? stored : null;
      },
    };
    const mm = (q: string) => ({ matches: q === "(prefers-color-scheme: light)" && systemLight });
    const win = hasMatchMedia ? { matchMedia: mm } : {};
    const doc = { documentElement: { setAttribute: (k: string, v: string) => { if (k === "data-theme") set = v; } } };
    new Function("localStorage", "window", "matchMedia", "document", body)(storage, win, mm, doc);
    return set;
  }

  it("is present and runs before the app module", () => {
    expect(body).toContain(THEME_KEY);
    expect(html.indexOf("<script>")).toBeLessThan(html.indexOf('src="/src/main.tsx"'));
  });

  it("resolves exactly like resolveTheme for every stored value", () => {
    for (const stored of [null, "", "junk", ...THEME_CHOICES]) {
      for (const systemLight of [true, false]) {
        expect(run(stored, systemLight), `${stored} / system light ${systemLight}`).toBe(
          resolveTheme(parseChoice(stored), systemLight),
        );
      }
    }
  });

  it("stays dark when storage throws or matchMedia is missing", () => {
    expect(run("throw", true)).toBe("dark");
    expect(run("system", true, false)).toBe("dark");
  });
});
