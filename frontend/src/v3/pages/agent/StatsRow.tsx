import { useTranslation } from "react-i18next";

import { toolsCount } from "../../../lib/agent-tools";
import type { AgentInfo } from "../../../lib/api";
import { ago } from "../../format";
import { Panel, Stat } from "../../ui";
import { isMcpServer } from "./tabs";

/** Version, model (or MCP kind), tool count (or MCP target), last update. */
export function StatsRow({ agent }: { agent: AgentInfo }) {
  const { t } = useTranslation();
  const spec = agent.spec as { model_id?: string; model?: string; mcp?: { target_name?: string } };
  const mcp = isMcpServer(agent);
  return (
    <div className="v3-grid c4">
      <Panel><Stat label={t("v3.agents.version")} value={`v${agent.version ?? "—"}`} /></Panel>
      {mcp ? (
        <Panel><Stat label={t("v3.mcp.kind")} value={<span style={{ fontSize: 15 }}>{t("v3.mcp.kindValue")}</span>} /></Panel>
      ) : (
        <Panel><Stat label={t("v3.agent.model")} value={<span className="mono" style={{ fontSize: 13, lineHeight: 1.45, wordBreak: "break-all", display: "block" }}>{spec.model_id ?? spec.model ?? "—"}</span>} /></Panel>
      )}
      {mcp ? (
        <Panel><Stat label={t("v3.mcp.target")} value={<span className="mono" style={{ fontSize: 15 }}>{spec.mcp?.target_name ?? "—"}</span>} /></Panel>
      ) : (
        <Panel><Stat label={t("v3.agent.tools")} value={<a href="#agent-tools">{toolsCount(agent)}</a>} /></Panel>
      )}
      <Panel><Stat label={t("v3.agents.updated")} value={ago(agent.updated_at)} /></Panel>
    </div>
  );
}
