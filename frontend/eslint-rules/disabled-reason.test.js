// `launchpad/disabled-reason` (guided-console §4): which disabled controls need a
// reason, which are exempt, and how the shrink-only baseline gates `npm run lint`.
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { RuleTester } from "eslint";
import tseslint from "typescript-eslint";
import { afterAll, describe, expect, it } from "vitest";

import rule, { inFlightOnly } from "./disabled-reason.js";

RuleTester.describe = describe;
RuleTester.it = it;
RuleTester.afterAll = afterAll;

const tester = new RuleTester({
  languageOptions: { parser: tseslint.parser, parserOptions: { ecmaFeatures: { jsx: true } } },
});

const missing = (name) => ({ messageId: "missing", data: { name } });

tester.run("disabled-reason", rule, {
  valid: [
    // the Btn prop, and a native control in the wrapper
    { code: `<Btn disabled={!agentId} disabledReason={t("story.reason.pickAgent")}>Go</Btn>` },
    { code: `<Reasoned disabledReason={why}><select disabled={!mayEdit} /></Reasoned>` },
    // locked only while a request runs needs no reason
    { code: `<Btn disabled={busy}>Save</Btn>` },
    { code: `<button disabled={saving || loading}>Save</button>` },
    { code: `<Btn disabled={busy === row.id}>Retry</Btn>` },
    { code: `<Btn disabled={!!state.busy}>Retry</Btn>` },
    { code: `<Btn disabled={mutation.isPending}>Retry</Btn>` },
    { code: `<Btn disabled={Boolean(lineage) && served.loading}>Template</Btn>` },
    { code: `<Btn busy={busy}>Save</Btn>` },
    // nothing to explain
    { code: `<Btn disabled={false}>Go</Btn>` },
    { code: `<select><option disabled value="">Pick</option></select>` },
    { code: `<Confirm confirmDisabled={!typed} confirmReason={t("story.reason.typeName")} />` },
  ],
  invalid: [
    { code: `<Btn disabled={!agentId}>Go</Btn>`, errors: [missing("Btn")] },
    { code: `<button disabled>Go</button>`, errors: [missing("button")] },
    // busy beside a real lock does not explain the lock
    { code: `<Btn busy={busy} disabled={!name.trim()}>Save</Btn>`, errors: [missing("Btn")] },
    { code: `<Btn disabled={busy || !can("eval.run")}>Run</Btn>`, errors: [missing("Btn")] },
    // the wrapper must carry the reason, and be the direct parent
    { code: `<Reasoned><input disabled={!ready} /></Reasoned>`, errors: [missing("input")] },
    { code: `<Reasoned disabledReason={why}><label><input disabled={!ready} /></label></Reasoned>`, errors: [missing("input")] },
    { code: `<Confirm confirmDisabled={!typed} />`, errors: [missing("Confirm confirmDisabled")] },
  ],
});

describe("the in-flight heuristic", () => {
  const expr = (src) => tseslint.parser.parseForESLint(`(${src})`, { range: true }).ast.body[0].expression;
  it("names that only mean a request is running", () => {
    for (const src of ["busy", "isBusy", "rowBusy", "busyHere", "saving", "props.loading", "deleting === id", "busy !== null"]) {
      expect(inFlightOnly(expr(src)), src).toBe(true);
    }
  });
  it("anything that can be true without a request running", () => {
    for (const src of ["!ready", "missing", "approvalPending", "busy || !valid", "!busy", "rows.length === 0"]) {
      expect(inFlightOnly(expr(src)), src).toBe(false);
    }
  });
});

describe("the baseline (only shrinks)", () => {
  const dir = mkdtempSync(join(tmpdir(), "lp-disabled-reason-"));
  const baseline = join(dir, "baseline.json");
  writeFileSync(baseline, JSON.stringify({ "src/v3/Old.tsx": 2 }));
  const two = `<><Btn disabled={!a}>A</Btn><Btn disabled={!b}>B</Btn></>`;
  const three = `<><Btn disabled={!a}>A</Btn><Btn disabled={!b}>B</Btn><Btn disabled={!c}>C</Btn></>`;
  const over = { messageId: "overBaseline" };
  // the rule keys files by their path from the working directory (frontend/ under npm)
  const at = (rel) => join(process.cwd(), rel);
  const opts = [{ baseline }];
  new RuleTester({
    languageOptions: { parser: tseslint.parser, parserOptions: { ecmaFeatures: { jsx: true } } },
  }).run("disabled-reason (baseline)", rule, {
    valid: [
      // a file at (or under) its count passes `npm run lint`
      { code: two, filename: at("src/v3/Old.tsx"), options: opts },
    ],
    invalid: [
      // one more than the baseline: every place in the file is reported
      { code: three, filename: at("src/v3/Old.tsx"), options: opts, errors: [over, over, over] },
      // a file the baseline does not list allows none
      { code: `<Btn disabled={!a}>A</Btn>`, filename: at("src/v3/New.tsx"), options: opts, errors: [over] },
    ],
  });
});
