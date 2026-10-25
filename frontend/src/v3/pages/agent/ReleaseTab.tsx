import { useTranslation } from "react-i18next";

import type { AgentInfo } from "../../../lib/api";
import type { ReleaseState } from "../../../lib/dlc";
import { Notice } from "../../ui";
import { GateEntry } from "./GateEntry";
import { PromotionCard } from "./PromotionCard";
import type { DetailSections } from "./tabs";

/** 发布: the release gate entry and cross-workspace promotion. */
export function ReleaseTab({
  agent,
  sections,
  release,
  currentWorkspaceId,
}: {
  agent: AgentInfo;
  sections: DetailSections;
  release: ReleaseState | null;
  currentWorkspaceId: string | null;
}) {
  const { t } = useTranslation();
  return (
    <>
      {sections.gate && <GateEntry agentId={agent.id} release={release} />}
      {sections.promotion ? (
        <PromotionCard agent={agent} currentWorkspaceId={currentWorkspaceId} />
      ) : (
        <Notice s="info">{t(agent.system ? "v3.agent.promotion.system" : "v3.agent.promotion.inactive")}</Notice>
      )}
    </>
  );
}
