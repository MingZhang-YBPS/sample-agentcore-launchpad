#!/usr/bin/env node
// Storyline consistency checks (storyline-ux §4, guided-console §7), run by
// scripts/verify.sh. Every check reads source and copy only — no build, no backend.
//
//   1. actions: every storyline step's action, every DEFAULT_NEXT action, and every
//      `next_step("…")` the backend names exists in NEXT_ACTIONS (story/actions.ts);
//   2. copy: every action has en + zh-CN `apiNext.<key>`, every step `story.step.<key>`;
//   3. error codes: a backend `AppError` / `NotFoundError` code without en + zh-CN
//      `apiErrors.<code>` must be listed in src/locales/missing-api-errors.json, and a
//      listed code that has its copy now (or is gone) must be removed — the baseline
//      only shrinks;
//   4. disabled reasons: the `launchpad/disabled-reason` ESLint rule's count per file
//      over src/v3 matches eslint-rules/disabled-reason-baseline.json exactly — more
//      fails (a new reasonless disabled control), fewer fails too (remove the entry).
//
// `--prune` rewrites both baselines with the fixed entries removed; it never adds one.
import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const FRONTEND = resolve(dirname(fileURLToPath(import.meta.url)), "..");
export const BACKEND_APP = resolve(FRONTEND, "../backend/app");
export const API_ERRORS_BASELINE = "src/locales/missing-api-errors.json";
export const DISABLED_BASELINE = "eslint-rules/disabled-reason-baseline.json";

const read = (rel) => readFileSync(resolve(FRONTEND, rel), "utf-8");
const readJson = (rel) => JSON.parse(read(rel));

/* ── sources ──────────────────────────────────────────────────────────────── */

/** NEXT_ACTIONS keys, from `"key": action("key", …)` entries in story/actions.ts. */
export function parseActionKeys(src) {
  const body = src.slice(src.indexOf("export const NEXT_ACTIONS"), src.indexOf("} satisfies"));
  return [...body.matchAll(/^\s*"([a-z_.]+)":\s*action\("\1"/gm)].map((m) => m[1]);
}

/** DEFAULT_NEXT's actions. */
export function parseDefaultNext(src) {
  const start = src.indexOf("export const DEFAULT_NEXT");
  const body = src.slice(start, src.indexOf("};", start));
  return [...body.matchAll(/^\s*"[a-z_.]+":\s*"([a-z_.]+)"/gm)].map((m) => m[1]);
}

/** Story steps `{ key, action }` from `step("key", "chapter", [roles], "perm", "action", …)`. */
export function parseSteps(src) {
  return [...src.matchAll(/\bstep\(\s*"([a-z_.]+)",\s*"[a-z]+",\s*\[[^\]]*\],\s*"[^"]*",\s*"([a-z_.]+)"/g)]
    .map((m) => ({ key: m[1], action: m[2] }));
}

function pyFiles(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const path = join(dir, name);
    if (statSync(path).isDirectory()) {
      if (name !== "__pycache__") out.push(...pyFiles(path));
    } else if (name.endsWith(".py")) out.push(path);
  }
  return out;
}

/** `next_step("action", …)` names in backend source. */
export function parseBackendNext(src) {
  return [...src.matchAll(/\bnext_step\(\s*"([a-z_.]+)"/g)].map((m) => m[1]);
}

/**
 * Error codes raised in backend source: `AppError("a.b", …)`, `NotFoundError("a.b", …)`,
 * and an `AppError` subclass's `super().__init__("a.b", …)`. Computed codes (f-strings)
 * are out of reach and not counted.
 */
export function parseErrorCodes(src) {
  const re = /\b(?:AppError|NotFoundError|super\(\)\.__init__)\(\s*"([a-z0-9_]+(?:\.[a-z0-9_]+)+)"/g;
  return [...src.matchAll(re)].map((m) => m[1]);
}

function backendScan(appDir = BACKEND_APP) {
  const next = new Set();
  const codes = new Set();
  if (!existsSync(appDir)) return { next, codes };
  for (const file of pyFiles(appDir)) {
    const src = readFileSync(file, "utf-8");
    for (const a of parseBackendNext(src)) next.add(a);
    for (const c of parseErrorCodes(src)) codes.add(c);
  }
  return { next, codes };
}

/* ── checks (each returns a list of problems) ─────────────────────────────── */

export function checkActions({ actions, steps, defaultNext, backendNext }) {
  const known = new Set(actions);
  const problems = [];
  for (const s of steps) if (!known.has(s.action)) problems.push(`story step ${s.key}: action ${s.action} is not in NEXT_ACTIONS`);
  for (const a of defaultNext) if (!known.has(a)) problems.push(`DEFAULT_NEXT: ${a} is not in NEXT_ACTIONS`);
  for (const a of backendNext) if (!known.has(a)) problems.push(`backend next_step("${a}") is not in NEXT_ACTIONS`);
  return problems;
}

export function checkCopy({ actions, steps, locales }) {
  const problems = [];
  for (const [lang, copy] of Object.entries(locales)) {
    for (const a of actions) if (!copy.apiNext?.[a]) problems.push(`${lang}: apiNext.${a} is missing`);
    for (const s of steps) if (!copy.story?.step?.[s.key]) problems.push(`${lang}: story.step.${s.key} is missing`);
  }
  return problems;
}

/** Codes without copy in some locale, sorted. */
export function missingErrorCopy(codes, locales) {
  return [...codes].filter((c) => Object.values(locales).some((copy) => !copy.apiErrors?.[c])).sort();
}

/** Compare a shrink-only list: what is new (fails) and what is fixed but still listed (fails). */
export function compareList(current, baseline) {
  const listed = new Set(baseline);
  const now = new Set(current);
  return { added: [...now].filter((c) => !listed.has(c)).sort(), fixed: [...listed].filter((c) => !now.has(c)).sort() };
}

/** Compare per-file counts: grown (or new) files fail, shrunk files must lower their entry. */
export function compareCounts(current, baseline) {
  const grown = [];
  const shrunk = [];
  for (const [file, n] of Object.entries(current)) {
    const allowed = baseline[file] ?? 0;
    if (n > allowed) grown.push({ file, count: n, allowed });
  }
  for (const [file, allowed] of Object.entries(baseline)) {
    const n = current[file] ?? 0;
    if (n < allowed) shrunk.push({ file, count: n, allowed });
  }
  return { grown, shrunk };
}

/** The rule's per-file counts over src/v3 (tests aside), without a baseline. */
export async function disabledCounts(cwd = FRONTEND, patterns = ["src/v3"]) {
  const { ESLint } = await import("eslint");
  const { default: tseslint } = await import("typescript-eslint");
  const { default: plugin } = await import("../eslint-rules/index.js");
  const eslint = new ESLint({
    cwd,
    overrideConfigFile: true,
    overrideConfig: [
      {
        files: ["**/*.{ts,tsx}"],
        ignores: ["**/*.test.tsx", "**/*.test.ts"],
        languageOptions: { parser: tseslint.parser },
        plugins: { launchpad: plugin },
        linterOptions: { reportUnusedDisableDirectives: "off" },
        rules: { "launchpad/disabled-reason": "error" },
      },
    ],
  });
  const counts = {};
  for (const r of await eslint.lintFiles(patterns)) {
    const n = r.messages.filter((m) => m.ruleId === "launchpad/disabled-reason").length;
    if (n) counts[relative(cwd, r.filePath).split("\\").join("/")] = n;
  }
  return sortKeys(counts);
}

function sortKeys(obj) {
  return Object.fromEntries(Object.entries(obj).sort(([a], [b]) => a.localeCompare(b)));
}

/* ── main ─────────────────────────────────────────────────────────────────── */

async function main(argv) {
  const prune = argv.includes("--prune");
  const actionsSrc = read("src/v3/story/actions.ts");
  const actions = parseActionKeys(actionsSrc);
  const steps = parseSteps(read("src/v3/story/stories.ts"));
  const locales = { en: readJson("src/locales/en/common.json"), "zh-CN": readJson("src/locales/zh-CN/common.json") };
  const backend = backendScan();
  const problems = [];
  const sanity = [];
  if (actions.length < 10) sanity.push(`parsed only ${actions.length} NEXT_ACTIONS keys — has story/actions.ts changed shape?`);
  if (steps.length < 10) sanity.push(`parsed only ${steps.length} story steps — has story/stories.ts changed shape?`);
  if (backend.codes.size < 100) sanity.push(`parsed only ${backend.codes.size} backend error codes — is backend/app there?`);
  problems.push(...sanity);

  problems.push(...checkActions({ actions, steps, defaultNext: parseDefaultNext(actionsSrc), backendNext: [...backend.next] }));
  problems.push(...checkCopy({ actions, steps, locales }));

  const missing = missingErrorCopy(backend.codes, locales);
  const listed = existsSync(resolve(FRONTEND, API_ERRORS_BASELINE)) ? readJson(API_ERRORS_BASELINE) : [];
  const errs = compareList(missing, listed);
  for (const c of errs.added) problems.push(`apiErrors.${c}: backend raises it, en/zh-CN copy is missing (add the copy; the baseline ${API_ERRORS_BASELINE} only shrinks)`);
  if (prune && errs.fixed.length) {
    writeFileSync(resolve(FRONTEND, API_ERRORS_BASELINE), `${JSON.stringify(listed.filter((c) => !errs.fixed.includes(c)), null, 2)}\n`);
  } else {
    for (const c of errs.fixed) problems.push(`${API_ERRORS_BASELINE}: ${c} has its copy now (or is gone) — remove it (or run with --prune)`);
  }

  const counts = await disabledCounts();
  const baseline = existsSync(resolve(FRONTEND, DISABLED_BASELINE)) ? readJson(DISABLED_BASELINE) : {};
  const dis = compareCounts(counts, baseline);
  for (const g of dis.grown) problems.push(`${g.file}: ${g.count} disabled controls without a disabledReason, baseline allows ${g.allowed}`);
  if (prune && dis.shrunk.length) {
    const next = { ...baseline };
    for (const s of dis.shrunk) if (s.count) next[s.file] = s.count; else delete next[s.file];
    writeFileSync(resolve(FRONTEND, DISABLED_BASELINE), `${JSON.stringify(sortKeys(next), null, 2)}\n`);
  } else {
    for (const s of dis.shrunk) problems.push(`${DISABLED_BASELINE}: ${s.file} is down to ${s.count} (listed ${s.allowed}) — lower the entry (or run with --prune)`);
  }

  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  console.log(`story checks: ${actions.length} actions, ${steps.length} steps, ${backend.next.size} backend next actions, `
    + `${backend.codes.size} error codes (${missing.length} without copy, baselined), ${total} reasonless disabled controls (baselined)`);
  if (problems.length) {
    for (const p of problems) console.error(`  ✗ ${p}`);
    console.error(`story checks: FAIL (${problems.length})`);
    return 1;
  }
  console.log("story checks: OK");
  return 0;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2)).then((code) => process.exit(code), (err) => {
    console.error(err);
    process.exit(2);
  });
}
