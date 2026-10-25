import { Scale } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { ReleaseState } from "../../../lib/dlc";
import { releaseSignal } from "../../signals";
import { Notice, Panel } from "../../ui";

/** The release gate entry, with the candidate waiting on it called out. */
export function GateEntry({ agentId, release }: { agentId: string; release: ReleaseState | null }) {
  const { t } = useTranslation();
  const pending = release?.pending ?? null;
  return (
    <Panel title={t("v3.agent.gateEntry.title")}>
      <div style={{ display: "grid", gap: 12 }}>
        <div style={{ display: "flex", gap: 16, alignItems: "center", justifyContent: "space-between", flexWrap: "wrap" }}>
          <span className="v3-hint">{t("v3.agent.gateEntry.body")}</span>
          <Link to={`/v3/gate?agent=${agentId}`} className="v3-btn"><Scale size={14} /> {t("v3.agent.gateEntry.open")}</Link>
        </div>
        {pending && (
          <Notice s={releaseSignal(pending.decision)}>{t("v3.agent.pending", { version: pending.candidate_version ?? "—" })}</Notice>
        )}
      </div>
    </Panel>
  );
}
