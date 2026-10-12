import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const read = (p: string) => readFileSync(resolve(here, p), "utf-8");
const checks = (lang: string) => JSON.parse(read(`../../locales/${lang}/common.json`)).v3.models.check;

describe("Models page: probe check labels", () => {
  // live: testing the 新建智能体 row (GLM-5.3) printed the raw key v3.models.check.tool_use
  it("names every check the backend probe runs, in both languages", () => {
    const backend = read("../../../../backend/app/services/model_settings.py");
    const names = [...backend.matchAll(/_check\("([a-z_]+)"/g)].map((m) => m[1]);
    expect(names).toContain("tool_use");
    for (const lang of ["en", "zh-CN"]) {
      for (const name of new Set(names)) expect(checks(lang)[name], `${lang}: ${name}`).toBeTruthy();
    }
  });
});
