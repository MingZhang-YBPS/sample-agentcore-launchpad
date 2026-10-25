import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { MODEL_ID_ALLOWLIST } from "./model-id-allowlist";

const SRC = fileURLToPath(new URL("..", import.meta.url));
// a geographic / global inference profile, and a bare quoted anthropic./openai. id
const PROFILE_ID = /(global|us|eu|apac)\.(anthropic|openai|zai|amazon|meta|mistral|deepseek|qwen|moonshotai|xai)\.[a-z0-9]/;
const BARE_ID = /['"`](anthropic|openai)\.[a-z]/;

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) return name === "locales" ? [] : sources(path);
    return /\.(ts|tsx)$/.test(name) && !/\.test\.(ts|tsx)$/.test(name) ? [path] : [];
  });
}

describe("hard-coded Bedrock model ids", () => {
  it("appear only in allowlisted static fallbacks and label tables", () => {
    const hits: string[] = [];
    for (const path of sources(SRC)) {
      const file = relative(SRC, path).split("\\").join("/");
      if (file === "lib/model-id-allowlist.ts" || file in MODEL_ID_ALLOWLIST) continue;
      readFileSync(path, "utf8").split("\n").forEach((line, i) => {
        if (PROFILE_ID.test(line) || BARE_ID.test(line)) hits.push(`${file}:${i + 1}: ${line.trim()}`);
      });
    }
    expect(hits).toEqual([]);
  });

  it("allowlists only files that still need it, each with a reason", () => {
    for (const [file, reason] of Object.entries(MODEL_ID_ALLOWLIST)) {
      expect(reason.length, file).toBeGreaterThan(20);
      const text = readFileSync(join(SRC, file), "utf8");
      expect(PROFILE_ID.test(text) || BARE_ID.test(text), `${file} no longer needs its allowlist entry`).toBe(true);
    }
  });
});
