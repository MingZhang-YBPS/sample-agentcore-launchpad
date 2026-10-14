import { ArrowLeft, ArrowUpDown, MessagesSquare, Play, RefreshCw, Scale, Search, Settings2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { api, dlcApi, errorMessage, type AgentInfo, type StageInfo } from "../../lib/api";
import type { ReleaseState } from "../../lib/dlc";
import { useWorkspace } from "../../workspace/workspace-context";
import { agentSignal, gateSignal, releaseSignal } from "../signals";
import { Btn, Chip, Empty, Lamp, Notice, PageHead, Panel, type Signal, Skeleton, Stat } from "../ui";
import { useLoad, useToast } from "../hooks";
import { ago, ms } from "../format";

const STAGE_SIGNAL: Record<StageInfo["status"], Signal> = {
  succeeded: "ok",
  running: "wait",
  failed: "act",
  pending: "off",
  skipped: "off",
};

/**
 * The path a version takes, drawn as one line: built → on candidate → through the
 * gate → serving. Where the line stops is where the agent is; the colour of the
 * last lit node says whether that is fine.
 */
function Lifecycle({ agent, release }: { agent: AgentInfo; release: ReleaseState | null }) {
  const { t } = useTranslation();
  const state = release?.state;
  const pending = release?.pending ?? null;
  const verdict = (pending?.gate_report as { verdict?: "PASS" | "BLOCKED" | "INVALID" } | undefined)?.verdict;
  const gated = state?.endpoint_mode === "live";
  const nodes: { key: string; label: string; detail: string; s: Signal }[] = [
    {
      key: "build",
      label: t("v3.life.build"),
      detail: agent.status === "deploying" ? t("v3.life.building") : `v${agent.version ?? "—"}`,
      s: agent.status === "failed" ? "act" : agent.status === "deploying" ? "wait" : agent.status === "active" ? "ok" : "off",
    },
    {
      key: "candidate",
      label: t("v3.life.candidate"),
      detail: gated ? (state?.candidate_version ? `v${state.candidate_version}` : "—") : t("v3.life.skipped"),
      s: !gated ? "off" : pending ? "wait" : "ok",
    },
    {
      key: "gate",
      label: t("v3.life.gate"),
      detail: !gated ? t("v3.life.skipped") : verdict ?? (pending ? t("v3.life.notRun") : t("v3.life.clear")),
      s: !gated ? "off" : verdict ? gateSignal(verdict) : pending ? "wait" : "ok",
    },
    {
      key: "live",
      label: t("v3.life.live"),
      detail: gated ? `v${state?.live_version ?? "—"}` : `v${agent.version ?? "—"} · DEFAULT`,
      s: agent.status === "active" ? "ok" : "off",
    },
  ];
  return (
    <div style={{ display: "grid", gridTemplateColumns: `repeat(${nodes.length}, 1fr)`, gap: 0, position: "relative" }}>
      {nodes.map((n, i) => (
        <div key={n.key} style={{ position: "relative", padding: "6px 12px 4px 0" }}>
          {i < nodes.length - 1 && (
            <div
              aria-hidden="true"
              style={{
                position: "absolute", top: 13, left: 20, right: 0, height: 2,
                background: n.s === "ok" ? "linear-gradient(90deg, var(--v3-ok), var(--v3-line-2))" : "var(--v3-line)",
              }}
            />
          )}
          <div style={{ position: "relative", display: "flex", alignItems: "center", gap: 10 }}>
            <Lamp s={n.s} live={n.key === "live" && n.s === "ok"} />
          </div>
          <div className="v3-stat" style={{ marginTop: 12 }}>
            <div className="label">{n.label}</div>
            <div className="mono" style={{ marginTop: 4, color: n.s === "off" ? "var(--v3-text-3)" : "var(--v3-text)" }}>
              {n.detail}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

function TryIt({ agent }: { agent: AgentInfo }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [prompt, setPrompt] = useState("");
  const [answer, setAnswer] = useState<{ text: string; latency: number } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const run = () => {
    if (!prompt.trim() || busy || agent.status !== "active") return;
    setBusy(true);
    setError(null);
    setAnswer(null);
    api
      .invokeAgent(agent.id, prompt.trim())
      .then((res) => setAnswer({ text: res.text, latency: res.latency_ms }))
      .catch((err: unknown) => { setError(errorMessage(err)); toast("act", errorMessage(err)); })
      .finally(() => setBusy(false));
  };
  return (
    <Panel title={t("v3.agent.try")} end={answer ? <span className="mono">{ms(answer.latency)}</span> : undefined}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          run();
        }}
        style={{ display: "flex", gap: 8 }}
      >
        <input className="v3-input" value={prompt} onChange={(e) => setPrompt(e.target.value)}
          aria-label={t("v3.agent.tryPlaceholder")} placeholder={t("v3.agent.tryPlaceholder")} disabled={busy || agent.status !== "active"} />
        <Btn kind="primary" type="submit" disabled={busy || agent.status !== "active" || !prompt.trim()}>
          <Play size={14} /> {t(busy ? "v3.ux.running" : "v3.agent.run")}
        </Btn>
      </form>
      {agent.status !== "active" && <p className="v3-hint">{t("v3.ux.invokeUnavailable")}</p>}
      {error && <Notice s="act">{error}</Notice>}
      {answer && (
        <pre className="mono" aria-live="polite" style={{ marginTop: 14, whiteSpace: "pre-wrap", color: "var(--v3-text)", background: "var(--v3-ink-0)",
          border: "1px solid var(--v3-line)", borderRadius: 8, padding: 12, maxHeight: 260, overflow: "auto" }}>
          {answer.text}
        </pre>
      )}
    </Panel>
  );
}

const TARGET_SIGNAL = (status: string | null): Signal =>
  status === "READY" ? "ok" : status === "FAILED" || status === "MISSING" || status?.endsWith("UNSUCCESSFUL") ? "act" : status ? "wait" : "off";

/**
 * An MCP server is a tool, not an agent: no chat, no gate. What matters is its
 * Gateway target and the tools agents see through it (`<target>___<tool>`).
 */
function McpServerPanel({ agent }: { agent: AgentInfo }) {
  const { t } = useTranslation();
  const status = useLoad(() => api.agentMcp(agent.id), `v3-agent-mcp:${agent.id}:${agent.status}:${agent.version}`);
  const d = status.data;
  return (
    <Panel
      title={t("v3.mcp.title")}
      signal={TARGET_SIGNAL(d?.target_status ?? null)}
      end={<button type="button" className="v3-btn ghost sm" onClick={status.reload} disabled={status.loading}><RefreshCw size={13} /> {t("v3.ux.refresh")}</button>}
    >
      {status.error ? (
        <Notice s="act">{status.error}</Notice>
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
          <Notice s="info">
            {t("v3.mcp.howToUse", { target: d.target_name })}{" "}
            <Link to="/v3/governance" style={{ textDecoration: "underline" }}>{t("v3.mcp.openGovernance")}</Link>
          </Notice>
        </div>
      )}
    </Panel>
  );
}

function AgentDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const listParams = new URLSearchParams(params);
  listParams.delete("id");
  const listTo = `/v3/agents${listParams.size ? `?${listParams}` : ""}`;
  const agent = useLoad(() => api.getAgent(id), `v3-agent:${id}`);
  const release = useLoad(() => dlcApi.release(id), `v3-agent-rel:${id}`);
  const versions = useLoad(() => api.agentVersions(id), `v3-agent-ver:${id}`);
  const a = agent.data;
  // a deploy in flight moves on its own: follow it until it lands or fails
  const deploying = a?.status === "deploying";
  const reloadAgent = agent.reload;
  const reloadVersions = versions.reload;
  const reloadRelease = release.reload;
  useEffect(() => {
    if (!deploying) return;
    const timer = window.setInterval(reloadAgent, 4000);
    return () => {
      window.clearInterval(timer);
      reloadVersions();
      reloadRelease();
    };
  }, [deploying, reloadAgent, reloadVersions, reloadRelease]);

  if (agent.loading && !a) return <Skeleton rows={6} />;
  if (!a) return <Notice s="act">{agent.error ?? t("v3.agent.notFound")}
    <button type="button" className="v3-btn sm" onClick={agent.reload}>{t("v3.ux.retry")}</button>
    <Link to={listTo} className="v3-inline-link">{t("v3.agents.title")}</Link>
  </Notice>;

  const name = a.display_name || a.name;
  const deploy = a.deployments?.[0] ?? a.deployment;
  const spec = a.spec as { model_id?: string; model?: string; system_prompt?: string; tools?: unknown[]; protocol?: string };
  const pending = release.data?.pending ?? null;
  const mcpServer = spec.protocol === "mcp";

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <div>
        <button type="button" className="v3-btn ghost sm" onClick={() => navigate(listTo)}>
          <ArrowLeft size={14} /> {t("v3.agents.title")}
        </button>
      </div>
      <PageHead
        eyebrow={`${a.method} · ${a.id.slice(0, 8)}`}
        title={
          <span style={{ display: "inline-flex", alignItems: "center", gap: 14 }}>
            <Lamp s={agentSignal(a)} live={a.status === "active"} />
            {name}
          </span>
        }
        sub={a.status === "failed" ? a.error ?? t("v3.agent.failed") : spec.system_prompt?.slice(0, 180)}
        end={
          <>
            <button type="button" className="v3-btn" disabled={agent.loading || release.loading || versions.loading}
              onClick={() => { agent.reload(); release.reload(); versions.reload(); }}><RefreshCw size={14} /> {t("v3.ux.refresh")}</button>
            {!mcpServer && <Link to={`/v3/chat?agent=${a.id}`} className="v3-btn"><MessagesSquare size={14} /> {t("v3.agent.chat")}</Link>}
            {!mcpServer && <Link to={`/v3/gate?agent=${a.id}`} className="v3-btn"><Scale size={14} /> {t("v3.agent.gate")}</Link>}
            <Link to={`/v2/agents?view=detail&id=${a.id}`} className="v3-btn ghost">
              <Settings2 size={14} /> {t("v3.agent.edit")}
            </Link>
          </>
        }
      />
      {agent.error && <Notice s="act">{agent.error} <button type="button" className="v3-btn sm" onClick={agent.reload}>{t("v3.ux.retry")}</button></Notice>}
      <div className="v3-agent-status"><Chip s={agentSignal(a)}>{t(`v3.agents.filter.${agentSignal(a)}`)}</Chip>
        <span className="v3-hint">{t("v3.ux.runtimeStatus")}</span>
      </div>

      {mcpServer ? <McpServerPanel agent={a} /> : <Panel title={t("v3.agent.lifecycle")} signal={agentSignal(a)}>
        {release.error ? <Notice s="wait">{t("v3.ux.releaseFailed")} {release.error}
          <button type="button" className="v3-btn sm" onClick={release.reload}>{t("v3.ux.retry")}</button>
        </Notice> : !release.data ? <Skeleton rows={2} /> : <Lifecycle agent={a} release={release.data} />}
        {pending && (
          <div style={{ marginTop: 14 }}>
            <Notice s={releaseSignal(pending.decision)}>
              {t("v3.agent.pending", { version: pending.candidate_version ?? "—" })}{" "}
              <Link to={`/v3/gate?agent=${a.id}`} style={{ textDecoration: "underline" }}>{t("v3.agent.openGate")}</Link>
            </Notice>
          </div>
        )}
      </Panel>}

      <div className="v3-grid c4">
        <Panel><Stat label={t("v3.agents.version")} value={`v${a.version ?? "—"}`} /></Panel>
        {mcpServer ? (
          <Panel><Stat label={t("v3.mcp.kind")} value={<span style={{ fontSize: 15 }}>{t("v3.mcp.kindValue")}</span>} /></Panel>
        ) : (
          <Panel><Stat label={t("v3.agent.model")} value={<span className="mono" style={{ fontSize: 13, lineHeight: 1.45, wordBreak: "break-all", display: "block" }}>{spec.model_id ?? spec.model ?? "—"}</span>} /></Panel>
        )}
        {mcpServer ? (
          <Panel><Stat label={t("v3.mcp.target")} value={<span className="mono" style={{ fontSize: 15 }}>{(a.spec as { mcp?: { target_name?: string } }).mcp?.target_name ?? "—"}</span>} /></Panel>
        ) : (
          <Panel><Stat label={t("v3.agent.tools")} value={Array.isArray(spec.tools) ? spec.tools.length : 0} /></Panel>
        )}
        <Panel><Stat label={t("v3.agents.updated")} value={ago(a.updated_at)} /></Panel>
      </div>

      <div className="v3-grid v3-split">
        <Panel title={t("v3.agent.deploy")} flush>
          {!deploy ? (
            <Empty title={t("v3.agent.noDeploy")} />
          ) : (
            <table className="v3-table v3-deploy-stages">
              <tbody>
                {deploy.stages.map((st) => (
                  <tr key={st.name}>
                    <td style={{ width: 30 }}><Lamp s={STAGE_SIGNAL[st.status]} live={st.status === "running"} /></td>
                    <td className="mono" style={{ width: 120 }}>{st.name}</td>
                    <td><Chip s={STAGE_SIGNAL[st.status]}>{t(`v3.ux.stage.${st.status}`)}</Chip></td>
                    <td style={{ color: "var(--v3-text-2)" }}>{st.detail || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
        <Panel title={t("v3.agent.versions")} flush>
          {versions.error ? <div style={{ padding: 20 }}><Notice s="act">{versions.error}
            <button type="button" className="v3-btn sm" onClick={versions.reload}>{t("v3.ux.retry")}</button></Notice></div> : !versions.data ? (
              <div style={{ padding: 20 }}>{versions.loading ? <Skeleton rows={3} /> :
                <Empty title={t("v3.agent.noVersions")} />}</div>
          ) : (
            <table className="v3-table">
              <thead><tr><th>{t("v3.agents.version")}</th><th>{t("v3.agent.endpoints")}</th><th className="num">{t("v3.agents.updated")}</th></tr></thead>
              <tbody>
                {versions.data.versions.slice(0, 8).map((v) => {
                  const serving = versions.data!.endpoints.filter((e) => e.live_version === v.version).map((e) => e.name);
                  return (
                    <tr key={v.version ?? "?"}>
                      <td className="mono">v{v.version}</td>
                      <td style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                        {serving.length ? serving.map((n) => <Chip key={n} s={n === "live" || n === "DEFAULT" ? "ok" : "info"}>{n}</Chip>) : <span style={{ color: "var(--v3-text-3)" }}>—</span>}
                      </td>
                      <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(v.last_updated_at)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Panel>
      </div>

      {!mcpServer && <TryIt agent={a} />}
    </div>
  );
}

function AgentList() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const { current } = useWorkspace();
  const agents = useLoad(() => api.listAgents(), `v3-agents:${current?.id ?? ""}`);
  const q = params.get("q") ?? "";
  const status = params.get("status");
  const filter = (["ok", "wait", "act", "off"] as const).find((s) => s === status) ?? "all";
  const sort = params.get("sort") === "name" ? "name" : "updated";
  const update = (key: string, value: string) => setParams((prev) => {
    const next = new URLSearchParams(prev);
    if (value) next.set(key, value); else next.delete(key);
    return next;
  }, { replace: true });
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (agents.data?.agents ?? [])
      .filter((a) => a.status !== "deleted")
      .filter((a) => filter === "all" || agentSignal(a) === filter)
      .filter((a) => !needle || `${a.name} ${a.display_name ?? ""} ${a.method}`.toLowerCase().includes(needle))
      .sort((a, b) => sort === "name" ? (a.display_name || a.name).localeCompare(b.display_name || b.name) : (b.updated_at ?? "").localeCompare(a.updated_at ?? ""));
  }, [agents.data, q, filter, sort]);
  const all = (agents.data?.agents ?? []).filter((a) => a.status !== "deleted");
  const count = (s: Signal) => all.filter((a) => agentSignal(a) === s).length;

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.agents.eyebrow")}
        title={t("v3.agents.title")}
        sub={t("v3.agents.sub")}
        end={<><button type="button" className="v3-btn" onClick={agents.reload} disabled={agents.loading}><RefreshCw size={14} /> {t("v3.ux.refresh")}</button>
          <Link to="/v3/create" className="v3-btn primary">{t("v3.nav.create")}</Link></>}
      />
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        {(["all", "ok", "wait", "act", "off"] as const).map((f) => (
          <button key={f} type="button" aria-pressed={filter === f} className={`v3-btn sm${filter === f ? "" : " ghost"}`} onClick={() => update("status", f === "all" ? "" : f)}>
            {f !== "all" && <Lamp s={f} />}
            {t(`v3.agents.filter.${f}`)}
            <span className="mono" style={{ color: "var(--v3-text-3)" }}>{agents.data ? (f === "all" ? all.length : count(f)) : "—"}</span>
          </button>
        ))}
        <div className="v3-agent-search">
          <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
          <input className="v3-input" style={{ paddingLeft: 34 }} value={q} onChange={(e) => update("q", e.target.value)}
            placeholder={t("v3.agents.search")} aria-label={t("v3.agents.search")} />
        </div>
      </div>
      <div className="v3-list-summary">
        <span aria-live="polite">{agents.data ? t("v3.ux.results", { count: rows.length, total: all.length }) : t("v3.home.loading")}</span>
        <label><ArrowUpDown size={14} /> <span>{t("v3.ux.sort")}</span>
          <select className="v3-input" value={sort} onChange={(e) => update("sort", e.target.value === "updated" ? "" : e.target.value)}>
            <option value="updated">{t("v3.ux.updatedFirst")}</option><option value="name">{t("v3.ux.nameOrder")}</option>
          </select>
        </label>
      </div>
      <Panel flush className="v3-agent-list">
        {agents.loading && !agents.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={6} /></div>
        ) : agents.error ? (
          <div style={{ padding: 20 }}><Notice s="act">{agents.error} <button type="button" className="v3-btn sm" onClick={agents.reload}>{t("v3.ux.retry")}</button></Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={t(all.length === 0 ? "v3.home.noAgents" : "v3.agents.none")}>
            {all.length === 0 ? <Link to="/v3/create" className="v3-btn primary">{t("v3.nav.create")}</Link> :
              <button type="button" className="v3-btn" onClick={() => setParams({})}>{t("v3.ux.clearFilters")}</button>}
          </Empty>
        ) : (
          <div className="v3-table-scroll" role="region" aria-label={t("v3.ux.tableScroll")} tabIndex={0}>
          <table className="v3-table">
            <thead>
              <tr>
                <th>{t("v3.ux.status")}</th>
                <th>{t("v3.agents.name")}</th>
                <th>{t("v3.agents.method")}</th>
                <th className="num">{t("v3.agents.version")}</th>
                <th>{t("v3.agents.owner")}</th>
                <th className="num">{t("v3.agents.updated")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((a) => (
                <tr key={a.id}>
                  <td><Chip s={agentSignal(a)}><Lamp s={agentSignal(a)} live={a.status === "active"} /> {t(`v3.agents.filter.${agentSignal(a)}`)}</Chip></td>
                  <td>
                    <div className="v3-name">
                      <div>
                        <Link className="v3-inline-link" to={`/v3/agents?${new URLSearchParams({ ...Object.fromEntries(params), id: a.id })}`}><b>{a.display_name || a.name}</b></Link>
                        <small>{a.status === "failed" ? (a.error ?? "").slice(0, 80) : a.name}</small>
                      </div>
                    </div>
                  </td>
                  <td><Chip>{a.method}</Chip></td>
                  <td className="num">v{a.version ?? "—"}</td>
                  <td className="mono" style={{ color: "var(--v3-text-2)" }}>{a.created_by_name || a.owner || "—"}</td>
                  <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(a.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>
        )}
      </Panel>
    </div>
  );
}

export function V3Agents() {
  const [params] = useSearchParams();
  const id = params.get("id");
  return id ? <AgentDetail key={id} id={id} /> : <AgentList />;
}
