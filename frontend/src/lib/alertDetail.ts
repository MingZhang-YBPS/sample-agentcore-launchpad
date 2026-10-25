/**
 * An alert rule's (or a drift-watch schedule's) `last_detail` in the console's language.
 *
 * The backend (`app/services/alerts.py`) records one English line per evaluation —
 * the same line rides on the webhook, so it stays English there — and the console
 * renders it through this table. Unknown lines (a future template, a free-form
 * error) fall back to the original text.
 */
type Translate = (key: string, options?: Record<string, unknown>) => string;

type Rule = [RegExp, (m: RegExpExecArray, t: Translate) => string];

const RULES: Rule[] = [
  [/^p95 over (\d+) traces$/, (m, t) => t("alertDetail.p95", { n: Number(m[1]) })],
  [/^(\d+)\/(\d+) traces failed$/, (m, t) => t("alertDetail.errors", { errors: Number(m[1]), n: Number(m[2]) })],
  [/^no traffic in this window$/, (_m, t) => t("alertDetail.noTraffic")],
  [/^no latency percentile in this window$/, (_m, t) => t("alertDetail.noLatency")],
  [/^window '([^']*)' is not an observability range$/, (m, t) => t("alertDetail.badWindow", { window: m[1] })],
  [/^could not read telemetry: (.+)$/, (m, t) => t("alertDetail.telemetryFailed", { error: m[1] })],
  [/^could not read online evaluation: (.+)$/, (m, t) => t("alertDetail.onlineFailed", { error: m[1] })],
  [/^no online evaluation scores in the last 24 h$/, (_m, t) => t("alertDetail.noScores")],
  [/^(\d+) score\(s\) over (\d+) session\(s\)$/, (m, t) => t("alertDetail.scores", { scores: Number(m[1]), sessions: Number(m[2]) })],
  [/^could not estimate spend: (.+)$/, (m, t) => t("alertDetail.spendFailed", { error: m[1] })],
  [/^could not read billed spend: (.+)$/, (m, t) => t("alertDetail.billedFailed", { error: m[1] })],
  [/^could not read workspace (\S+): (.+)$/, (m, t) => t("alertDetail.workspaceFailed", { workspace: m[1], error: m[2] })],
  [/^month-to-date estimate from the price map(?: \((.+)\))?$/, (m, t) => m[1] ? t("alertDetail.estimateParts", { parts: m[1] }) : t("alertDetail.estimate")],
  [/^month-to-date billed cost \(Cost Explorer, launchpad:project\)(?: \((.+)\))?$/, (m, t) => m[1] ? t("alertDetail.billedParts", { parts: m[1] }) : t("alertDetail.billed")],
  [/^unknown kind '([^']*)'$/, (m, t) => t("alertDetail.unknownKind", { kind: m[1] })],
  // drift-watch schedule lines (app/dlc/watch.py, app/dlc/scheduler.py)
  [/^run (\w+)$/, (m, t) => t("alertDetail.watchRun", { id: m[1] })],
  [/^project archived$/, (_m, t) => t("alertDetail.projectArchived")],
  [/^estimated \$([\d.]+) exceeds the configured ceiling \$([\d.]+)$/, (m, t) => t("alertDetail.overCeiling", { estimate: m[1], ceiling: m[2] })],
];

function one(t: Translate, text: string): string {
  for (const [re, render] of RULES) {
    const m = re.exec(text);
    if (m) return render(m, t);
  }
  // "<workspace>: <detail>" — a multi-workspace budget names the workspace that failed
  const prefixed = /^([a-z0-9][\w-]*): (.+)$/.exec(text);
  if (prefixed) {
    const inner = one(t, prefixed[2]);
    if (inner !== prefixed[2]) return `${prefixed[1]}: ${inner}`;
  }
  return text;
}

export function alertDetailText(t: Translate, text: string | null | undefined): string {
  if (!text) return text ?? "";
  // "<detail> · webhook <outcome>" — the delivery result is appended after a breach
  const hook = / · webhook (.+)$/.exec(text);
  if (hook) {
    return `${one(t, text.slice(0, hook.index))} · ${t("alertDetail.webhook", { outcome: hook[1] })}`;
  }
  return one(t, text);
}
