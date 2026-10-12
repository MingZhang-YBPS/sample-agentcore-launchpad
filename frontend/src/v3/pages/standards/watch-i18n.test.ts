import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const read = (p: string) => readFileSync(resolve(here, p), "utf-8");
const watchKeys = (lang: string) => JSON.parse(read(`../../../locales/${lang}/common.json`)).v2.dlc.watch;

describe("drift watch: agent-or-judge hint", () => {
  it("is translated in both languages", () => {
    // the backend ships an English-only hint; a zh-CN reader saw it untranslated
    expect(watchKeys("en").driftHint).toMatch(/judge drift/);
    expect(watchKeys("zh-CN").driftHint).toMatch(/裁判漂移/);
  });

  it("renders the locale string, not the raw backend hint", () => {
    const src = read("./Watch.tsx");
    expect(src).not.toContain("{drift.hint}");
    expect(src).toContain('t("v2.dlc.watch.driftHint")');
  });
});
