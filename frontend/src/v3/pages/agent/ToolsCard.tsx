import { RefreshCw } from "lucide-react";
import { type ReactNode, useRef } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { diffTools, gatewayNotices, type ToolItem, toolsModel } from "../../../lib/agent-tools";
import { diffText, gatewayNoticeText, policiesUrl, telemetryText } from "../../../lib/agent-tools-text";
import type { AgentInfo } from "../../../lib/api";
import {
  type AgentTelemetry,
  agentTelemetry,
  loadToolsFacts,
  loadVersionPair,
  type ToolsFacts,
  type VersionPair,
} from "../../../lib/api/agent-tools";
import type { ReleaseState } from "../../../lib/dlc";
import { useLoad } from "../../hooks";
import { Chip, Empty, Notice, Panel } from "../../ui";

/**
 * Tools & capabilities (projects §18): everything the agent can call — Gateway
 * tools, remote MCP servers, built-in / REST tools, knowledge bases, skills, demo
 * toolkits, native execution, template tools, a Harness's `allowed_tools` — with
 * what the candidate adds over live in a gated workspace, the gateways that would
 * deny a tool by default, and whether telemetry arrives (§17.3-4).
 */
export function ToolsCard({ agent, release }: { agent: AgentInfo; release: ReleaseState | null }) {
  const versionKey = `${release?.release_mode ?? ""}:${release?.state.live_version ?? ""}:${release?.state.candidate_version ?? ""}`;
  const facts = useLoad(() => loadToolsFacts(agent), `v3-agent-tools:${agent.id}:${agent.version}:${agent.updated_at}`);
  // the drift is best-effort: no snapshot of the live version means no notice
  const versions = useLoad(() => loadVersionPair(agent, release).catch(() => null), `v3-agent-tools-ver:${agent.id}:${agent.version}:${versionKey}`);
  // the refresh button probes again; a page open reads the backend's cached answer
  const bust = useRef(false);
  const telemetry = useLoad(() => {
    const refresh = bust.current;
    bust.current = false;
    return agentTelemetry(agent.id, refresh);
  }, `v3-agent-tel:${agent.id}:${agent.version}`);
  const refresh = () => {
    bust.current = true;
    facts.reload();
    telemetry.reload();
  };
  return (
    <ToolsCardView
      agent={agent}
      facts={facts.data}
      versions={versions.data}
      telemetry={telemetry.error ? "error" : telemetry.data}
      refreshing={facts.loading || telemetry.loading}
      onRefresh={refresh}
    />
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div style={{ display: "contents" }}>
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

const wrap = { display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" } as const;

/** The card's body from the spec and the loaded facts (static-renderable for tests). */
export function ToolsCardView({
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
  const chip = (item: Pick<ToolItem, "key" | "label">, title?: string) => (
    <Chip key={item.key} s={added.has(item.key) ? "info" : undefined} title={added.has(item.key) ? t("agentTools.diff.new") : title}>
      <span className="mono">{item.label}</span>
      {added.has(item.key) && <span style={{ marginLeft: 6 }}>· {t("agentTools.diff.new")}</span>}
    </Chip>
  );
  const tel = telemetry && telemetry !== "error" ? telemetryText(telemetry, t) : null;
  const empty = model.items.length === 0 && !model.codeDefined && !model.allowed;

  return (
    <Panel
      title={
        <span style={{ display: "inline-flex", gap: 10, alignItems: "center" }}>
          {t("agentTools.title")} <Chip>{model.items.length}</Chip>
        </span>
      }
      end={
        onRefresh && (
          <button type="button" className="v3-btn ghost sm" onClick={onRefresh} disabled={refreshing}>
            <RefreshCw size={13} /> {t("agentTools.refresh")}
          </button>
        )
      }
    >
      <div id="agent-tools" style={{ display: "grid", gap: 14 }} data-testid="v3-agent-tools">
        {drift && versions && (
          <Notice s="wait">
            {(() => {
              const text = diffText(drift, versions, t);
              return (
                <span data-testid="v3-agent-tools-diff">
                  <strong>{text.title}</strong> {text.added} {text.removed} <span className="v3-hint">{text.hint}</span>
                </span>
              );
            })()}
          </Notice>
        )}
        {notices.map((notice) => (
          <Notice key={notice.gateway.name} s="wait">
            <span data-testid="v3-agent-tools-gateway-warning">
              {gatewayNoticeText(notice, notice.gateway.gatewayId ? facts?.gateways[notice.gateway.gatewayId]?.name ?? null : null, t).join(" ")}{" "}
              {notice.uncovered.length > 0 && notice.gateway.gatewayId && (
                <Link to={policiesUrl(notice.gateway.gatewayId)} className="v3-inline-link">{t("agentTools.openPolicies")}</Link>
              )}
            </span>
          </Notice>
        ))}
        {empty ? (
          <Empty title={t("agentTools.empty")} />
        ) : (
          <dl className="v3-kv">
            {model.gateway.length > 0 && (
              <Row label={t("agentTools.group.gateway")}>
                <div style={{ display: "grid", gap: 8 }}>
                  {model.gateway.map((gw) => {
                    const gatewayName = gw.gatewayId ? facts?.gateways[gw.gatewayId]?.name : null;
                    const status = gw.recordId ? facts?.records[gw.recordId] : undefined;
                    return (
                      <div key={gw.name} style={{ display: "grid", gap: 4 }}>
                        <div style={wrap}>
                          <span className="mono">{gw.name}</span>
                          {gatewayName && <span className="v3-hint">{t("agentTools.gatewayVia", { gateway: gatewayName })}</span>}
                          {status && <Chip s={status === "APPROVED" ? "ok" : "wait"}>{status}</Chip>}
                        </div>
                        <div style={wrap}>
                          {gw.tools
                            ? gw.tools.map((tool) => chip({ key: `gateway:${gw.name}:${tool}`, label: tool }))
                            : (
                              <>
                                {chip({ key: `gateway:${gw.name}:${gw.scope}`, label: gw.scope })}
                                <span className="v3-hint">{t("agentTools.gatewayAll")}</span>
                              </>
                            )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </Row>
            )}
            {model.mcp.length > 0 && (
              <Row label={t("agentTools.group.mcp")}>
                <div style={{ display: "grid", gap: 6 }}>
                  {model.mcp.map((server) => {
                    const registryAuth = facts?.mcpAuth[server.name];
                    return (
                      <div key={server.name} style={wrap}>
                        {chip({ key: `mcp:${server.name}`, label: server.name })}
                        {server.url && <code className="mono" style={{ fontSize: 12, wordBreak: "break-all" }}>{server.url}</code>}
                        <span className="v3-hint">
                          {server.auth
                            ? t("agentTools.mcpAuth", { kind: server.auth.kind, mode: server.auth.mode })
                            : registryAuth
                              ? t("agentTools.mcpAuthRegistry", { kind: registryAuth })
                              : t("agentTools.mcpAuthNone")}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </Row>
            )}
            {model.builtin.length > 0 && (
              <Row label={t("agentTools.group.builtin")}>
                <div style={wrap}>{model.builtin.map((name) => chip({ key: `builtin:${name}`, label: name }))}</div>
              </Row>
            )}
            {model.rest.length > 0 && (
              <Row label={t("agentTools.group.rest")}>
                <div style={wrap}>
                  {model.rest.map((api) => (
                    <span key={api.name} style={wrap}>
                      {chip({ key: `rest:${api.name}`, label: api.name })}
                      {api.host && <span className="v3-hint mono">{api.host}</span>}
                    </span>
                  ))}
                </div>
              </Row>
            )}
            {model.kbs.length > 0 && (
              <Row label={t("agentTools.group.kb")}>
                <div style={wrap}>
                  {model.kbs.map((kb) => (
                    <Link key={kb.kbId} to={`/v3/knowledge?id=${encodeURIComponent(kb.kbId)}`} className="v3-chip" data-s={added.has(`kb:${kb.kbId}`) ? "info" : undefined}>
                      {kb.name}
                    </Link>
                  ))}
                  {model.kbTools.length > 0 && <span className="v3-hint">{t("agentTools.kbTools", { tools: model.kbTools.join(", ") })}</span>}
                </div>
              </Row>
            )}
            {model.skills.length > 0 && (
              <Row label={t("agentTools.group.skill")}>
                <div style={{ display: "grid", gap: 4 }}>
                  {model.skills.map((skill) => (
                    <div key={skill.path} style={wrap}>
                      {chip({ key: `skill:${skill.path}`, label: skill.name }, skill.path)}
                      <span className="v3-hint mono" style={{ wordBreak: "break-all" }}>{skill.path}</span>
                    </div>
                  ))}
                </div>
              </Row>
            )}
            {model.toolkits.length > 0 && (
              <Row label={t("agentTools.group.toolkit")}>
                <div style={{ display: "grid", gap: 4 }}>
                  {model.toolkits.map((kit) => (
                    <div key={kit.name} style={wrap}>
                      {chip({ key: `toolkit:${kit.name}`, label: kit.name })}
                      <span className="v3-hint mono">{kit.tools.join(", ")}</span>
                    </div>
                  ))}
                </div>
              </Row>
            )}
            {model.native.length > 0 && (
              <Row label={t("agentTools.group.native")}>
                <div style={wrap}>
                  {model.native.map((name) => chip({ key: `native:${name}`, label: t(`agentTools.native.${name}`, { defaultValue: name }) }))}
                </div>
              </Row>
            )}
            {model.template.length > 0 && (
              <Row label={t("agentTools.group.template")}>
                <div style={wrap}>
                  {model.template.map((name) => chip({ key: `template:${name}`, label: name }))}
                  <span className="v3-hint">{t("agentTools.templateHint")}</span>
                </div>
              </Row>
            )}
            {(model.allowed || model.allowedDerived) && (
              <Row label={t("agentTools.group.allowed")}>
                {model.allowed ? (
                  <div style={wrap} data-testid="v3-agent-tools-allowed">
                    {model.allowed.map((entry) => (
                      <Chip key={entry.pattern} s={entry.source ? "info" : undefined}>
                        <span className="mono">{entry.pattern}</span>
                        {entry.source && <span style={{ marginLeft: 6 }}>· {t(`agentTools.allowedFrom.${entry.source}`)}</span>}
                      </Chip>
                    ))}
                    {model.allowed.length === 0 && <span className="v3-hint">{t("agentTools.allowedNone")}</span>}
                  </div>
                ) : (
                  <span className="v3-hint">{t("agentTools.allowedDerived")}</span>
                )}
              </Row>
            )}
            {model.codeDefined && (
              <Row label={t("agentTools.group.code")}>
                <span>
                  {t("agentTools.code")}{" "}
                  <Link to="/v3/observability" className="v3-inline-link">{t("agentTools.openObservability")}</Link>
                </span>
              </Row>
            )}
          </dl>
        )}
        <dl className="v3-kv">
          <Row label={t("agentTools.telemetry.label")}>
            <span data-testid="v3-agent-tools-telemetry" style={wrap}>
              {telemetry === "error" ? (
                <span className="v3-hint">{t("agentTools.telemetry.unknown")}</span>
              ) : !tel ? (
                <span className="v3-hint">…</span>
              ) : (
                <>
                  <Chip s={tel.tone}>{tel.line}</Chip>
                  {tel.hint && <span className="v3-hint">{tel.hint}</span>}
                  {tel.ok && <Link to="/v3/observability" className="v3-inline-link">{t("agentTools.openObservability")}</Link>}
                </>
              )}
            </span>
          </Row>
        </dl>
      </div>
    </Panel>
  );
}
