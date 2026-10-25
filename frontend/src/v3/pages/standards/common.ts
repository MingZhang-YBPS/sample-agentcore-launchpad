// Agent-DLC on the V3 signal vocabulary. lib/dlc.ts speaks V2 tag tones; every
// tone is mapped onto a Signal exactly once, here, so a verdict, a tier and a κ
// band read the same way everywhere on the page.
import type { GateRowVerdict, GateVerdict, ReleaseRecord, Scorecard } from "../../../lib/dlc";
import { gateTone, kappaTone, tierTone, type DlcTier } from "../../../lib/dlc";
import type { Signal } from "../../ui";

const TONE: Record<string, Signal> = { green: "ok", red: "act", orange: "wait", blue: "info", gray: "off" };

export const toneSignal = (tone: string): Signal => TONE[tone] ?? "off";
export const gateSignal = (v: GateRowVerdict | GateVerdict | null): Signal => toneSignal(gateTone(v));
export const tierSignal = (tier: DlcTier): Signal => toneSignal(tierTone(tier));
export const kappaSignal = (kappa: number | null): Signal => toneSignal(kappaTone(kappa));

export function decisionSignal(decision: ReleaseRecord["decision"] | null | undefined): Signal {
  if (decision === "released") return "ok";
  if (decision === "blocked" || decision === "invalid") return "act";
  if (decision === "rolled_back" || decision === "pending" || decision === "open" || decision === "evaluating") return "wait";
  return "off";
}

/** A rate with one decimal: gate thresholds differ in tenths, so 95% vs 95.4% matters. */
export const rate = (value: number | null | undefined, digits = 1): string =>
  value === null || value === undefined ? "—" : `${(value * 100).toFixed(digits)}%`;

/** ISO timestamp → "YYYY-MM-DD HH:MM" (absolute: an audit needs the exact time, not "3d"). */
export const stamp = (iso: string | null | undefined, len = 16): string => iso?.slice(0, len).replace("T", " ") ?? "—";

export const VIEWS = [
  "scorecard",
  "criteria",
  "golden",
  "admission",
  "calibration",
  "release",
  "watch",
  "compare",
  "audit",
] as const;
export type View = (typeof VIEWS)[number];

export interface Attention {
  key: string;
  s: "act" | "wait";
  text: string;
  view: string;
}

/** What on this agent's standard needs a person, most urgent first. */
export function attentionOf(card: Scorecard, t: (k: string, o?: Record<string, unknown>) => string): Attention[] {
  const out: Attention[] = [];
  if (!card.alerts.quiet) out.push({ key: "alerts", s: "act", text: t("v3.standards.att.alerts", { n: card.alerts.firing }), view: "watch" });
  if (card.last_gate === "BLOCKED" || card.last_gate === "INVALID")
    out.push({ key: "gate", s: card.last_gate === "BLOCKED" ? "act" : "wait", text: t("v3.standards.att.gate", { v: card.last_gate }), view: "release" });
  if (!card.criteria_set) out.push({ key: "nocriteria", s: "wait", text: t("v3.standards.att.noCriteria"), view: "criteria" });
  else if (!card.criteria_set.signed_by) out.push({ key: "unsigned", s: "wait", text: t("v3.standards.att.unsigned", { v: card.criteria_set.version }), view: "criteria" });
  if (card.calibration_debt.length > 0)
    out.push({ key: "calib", s: "wait", text: t("v3.standards.att.uncalibrated", { n: card.calibration_debt.length }), view: "calibration" });
  if (card.open_waivers.length > 0) out.push({ key: "waivers", s: "wait", text: t("v3.standards.att.waivers", { n: card.open_waivers.length }), view: "release" });
  const thin = (card.coverage?.criteria ?? []).filter((c) => c.thin).length;
  if (thin > 0) out.push({ key: "thin", s: "wait", text: t("v3.standards.att.thin", { n: thin }), view: "golden" });
  return out;
}


/* ── backend copy ─────────────────────────────────────────────────────────── */

type Translate = (key: string, options?: Record<string, unknown>) => string;

/** A backend English message shown in the console, in the console's language: the
 * gate's row reasons, the ladder's incomparable reason, the watch's alert details.
 * Unknown text passes through unchanged. */
export function dlcText(t: Translate, text: string | null | undefined): string {
  if (!text) return text ?? "";
  let m = /^(\d+) evaluation error\(s\)$/.exec(text);
  if (m) return t("v3.dlcText.msg.evaluationErrors", { n: Number(m[1]) });
  m = /^(\d+) of (\d+) expected items have no verdict$/.exec(text);
  if (m) return t("v3.dlcText.msg.noVerdict", { missing: Number(m[1]), expected: Number(m[2]) });
  m = /^(\d+) violation\(s\) — red lines are never waived$/.exec(text);
  if (m) return t("v3.dlcText.msg.redlineViolations", { n: Number(m[1]) });
  return t(`v3.dlcText.msg.${text}`, { defaultValue: text });
}

/** A criteria-table finding (error or warning) in the console's language. */
export function findingText(t: Translate, f: { code: string; message: string }): string {
  const dim = /^no '(\w+)' criterion/.exec(f.message)?.[1];
  return t(`v3.dlcText.finding.${f.code}`, {
    defaultValue: f.message,
    raw: dim ?? "",
    dimension: dim ? t(`v2.dlc.dimension.${dim}`, { defaultValue: dim }) : "",
  });
}

/** What a golden-set source is missing, in the console's language. */
export function biasText(t: Translate, origin: string, bias: string): string {
  return t(`v3.dlcText.msg.bias.${origin}`, { defaultValue: bias });
}
