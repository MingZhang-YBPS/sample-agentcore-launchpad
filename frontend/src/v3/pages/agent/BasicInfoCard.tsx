import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { methodLabel } from "../../../components/methodChipMeta";
import type { AgentInfo } from "../../../lib/api";
import { Chip, Panel } from "../../ui";
import { Copyable } from "./Copyable";
import { when } from "./format";

/** Display name, resource name, id, method, protocol, creator, times, Registry record, ARN. */
export function BasicInfoCard({ agent }: { agent: AgentInfo }) {
  const { t } = useTranslation();
  const spec = (agent.spec ?? {}) as Record<string, unknown>;
  const rows: [string, ReactNode][] = [
    [t("v3.agent.basic.displayName"), agent.display_name || "—"],
    [t("v3.agent.basic.resourceName"), <Copyable key="name" value={agent.name} label={t("v3.agent.basic.resourceName")} />],
    [t("v3.agent.basic.id"), <Copyable key="id" value={agent.id} label={t("v3.agent.basic.id")} />],
    [t("v3.agent.basic.method"), <Chip key="method">{methodLabel(agent.method)}</Chip>],
    [t("v3.agent.basic.protocol"), String(spec.protocol ?? "http").toUpperCase()],
    [t("v3.agent.basic.creator"), agent.created_by_name || "—"],
    [t("v3.agent.basic.created"), when(agent.created_at)],
    [t("v3.agent.basic.updated"), when(agent.updated_at)],
    [
      t("v3.agent.basic.registry"),
      agent.registry_record_id ? (
        <Copyable key="reg" value={agent.registry_record_id} label={t("v3.agent.basic.registry")}>
          <Link to={`/v3/registry?id=${encodeURIComponent(agent.registry_record_id)}`} className="v3-inline-link">
            {agent.registry_record_id}
          </Link>
        </Copyable>
      ) : (
        "—"
      ),
    ],
    [t("v3.agent.basic.arn"), agent.arn ? <Copyable key="arn" value={agent.arn} label="ARN" /> : "—"],
  ];
  return (
    <Panel title={t("v3.agent.basic.title")}>
      <dl className="v3-kv" data-testid="v3-agent-basic">
        {rows.map(([label, value]) => (
          <div key={label} style={{ display: "contents" }}>
            <dt>{label}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
    </Panel>
  );
}
