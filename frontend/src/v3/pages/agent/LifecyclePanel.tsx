import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { AgentInfo } from "../../../lib/api";
import type { ReleaseState } from "../../../lib/dlc";
import type { Loaded } from "../../hooks";
import { agentSignal, releaseSignal } from "../../signals";
import { Notice, Panel, Skeleton } from "../../ui";
import { Lifecycle } from "./Lifecycle";

/** The lifecycle line, with the version waiting on the gate called out. */
export function LifecyclePanel({ agent, release }: { agent: AgentInfo; release: Loaded<ReleaseState> }) {
  const { t } = useTranslation();
  const pending = release.data?.pending ?? null;
  return (
    <Panel title={t("v3.agent.lifecycle")} signal={agentSignal(agent)}>
      {release.error ? (
        <Notice s="wait">
          {t("v3.ux.releaseFailed")} {release.error}
          <button type="button" className="v3-btn sm" onClick={release.reload}>{t("v3.ux.retry")}</button>
        </Notice>
      ) : !release.data ? (
        <Skeleton rows={2} />
      ) : (
        <Lifecycle agent={agent} release={release.data} />
      )}
      {pending && (
        <div style={{ marginTop: 14 }}>
          <Notice s={releaseSignal(pending.decision)}>
            {t("v3.agent.pending", { version: pending.candidate_version ?? "—" })}{" "}
            <Link to={`/v3/gate?agent=${agent.id}`} style={{ textDecoration: "underline" }}>{t("v3.agent.openGate")}</Link>
          </Notice>
        </div>
      )}
    </Panel>
  );
}
