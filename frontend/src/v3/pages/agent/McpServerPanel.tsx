import { RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { api, type AgentInfo } from "../../../lib/api";
import type { McpServerStatusWithReview } from "../../../lib/api/mcpReviews";
import { useLoad } from "../../hooks";
import { Chip, Empty, Notice, Panel, type Signal, Skeleton, Stat } from "../../ui";
import { McpReviewSection } from "./McpReviewSection";

const TARGET_SIGNAL = (status: string | null): Signal =>
  status === "READY" ? "ok" : status === "FAILED" || status === "MISSING" || status?.endsWith("UNSUCCESSFUL") ? "act" : status ? "wait" : "off";

/**
 * An MCP server is a tool, not an agent: no chat, no gate. What matters is its
 * Gateway target, the tools agents see through it (`<target>___<tool>`) and the
 * tool review that decides whether the target is served at all (§15.6).
 */
export function McpServerPanel({ agent }: { agent: AgentInfo }) {
  const { t } = useTranslation();
  const status = useLoad(
    () => api.agentMcp(agent.id) as Promise<McpServerStatusWithReview>,
    `v3-agent-mcp:${agent.id}:${agent.status}:${agent.version}`,
  );
  const d = status.data;
  return (
    <Panel
      title={t("v3.mcp.title")}
      signal={TARGET_SIGNAL(d?.target_status ?? null)}
      end={<button type="button" className="v3-btn ghost sm" onClick={status.reload} disabled={status.loading}><RefreshCw size={13} /> {t("v3.ux.refresh")}</button>}
    >
      {status.error ? (
        <Notice s="act"><ApiErrorDetail error={status.failure ?? status.error} /></Notice>
      ) : !d ? (
        <Skeleton rows={3} />
      ) : (
        <div style={{ display: "grid", gap: 14 }} data-testid="v3-mcp-panel">
          <div className="v3-grid c3">
            <Stat label={t("v3.mcp.target")} value={<span className="mono" style={{ fontSize: 15 }}>{d.target_name || "—"}</span>} />
            <Stat
              label={t("v3.mcp.status")}
              value={<Chip s={TARGET_SIGNAL(d.target_status)}>{d.target_status ?? t("v3.mcp.notWired")}</Chip>}
              foot={d.status_reasons.join("; ") || undefined}
            />
            <Stat label={t("v3.mcp.tools")} value={d.tools.length} />
          </div>
          {d.endpoint && (
            <div>
              <div className="v3-hint">{t("v3.mcp.endpoint")}</div>
              <code className="mono" style={{ wordBreak: "break-all", fontSize: 12 }}>{d.endpoint}</code>
            </div>
          )}
          {d.tools_error && <Notice s="wait">{t("v3.mcp.toolsError")} {d.tools_error}</Notice>}
          {d.tools.length > 0 ? (
            <table className="v3-table">
              <thead><tr><th>{t("v3.mcp.toolName")}</th><th>{t("v3.mcp.toolDesc")}</th></tr></thead>
              <tbody>
                {d.tools.map((tool) => (
                  <tr key={tool.name}><td className="mono">{tool.name}</td><td style={{ color: "var(--v3-text-2)" }}>{tool.description || "—"}</td></tr>
                ))}
              </tbody>
            </table>
          ) : d.target_status === "READY" && !d.tools_error ? (
            <Empty title={t("v3.mcp.noTools")} />
          ) : null}
          <McpReviewSection agentId={agent.id} drift={Boolean(d.review?.drift)} onChanged={status.reload} />
          <Notice s="info">
            {t("v3.mcp.howToUse", { target: d.target_name })}{" "}
            <Link to="/v3/governance" style={{ textDecoration: "underline" }}>{t("v3.mcp.openGovernance")}</Link>
          </Notice>
        </div>
      )}
    </Panel>
  );
}
