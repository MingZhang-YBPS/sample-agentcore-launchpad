/**
 * Copy shared by the V3 and V2 "Tools & capabilities" cards (projects §18, §17.3-4),
 * so both say the same thing about telemetry, version drift and gateway policies.
 */
import type { TFunction } from "i18next";

import type { GatewayNotice, ToolsDiff } from "./agent-tools";
import type { AgentTelemetry } from "./api/agent-tools";

/** The telemetry line, and the extra hint when the cause is known. */
export function telemetryText(
  tel: AgentTelemetry,
  t: TFunction,
): { line: string; hint: string | null; ok: boolean; tone: "ok" | "wait" | "off" } {
  const line =
    tel.status === "receiving"
      ? t("agentTools.telemetry.receiving", { window: t(`agentTools.telemetry.window.${tel.last_span_within ?? "14d"}`) })
      : tel.status === "none"
        ? t("agentTools.telemetry.none", { days: tel.lookback_days })
        : t(`agentTools.telemetry.${tel.status}`);
  const hint =
    tel.status !== "none"
      ? null
      : tel.byoc_no_adot
        ? t("agentTools.telemetry.byocNoAdot")
        : tel.reason === "no_log_group"
          ? t("agentTools.telemetry.noLogGroup")
          : null;
  const ok = tel.status === "receiving";
  return { line, hint, ok, tone: ok ? "ok" : tel.status === "none" ? "wait" : "off" };
}

export function diffText(diff: ToolsDiff, versions: { candidateVersion: string; liveVersion: string }, t: TFunction) {
  const list = (items: { label: string }[]) => items.map((item) => item.label).join(", ");
  return {
    title: t("agentTools.diff.title", { candidate: versions.candidateVersion, live: versions.liveVersion }),
    added: diff.added.length ? t("agentTools.diff.added", { items: list(diff.added) }) : null,
    removed: diff.removed.length ? t("agentTools.diff.removed", { items: list(diff.removed) }) : null,
    hint: t("agentTools.diff.hint"),
  };
}

export function gatewayNoticeText(notice: GatewayNotice, gatewayName: string | null, t: TFunction): string[] {
  const lines: string[] = [];
  if (notice.uncovered.length) {
    lines.push(
      t("agentTools.enforce", { gateway: gatewayName ?? notice.gateway.gatewayId ?? notice.gateway.name, tools: notice.uncovered.join(", ") }),
    );
  }
  if (notice.recordStatus) {
    lines.push(t("agentTools.recordInactive", { name: notice.gateway.name, status: notice.recordStatus }));
  }
  return lines;
}

/** The governance page of the gateway's policies (hosted from V2, like the V3 governance page links). */
export const policiesUrl = (gatewayId: string) =>
  `/v2/governance?view=gateway&gateway=${encodeURIComponent(gatewayId)}&section=policies`;
