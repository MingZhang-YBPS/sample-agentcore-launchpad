import { useTranslation } from "react-i18next";

import type { AgentInfo } from "../../../lib/api";
import type { ReleaseState } from "../../../lib/dlc";
import { gateSignal } from "../../signals";
import { Lamp, type Signal } from "../../ui";

/**
 * The path a version takes, drawn as one line: built → on candidate → through the
 * gate → serving. Where the line stops is where the agent is; the colour of the
 * last lit node says whether that is fine.
 */
export function Lifecycle({ agent, release }: { agent: AgentInfo; release: ReleaseState | null }) {
  const { t } = useTranslation();
  const state = release?.state;
  const pending = release?.pending ?? null;
  const verdict = (pending?.gate_report as { verdict?: "PASS" | "BLOCKED" | "INVALID" } | undefined)?.verdict;
  const gated = state?.endpoint_mode === "live";
  const nodes: { key: string; label: string; detail: string; s: Signal }[] = [
    {
      key: "build",
      label: t("v3.life.build"),
      detail: agent.status === "deploying" ? t("v3.life.building") : `v${agent.version ?? "—"}`,
      s: agent.status === "failed" ? "act" : agent.status === "deploying" ? "wait" : agent.status === "active" ? "ok" : "off",
    },
    {
      key: "candidate",
      label: t("v3.life.candidate"),
      detail: gated ? (state?.candidate_version ? `v${state.candidate_version}` : "—") : t("v3.life.skipped"),
      s: !gated ? "off" : pending ? "wait" : "ok",
    },
    {
      key: "gate",
      label: t("v3.life.gate"),
      detail: !gated ? t("v3.life.skipped") : verdict ?? (pending ? t("v3.life.notRun") : t("v3.life.clear")),
      s: !gated ? "off" : verdict ? gateSignal(verdict) : pending ? "wait" : "ok",
    },
    {
      key: "live",
      label: t("v3.life.live"),
      detail: gated ? `v${state?.live_version ?? "—"}` : `v${agent.version ?? "—"} · DEFAULT`,
      s: agent.status === "active" ? "ok" : "off",
    },
  ];
  return (
    <div style={{ display: "grid", gridTemplateColumns: `repeat(${nodes.length}, 1fr)`, gap: 0, position: "relative" }}>
      {nodes.map((n, i) => (
        <div key={n.key} style={{ position: "relative", padding: "6px 12px 4px 0" }}>
          {i < nodes.length - 1 && (
            <div
              aria-hidden="true"
              style={{
                position: "absolute", top: 13, left: 20, right: 0, height: 2,
                background: n.s === "ok" ? "linear-gradient(90deg, var(--v3-ok), var(--v3-line-2))" : "var(--v3-line)",
              }}
            />
          )}
          <div style={{ position: "relative", display: "flex", alignItems: "center", gap: 10 }}>
            <Lamp s={n.s} live={n.key === "live" && n.s === "ok"} />
          </div>
          <div className="v3-stat" style={{ marginTop: 12 }}>
            <div className="label">{n.label}</div>
            <div className="mono" style={{ marginTop: 4, color: n.s === "off" ? "var(--v3-text-3)" : "var(--v3-text)" }}>
              {n.detail}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
