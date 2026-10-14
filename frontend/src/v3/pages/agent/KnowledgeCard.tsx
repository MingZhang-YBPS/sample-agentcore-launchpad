import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { AgentInfo } from "../../../lib/api";
import { Panel } from "../../ui";

/** The knowledge bases mounted on the agent, each opening its V3 page. */
export function KnowledgeCard({ agent }: { agent: AgentInfo }) {
  const { t } = useTranslation();
  const kbs = ((agent.spec ?? {}) as { knowledge_bases?: { kb_id: string; name: string }[] }).knowledge_bases ?? [];
  if (!kbs.length) return null;
  return (
    <Panel title={t("v3.agent.kbs")}>
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        {kbs.map((kb) => (
          <Link key={kb.kb_id} to={`/v3/knowledge?id=${encodeURIComponent(kb.kb_id)}`} className="v3-chip">
            {kb.name}
          </Link>
        ))}
      </div>
    </Panel>
  );
}
