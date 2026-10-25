import { type ReactNode, useRef } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { diffTools, gatewayNotices, toolsModel } from "../../../lib/agent-tools";
import { diffText, gatewayNoticeText, policiesUrl, telemetryText } from "../../../lib/agent-tools-text";
import { type AgentInfo, dlcApi } from "../../../lib/api";
import {
  type AgentTelemetry,
  agentTelemetry,
  loadToolsFacts,
  loadVersionPair,
  type ToolsFacts,
  type VersionPair,
} from "../../../lib/api/agent-tools";
import { useLoad } from "../../hooks";
import { Alert, Button, Card, Descriptions, Tag } from "../../ui";

/**
 * The V2 detail's read-only "Tools & capabilities" card (projects §18.3-4): the
 * same derivation, drift and warnings as the V3 overview's card
 * (`v3/pages/agent/ToolsCard.tsx`), in the V2 kit, so **Manage** shows them too.
 */
export function ToolsCard({ agent }: { agent: AgentInfo }) {
  const release = useLoad(() => dlcApi.release(agent.id).catch(() => null), `v2-agent-tools-rel:${agent.id}:${agent.version}`);
  const facts = useLoad(() => loadToolsFacts(agent), `v2-agent-tools:${agent.id}:${agent.version}:${agent.updated_at}`);
  const versions = useLoad(
    () => (release.data ? loadVersionPair(agent, release.data).catch(() => null) : Promise.resolve(null)),
    `v2-agent-tools-ver:${agent.id}:${agent.version}:${release.data?.state.live_version ?? ""}:${release.data?.release_mode ?? ""}`,
  );
  const bust = useRef(false);
  const telemetry = useLoad(() => {
    const refresh = bust.current;
    bust.current = false;
    return agentTelemetry(agent.id, refresh);
  }, `v2-agent-tel:${agent.id}:${agent.version}`);
  return (
    <ToolsCardV2View
      agent={agent}
      facts={facts.data}
      versions={versions.data}
      telemetry={telemetry.error ? "error" : telemetry.data}
      refreshing={facts.loading || telemetry.loading}
      onRefresh={() => {
        bust.current = true;
        facts.reload();
        telemetry.reload();
      }}
    />
  );
}

export function ToolsCardV2View({
  agent,
  facts,
  versions = null,
  telemetry,
  refreshing = false,
  onRefresh,
}: {
  agent: AgentInfo;
  facts: ToolsFacts | null;
  versions?: VersionPair | null;
  telemetry: AgentTelemetry | "error" | null;
  refreshing?: boolean;
  onRefresh?: () => void;
}) {
  const { t } = useTranslation();
  const model = toolsModel(agent, agent.spec);
  const diff = versions ? diffTools(toolsModel(agent, versions.candidateSpec), toolsModel(agent, versions.liveSpec)) : null;
  const drift = diff && (diff.added.length || diff.removed.length) ? diff : null;
  const added = new Set(drift?.added.map((item) => item.key) ?? []);
  const notices = facts ? gatewayNotices(model, facts.gateways, facts.records) : [];
  const tag = (key: string, label: string, title?: string) => (
    <Tag key={key} tone={added.has(key) ? "blue" : "outline"} title={added.has(key) ? t("agentTools.diff.new") : title}>
      <span className="mono">{label}</span>
      {added.has(key) && <> · {t("agentTools.diff.new")}</>}
    </Tag>
  );
  const tags = (children: ReactNode) => <div className="v2-tags">{children}</div>;
  const items: { label: string; value: ReactNode }[] = [];
  if (model.gateway.length)
    items.push({
      label: t("agentTools.group.gateway"),
      value: (
        <div style={{ display: "grid", gap: 6 }}>
          {model.gateway.map((gw) => {
            const gatewayName = gw.gatewayId ? facts?.gateways[gw.gatewayId]?.name : null;
            const status = gw.recordId ? facts?.records[gw.recordId] : undefined;
            return (
              <div key={gw.name} style={{ display: "grid", gap: 4 }}>
                <span className="v2-row">
                  <span className="mono">{gw.name}</span>
                  {gatewayName && <span className="v2-muted">{t("agentTools.gatewayVia", { gateway: gatewayName })}</span>}
                  {status && <Tag tone={status === "APPROVED" ? "green" : "orange"}>{status}</Tag>}
                </span>
                {tags(
                  gw.tools ? (
                    gw.tools.map((tool) => tag(`gateway:${gw.name}:${tool}`, tool))
                  ) : (
                    <>
                      {tag(`gateway:${gw.name}:${gw.scope}`, gw.scope)}
                      <span className="v2-muted">{t("agentTools.gatewayAll")}</span>
                    </>
                  ),
                )}
              </div>
            );
          })}
        </div>
      ),
    });
  if (model.mcp.length)
    items.push({
      label: t("agentTools.group.mcp"),
      value: (
        <div style={{ display: "grid", gap: 4 }}>
          {model.mcp.map((server) => (
            <span key={server.name} className="v2-row">
              {tag(`mcp:${server.name}`, server.name)}
              {server.url && <span className="mono" style={{ wordBreak: "break-all" }}>{server.url}</span>}
              <span className="v2-muted">
                {server.auth
                  ? t("agentTools.mcpAuth", { kind: server.auth.kind, mode: server.auth.mode })
                  : facts?.mcpAuth[server.name]
                    ? t("agentTools.mcpAuthRegistry", { kind: facts.mcpAuth[server.name] })
                    : t("agentTools.mcpAuthNone")}
              </span>
            </span>
          ))}
        </div>
      ),
    });
  if (model.builtin.length)
    items.push({ label: t("agentTools.group.builtin"), value: tags(model.builtin.map((name) => tag(`builtin:${name}`, name))) });
  if (model.rest.length)
    items.push({
      label: t("agentTools.group.rest"),
      value: tags(model.rest.map((api) => tag(`rest:${api.name}`, api.host ? `${api.name} · ${api.host}` : api.name))),
    });
  if (model.kbs.length)
    items.push({
      label: t("agentTools.group.kb"),
      value: tags(
        <>
          {model.kbs.map((kb) => tag(`kb:${kb.kbId}`, kb.name))}
          {model.kbTools.length > 0 && <span className="v2-muted">{t("agentTools.kbTools", { tools: model.kbTools.join(", ") })}</span>}
        </>,
      ),
    });
  if (model.skills.length)
    items.push({ label: t("agentTools.group.skill"), value: tags(model.skills.map((skill) => tag(`skill:${skill.path}`, skill.name, skill.path))) });
  if (model.toolkits.length)
    items.push({
      label: t("agentTools.group.toolkit"),
      value: tags(model.toolkits.map((kit) => tag(`toolkit:${kit.name}`, kit.name, kit.tools.join(", ")))),
    });
  if (model.native.length)
    items.push({
      label: t("agentTools.group.native"),
      value: tags(model.native.map((name) => tag(`native:${name}`, t(`agentTools.native.${name}`, { defaultValue: name })))),
    });
  if (model.template.length)
    items.push({
      label: t("agentTools.group.template"),
      value: tags(
        <>
          {model.template.map((name) => tag(`template:${name}`, name))}
          <span className="v2-muted">{t("agentTools.templateHint")}</span>
        </>,
      ),
    });
  if (model.allowed || model.allowedDerived)
    items.push({
      label: t("agentTools.group.allowed"),
      value: model.allowed ? (
        tags(
          model.allowed.length ? (
            model.allowed.map((entry) => (
              <Tag key={entry.pattern} tone={entry.source ? "blue" : "outline"}>
                <span className="mono">{entry.pattern}</span>
                {entry.source && <> · {t(`agentTools.allowedFrom.${entry.source}`)}</>}
              </Tag>
            ))
          ) : (
            <span className="v2-muted">{t("agentTools.allowedNone")}</span>
          ),
        )
      ) : (
        <span className="v2-muted">{t("agentTools.allowedDerived")}</span>
      ),
    });
  if (model.codeDefined)
    items.push({
      label: t("agentTools.group.code"),
      value: (
        <span>
          {t("agentTools.code")} <Link to="/v2/observability">{t("agentTools.openObservability")}</Link>
        </span>
      ),
    });
  const tel = telemetry && telemetry !== "error" ? telemetryText(telemetry, t) : null;
  items.push({
    label: t("agentTools.telemetry.label"),
    value:
      telemetry === "error" ? (
        <span className="v2-muted">{t("agentTools.telemetry.unknown")}</span>
      ) : !tel ? (
        <span className="v2-muted">…</span>
      ) : (
        <span className="v2-row" data-testid="v2-agent-tools-telemetry">
          <Tag tone={tel.tone === "ok" ? "green" : tel.tone === "wait" ? "orange" : "gray"}>{tel.line}</Tag>
          {tel.hint && <span className="v2-muted">{tel.hint}</span>}
        </span>
      ),
  });

  return (
    <Card
      title={`${t("agentTools.title")} · ${model.items.length}`}
      testId="v2-agent-tools"
      end={onRefresh && <Button size="sm" onClick={onRefresh} disabled={refreshing}>{t("agentTools.refresh")}</Button>}
    >
      <div style={{ display: "grid", gap: 10 }}>
        {drift && versions && (
          <Alert tone="warn">
            {(() => {
              const text = diffText(drift, versions, t);
              return (
                <span data-testid="v2-agent-tools-diff">
                  <strong>{text.title}</strong> {text.added} {text.removed} {text.hint}
                </span>
              );
            })()}
          </Alert>
        )}
        {notices.map((notice) => (
          <Alert key={notice.gateway.name} tone="warn">
            {gatewayNoticeText(notice, notice.gateway.gatewayId ? facts?.gateways[notice.gateway.gatewayId]?.name ?? null : null, t).join(" ")}{" "}
            {notice.uncovered.length > 0 && notice.gateway.gatewayId && (
              <Link to={policiesUrl(notice.gateway.gatewayId)}>{t("agentTools.openPolicies")}</Link>
            )}
          </Alert>
        ))}
        {model.items.length === 0 && !model.codeDefined && !model.allowed && (
          <span className="v2-muted">{t("agentTools.empty")}</span>
        )}
        <Descriptions one items={items} />
      </div>
    </Card>
  );
}
