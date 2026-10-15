// Why a control is off (guided-console §4, storyline-ux §4) — the reasons themselves,
// pure functions of what the page knows: the missing story step (with its action as
// the way there), the missing permission (with who usually holds it), or plain text.
// The flag that shows them is reason.tsx; `reasons.test.ts` covers these.
import type { ApiNextStep } from "../lib/api";
import type { ActionParams } from "./story/actions";
import { AGENT_STORY, PLATFORM_STORY } from "./story/stories";

/** A reason: plain text, or text plus the step that fixes it. */
export type DisabledReason = string | { text: string; next?: ApiNextStep };

/** Who usually holds a permission (the same table as ErrorCallout's). */
const HOLDER: Record<string, string> = {
  "criteria.sign": "biz-owner",
  "criteria.manage": "developer",
  "golden.admit": "biz-owner",
  "judge.calibrate": "biz-owner",
  "release.sign": "release-ops",
  "waiver.approve": "release-ops",
  "promotion.approve": "release-ops",
  "mcp.approve": "release-ops",
  "eval.run": "developer",
  "agents.deploy": "developer",
};

type T = (key: string, opts?: Record<string, unknown>) => string;

const STEPS = [...AGENT_STORY, ...PLATFORM_STORY];

/** "Complete first: <step>" with the step's own action as the way there. */
export function stepReason(t: T, stepKey: string, params: ActionParams = {}): DisabledReason {
  const step = STEPS.find((s) => s.key === stepKey);
  if (!step) throw new Error(`unknown story step ${stepKey}`);
  const clean = Object.fromEntries(Object.entries(params).filter(([, v]) => v)) as Record<string, string>;
  return { text: t("story.reason.step", { step: t(step.labelKey) }), next: { action: step.action, params: clean } };
}

/** "Needs the <perm> permission, usually held by <role>." — administrators get the Users link. */
export function permReason(t: T, perm: string, isAdmin = false): DisabledReason {
  const key = perm.replace(/^perm:/, "");
  if (key === "admin") return t("story.reason.adminOnly");
  const role = HOLDER[key];
  const text = role
    ? t("story.callout.needs", { perm: key, role: t(`story.role.${role}`) })
    : t("story.callout.needsAnyone", { perm: key });
  return isAdmin ? { text, next: { action: "users.open", params: {} } } : text;
}

/** The first reason that applies, in the order a user would have to fix them. */
export function firstReason(...reasons: (DisabledReason | null | false | undefined)[]): DisabledReason | null {
  return reasons.find((r): r is DisabledReason => Boolean(r)) ?? null;
}

export function reasonText(r: DisabledReason): string {
  return typeof r === "string" ? r : r.text;
}

/** The agent exists but is not running (deploying, failed, stopped): open it to see why. */
export function agentNotRunning(t: T, agentId: string): DisabledReason {
  return { text: t("story.reason.agentNotRunning"), next: { action: "agent.open", params: { agent_id: agentId } } };
}
