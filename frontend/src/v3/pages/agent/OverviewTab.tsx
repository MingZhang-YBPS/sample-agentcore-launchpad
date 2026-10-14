import { useTranslation } from "react-i18next";

import type { AgentInfo } from "../../../lib/api";
import type { ReleaseState } from "../../../lib/dlc";
import type { Loaded } from "../../hooks";
import { Notice } from "../../ui";
import { BasicInfoCard } from "./BasicInfoCard";
import { ByocCard } from "./ByocCard";
import { ConvertedCard } from "./ConvertedCard";
import { KnowledgeCard } from "./KnowledgeCard";
import { LifecyclePanel } from "./LifecyclePanel";
import { McpServerPanel } from "./McpServerPanel";
import { StatsRow } from "./StatsRow";
import type { DetailSections } from "./tabs";
import { TryIt } from "./TryIt";

/** 概览: runtime state, lifecycle, stats, the agent's facts, try-it. */
export function OverviewTab({
  agent,
  sections,
  release,
}: {
  agent: AgentInfo;
  sections: DetailSections;
  release: Loaded<ReleaseState>;
}) {
  const { t } = useTranslation();
  return (
    <>
      {sections.systemNote && agent.system && (
        <Notice s="info">
          {t("v3.agent.systemNote", { label: agent.system.label, version: agent.system.skill_version ?? "—" })}
        </Notice>
      )}
      {sections.mcpPanel && <McpServerPanel agent={agent} />}
      {sections.lifecycle && <LifecyclePanel agent={agent} release={release} />}
      <StatsRow agent={agent} />
      <BasicInfoCard agent={agent} />
      {sections.byoc && <ByocCard agent={agent} />}
      {sections.converted && <ConvertedCard agent={agent} />}
      {sections.knowledgeBases && <KnowledgeCard agent={agent} />}
      {sections.tryIt && <TryIt agent={agent} />}
    </>
  );
}
