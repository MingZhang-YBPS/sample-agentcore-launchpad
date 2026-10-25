import { ArrowRight, ChevronDown, Plus, X } from "lucide-react";
import { type ReactNode, useEffect, useId, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { type AgentForm, strandsContext } from "../../../lib/agent-spec";
import { api, errorMessage } from "../../../lib/api";
import { customToolsApi } from "../../../lib/api/custom-tools";
import {
  SANDBOX_TOOLS,
  type SandboxTool,
  agentToolBlock,
  agentToolRow,
  backoffDelays,
  bedrockOnlyApplies,
  MAX_AGENT_TOOLS,
  type ProviderChoice,
  PROVIDERS,
  providerApplies,
  RETRY_DEFAULTS,
  SB2_KEYS_SHOWN_ON_CONTINUE,
  type Sb2IssueKey,
  sb2Issues,
  splitList,
  type ConversationChoice,
  emptyStrandsForm,
  EXAMPLE_OUTPUT_SCHEMA,
  isClaudeModel,
  outputSchemaProblem,
  promptPlaceholders,
  schemaFields,
  splitHosts,
  STRANDS_TOOLS,
  type StrandsForm,
  type StrandsIssueKey,
  strandsIssues,
  type StrandsTool,
} from "../../../lib/strands-builder";
import { useLoad } from "../../hooks";
import { Button, Card, Field, Segmented, Select } from "../../ui";
import { CustomToolsPanel } from "./CustomToolEditor";
import { ISSUE_KEYS_SHOWN_ON_CONTINUE, sb, type StrandsGroupKey } from "./strandsGroups";
import type { SectionProps } from "./wizardKit";
import "./strands-sections.css";
import "./strands-sections-sb2.css";

/**
 * The Strands (zip_runtime) configure step as five collapsible groups — Basics /
 * Tools / Model behaviour / Conversation & output / Advanced (strands-builder §2,
 * "共同要求") — plus the SB-1 cards that fill them. Every group and option carries
 * one "when to use" line; the closed head shows a mono readout of what is set, so
 * a collapsed group still says what it holds.
 *
 * SB-2 rides inside the same cards so every group keeps its place: the Tools group
 * gains "Other agents as tools" and "Tool-call guards" (under the curated tools),
 * Model behaviour gains the model provider (head of the parameters card) and
 * throttling retry (its foot).
 *
 * SB-3 adds two panels to the Tools group, after the guards: the sandboxed
 * high-risk tools (an enclosure naming the Code Interpreter they run in, closed
 * until the workspace setting opens them) and the member's own Python tools
 * (`CustomToolEditor`).
 */

export function StrandsGroup({
  group,
  index,
  open,
  onToggle,
  readout,
  flagged,
  children,
}: {
  group: StrandsGroupKey;
  index: number;
  open: boolean;
  onToggle: () => void;
  readout?: ReactNode;
  /** holds a problem the member must fix (shown once they tried to continue) */
  flagged?: boolean;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  const bodyId = useId();
  const body = useRef<HTMLDivElement>(null);
  // a closed group stays mounted (its fields keep local state, e.g. staged KB
  // files) but leaves the tab order and the accessibility tree
  useEffect(() => {
    body.current?.toggleAttribute("inert", !open);
  }, [open]);
  return (
    <section
      className={["sb-group", open ? "open" : "", flagged ? "flagged" : ""].filter(Boolean).join(" ")}
      data-testid={`sb-group-${group}`}
    >
      <button type="button" className="sb-group-head" aria-expanded={open} aria-controls={bodyId} onClick={onToggle}>
        <span className="sb-idx" aria-hidden="true">
          {String(index + 1).padStart(2, "0")}
        </span>
        <span className="sb-head-text">
          <span className="sb-title">{t(`strandsBuilder.group.${group}`)}</span>
          <span className="sb-when">{t(`strandsBuilder.group.${group}When`)}</span>
        </span>
        {flagged && <span className="sb-flag">{t("strandsBuilder.needsAttention")}</span>}
        {readout && <span className="sb-readout">{readout}</span>}
        <ChevronDown size={16} className="sb-chev" aria-hidden="true" />
      </button>
      <div className="sb-body" id={bodyId} ref={body} role="region" aria-label={t(`strandsBuilder.group.${group}`)}>
        <div className="sb-body-inner">
          <div className="sb-stack">{children}</div>
        </div>
      </div>
    </section>
  );
}

/* ── shared bits ───────────────────────────────────────────────────────── */

function useStrands(set: SectionProps["set"]) {
  return (patch: Partial<StrandsForm> | ((prev: StrandsForm) => Partial<StrandsForm>)) =>
    set((prev) => {
      const cur = prev.strands ?? emptyStrandsForm();
      return { strands: { ...cur, ...(typeof patch === "function" ? patch(cur) : patch) } };
    });
}

/** The message for one issue, or undefined. Empty-input issues (a required value
 *  not typed yet) wait for the first continue; malformed input shows at once. */
function useIssue(form: AgentForm, touched: boolean) {
  const { t } = useTranslation();
  const { issues } = strandsIssues(form.strands, strandsContext(form));
  return (key: StrandsIssueKey): string | undefined => {
    const detail = issues[key];
    if (detail === undefined) return undefined;
    if (!detail && !touched && ISSUE_KEYS_SHOWN_ON_CONTINUE.includes(key)) return undefined;
    return t(`strandsBuilder.err.${key}`, { detail });
  };
}

/** The SB-2 counterpart of `useIssue` (own keys, same touched rule). */
function useSb2Issue(form: AgentForm, touched: boolean) {
  const { t } = useTranslation();
  const issues = sb2Issues(form.strands, strandsContext(form));
  return (key: Sb2IssueKey): string | undefined => {
    const detail = issues[key];
    if (detail === undefined) return undefined;
    if (!detail && !touched && SB2_KEYS_SHOWN_ON_CONTINUE.includes(key)) return undefined;
    return t(`strandsBuilder.err.${key}`, { detail });
  };
}

/** A drafted form saved before SB-2 has no SB-2 slice: fill the defaults in. */
const sb2Form = (form: AgentForm): StrandsForm => ({ ...emptyStrandsForm(), ...sb(form) });

/* ── Tools: the curated strands-agents-tools ─────────────────────────────── */

/** Mounted inside the Strands tools card (MethodSections). */
export function StrandsToolPicker({ form, set, touched }: Pick<SectionProps, "form" | "set"> & { touched: boolean }) {
  const { t } = useTranslation();
  const s = sb(form);
  const patch = useStrands(set);
  const issue = useIssue(form, touched);
  const hosts = splitHosts(s.httpHosts);
  const toggle = (name: StrandsTool) =>
    patch((prev) => ({
      tools: prev.tools.includes(name) ? prev.tools.filter((x) => x !== name) : STRANDS_TOOLS.filter((x) => x === name || prev.tools.includes(x)),
    }));
  const disabledWhy = (name: StrandsTool) =>
    name === "retrieve" && !form.selectedKbs.length && !s.tools.includes(name)
      ? t(form.protocol === "a2a" ? "strandsBuilder.tools.retrieveA2a" : "strandsBuilder.tools.retrieveNoKb")
      : null;
  return (
    <>
    <Field
      label={t("strandsBuilder.tools.label")}
      hint={s.tools.length ? t("strandsBuilder.tools.replaceNote") : t("strandsBuilder.tools.hint")}
      error={issue("retrieve")}
    >
      <div className="sb-tools" data-testid="sb-strands-tools">
        {STRANDS_TOOLS.map((name) => {
          const on = s.tools.includes(name);
          const why = disabledWhy(name);
          return (
            <label key={name} className={["sb-tool", on ? "on" : "", why ? "off" : ""].filter(Boolean).join(" ")}>
              <input type="checkbox" checked={on} disabled={Boolean(why)} onChange={() => toggle(name)} data-testid={`sb-tool-${name}`} />
              <span className="sb-lamp" aria-hidden="true" />
              <span className="sb-tool-name">{name}</span>
              <span className="sb-tool-when">{why ?? t(`strandsBuilder.tools.when.${name}`)}</span>
            </label>
          );
        })}
      </div>
      <p className="sb-fine">{t("strandsBuilder.tools.sandboxNote")}</p>
      {s.tools.includes("http_request") && (
        <div className="sb-reveal" data-testid="sb-http-hosts">
          <Field label={t("strandsBuilder.tools.hostsLabel")} required error={issue("httpHosts")} hint={t("strandsBuilder.tools.hostsHint")}>
            <input
              className="v2-input mono"
              value={s.httpHosts}
              placeholder="api.example.com, *.svc.example.org"
              onChange={(e) => patch({ httpHosts: e.target.value })}
              aria-label={t("strandsBuilder.tools.hostsLabel")}
            />
          </Field>
          <div className="sb-chips" aria-live="polite">
            {hosts.length ? (
              hosts.map((h) => (
                <span key={h} className="sb-chip mono">
                  {h}
                </span>
              ))
            ) : (
              <span className="sb-chip refused">{t("strandsBuilder.tools.hostsNone")}</span>
            )}
          </div>
        </div>
      )}
    </Field>
    <AgentToolsPanel form={form} set={set} touched={touched} />
    <ToolGuardsPanel form={form} set={set} touched={touched} />
    <SandboxToolsPanel form={form} set={set} />
    <CustomToolsPanel form={form} set={set} touched={touched} />
    </>
  );
}

/* ── Tools: the sandboxed high-risk tools (SB-3) ─────────────────────────── */

/** python_repl / shell / file_write / use_aws: selectable where the workspace's
 *  sandbox setting allows them; each call is a Code Interpreter call. An
 *  administrator opens them right here. A tool the member already picked stays
 *  visible (and removable) after the setting closed it — the deploy refuses it. */
function SandboxToolsPanel({ form, set }: Pick<SectionProps, "form" | "set">) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const s = sb2Form(form);
  const patch = useStrands(set);
  const load = useLoad(() => customToolsApi.sandbox(), "sb3-sandbox");
  const allowed = new Set<SandboxTool>(load.data?.allowed_tools ?? []);
  const interpreter = load.data?.effective_code_interpreter_id ?? "aws.codeinterpreter.v1";
  const open = allowed.size > 0;
  const toggle = (name: SandboxTool) =>
    patch((prev) => {
      const cur = prev.sandboxTools ?? [];
      return { sandboxTools: cur.includes(name) ? cur.filter((x) => x !== name) : SANDBOX_TOOLS.filter((x) => x === name || cur.includes(x)) };
    });
  const closedPicks = s.sandboxTools.filter((x) => load.data && !allowed.has(x));
  return (
    <Field
      label={t("strandsBuilder.sandbox.label")}
      hint={t("strandsBuilder.sandbox.hint")}
      error={closedPicks.length ? t("strandsBuilder.sandbox.closedPick", { tools: closedPicks.join(", ") }) : undefined}
    >
      <div className={open ? "ct-sandbox" : "ct-sandbox closed"} data-testid="sb-sandbox-tools">
        <div className="ct-rail">
          <span className="ct-rail-name">{t("strandsBuilder.sandbox.rail")}</span>
          <span className="mono">{interpreter}</span>
          <span>{load.loading ? t("strandsBuilder.sandbox.loading") : open ? t("strandsBuilder.sandbox.open") : t("strandsBuilder.sandbox.closed")}</span>
        </div>
        <div className="sb-tools ct-sandbox-tools">
          {SANDBOX_TOOLS.map((name) => {
            const on = s.sandboxTools.includes(name);
            const off = !on && !allowed.has(name);
            return (
              <label key={name} className={["sb-tool", on ? "on" : "", off ? "off" : ""].filter(Boolean).join(" ")}>
                <input type="checkbox" checked={on} disabled={off} onChange={() => toggle(name)} data-testid={`sb-sandbox-${name}`} />
                <span className="sb-lamp" aria-hidden="true" />
                <span className="sb-tool-name">{name}</span>
                <span className="sb-tool-when">{off ? t("strandsBuilder.sandbox.toolClosed") : t(`strandsBuilder.sandbox.when.${name}`)}</span>
              </label>
            );
          })}
        </div>
        {isAdmin && load.data && <SandboxSettingEditor allowed={load.data.allowed_tools} interpreterId={load.data.code_interpreter_id} onSaved={load.reload} />}
      </div>
      {load.error && (
        <p className="sb-err">
          {load.error}{" "}
          <Button size="sm" onClick={load.reload}>
            {t("strandsBuilder.agents.retry")}
          </Button>
        </p>
      )}
    </Field>
  );
}

function SandboxSettingEditor({
  allowed,
  interpreterId,
  onSaved,
}: {
  allowed: SandboxTool[];
  interpreterId: string | null;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState<SandboxTool[]>(allowed);
  const [ci, setCi] = useState(interpreterId ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const save = () => {
    setSaving(true);
    setError(null);
    customToolsApi
      .setSandbox(draft, ci.trim() || null)
      .then(onSaved)
      .catch((err: unknown) => setError(errorMessage(err)))
      .finally(() => setSaving(false));
  };
  return (
    <details className="ct-admin">
      <summary>{t("strandsBuilder.sandbox.adminSummary")}</summary>
      <div className="ct-admin-body">
        <div className="ct-admin-row">
          {SANDBOX_TOOLS.map((name) => (
            <label key={name} className="sb-chip mono">
              <input
                type="checkbox"
                checked={draft.includes(name)}
                onChange={() => setDraft((d) => (d.includes(name) ? d.filter((x) => x !== name) : SANDBOX_TOOLS.filter((x) => x === name || d.includes(x))))}
              />
              {name}
            </label>
          ))}
        </div>
        <Field label={t("strandsBuilder.sandbox.interpreter")} hint={t("strandsBuilder.sandbox.interpreterHint")}>
          <input className="v2-input mono" value={ci} placeholder="aws.codeinterpreter.v1" onChange={(e) => setCi(e.target.value)} aria-label={t("strandsBuilder.sandbox.interpreter")} />
        </Field>
        <div className="ct-admin-row">
          <Button size="sm" kind="primary" onClick={save} disabled={saving}>
            {t("strandsBuilder.sandbox.save")}
          </Button>
          <span className="sb-fine">{t("strandsBuilder.sandbox.saveNote")}</span>
        </div>
        {error && <p className="sb-err">{error}</p>}
      </div>
    </details>
  );
}

/* ── Tools: other agents as tools (SB-2) ─────────────────────────────────── */

/** Pick deployed agents of this project to call as tools; each becomes a relay
 *  row — the agent, the tool name the model sees, and when to call it. */
function AgentToolsPanel({ form, set, touched }: Pick<SectionProps, "form" | "set"> & { touched: boolean }) {
  const { t } = useTranslation();
  const s = sb2Form(form);
  const patch = useStrands(set);
  const issue = useSb2Issue(form, touched);
  const load = useLoad(() => api.listAgents(), "sb2-agent-tools");
  const agents = useMemo(() => load.data?.agents ?? [], [load.data]);
  const picked = new Set(s.agentTools.map((r) => r.agentId));
  const verdicts = agents.filter((a) => !picked.has(a.id)).map((a) => ({ agent: a, block: agentToolBlock(a, form.name.trim()) }));
  const open = verdicts.filter((v) => v.block === null);
  const blocked = verdicts.filter((v) => v.block !== null && v.block !== "self");
  const full = s.agentTools.length >= MAX_AGENT_TOOLS;
  const setRow = (i: number, change: Partial<StrandsForm["agentTools"][number]>) =>
    patch((prev) => ({ agentTools: (prev.agentTools ?? []).map((r, j) => (j === i ? { ...r, ...change } : r)) }));
  const add = (id: string) => {
    const agent = agents.find((a) => a.id === id);
    if (agent) patch((prev) => ({ agentTools: [...(prev.agentTools ?? []), agentToolRow(agent)] }));
  };
  const rowIssue = issue("agentToolName") ?? issue("agentToolDuplicate") ?? issue("agentToolDescription") ?? issue("agentToolCount");
  return (
    <Field label={t("strandsBuilder.agents.label")} hint={t("strandsBuilder.agents.hint")} error={rowIssue}>
      <div className="sb2-agents" data-testid="sb-agent-tools">
        {s.agentTools.length > 0 && (
          <ol className="sb2-relays">
            {s.agentTools.map((row, i) => (
              <li key={row.agentId} className="sb2-relay" data-testid={`sb-agent-tool-${row.agentId}`}>
                <div className="sb2-relay-head">
                  <span className="sb-lamp on" aria-hidden="true" />
                  <span className="sb2-relay-agent">{agents.find((a) => a.id === row.agentId)?.display_name || agents.find((a) => a.id === row.agentId)?.name || row.label}</span>
                  <span className="sb-chip mono">{row.protocol === "a2a" ? "A2A" : "HTTP"}</span>
                  {row.sharedFrom && <span className="sb-chip">{t("strandsBuilder.agents.sharedFrom", { project: row.sharedFrom })}</span>}
                  <ArrowRight size={14} className="sb2-relay-arrow" aria-hidden="true" />
                  <input
                    className="v2-input mono sb2-relay-name"
                    value={row.toolName}
                    aria-label={t("strandsBuilder.agents.toolName")}
                    onChange={(e) => setRow(i, { toolName: e.target.value })}
                    data-testid={`sb-agent-tool-name-${row.agentId}`}
                  />
                  <button
                    type="button"
                    className="sb-x"
                    aria-label={t("strandsBuilder.remove")}
                    onClick={() => patch((prev) => ({ agentTools: (prev.agentTools ?? []).filter((_, j) => j !== i) }))}
                  >
                    <X size={14} aria-hidden="true" />
                  </button>
                </div>
                <textarea
                  className="v2-textarea"
                  rows={2}
                  value={row.description}
                  placeholder={t("strandsBuilder.agents.descriptionPlaceholder")}
                  aria-label={t("strandsBuilder.agents.description")}
                  onChange={(e) => setRow(i, { description: e.target.value })}
                />
              </li>
            ))}
          </ol>
        )}
        <div className="sb2-agents-pick">
          <Select
            value=""
            placeholder={
              load.loading
                ? t("strandsBuilder.agents.loading")
                : open.length
                  ? t("strandsBuilder.agents.add")
                  : t("strandsBuilder.agents.none")
            }
            options={open.map(({ agent }) => ({ value: agent.id, label: agent.display_name || agent.name }))}
            onChange={(id) => id && add(id)}
            disabled={full || load.loading || !open.length}
            ariaLabel={t("strandsBuilder.agents.add")}
            testId="sb-agent-tool-add"
          />
          {load.error && (
            <Button size="sm" onClick={load.reload}>
              {t("strandsBuilder.agents.retry")}
            </Button>
          )}
        </div>
        {blocked.length > 0 && (
          <details className="sb2-blocked">
            <summary>{t("strandsBuilder.agents.blocked", { count: blocked.length })}</summary>
            <ul>
              {blocked.map(({ agent, block }) => (
                <li key={agent.id}>
                  <span className="mono">{agent.display_name || agent.name}</span>
                  <span>{t(`strandsBuilder.agents.block.${block}`)}</span>
                </li>
              ))}
            </ul>
          </details>
        )}
        <p className="sb-fine">{t("strandsBuilder.agents.roleNote")}</p>
      </div>
    </Field>
  );
}

/* ── Tools: tool-call guards (SB-2) ──────────────────────────────────────── */

function ToolGuardsPanel({ form, set, touched }: Pick<SectionProps, "form" | "set"> & { touched: boolean }) {
  const { t } = useTranslation();
  const s = sb2Form(form);
  const patch = useStrands(set);
  const issue = useSb2Issue(form, touched);
  const approval = splitList(s.approvalTools);
  const masked = ["password", "token", ...splitList(s.maskArgs).map((a) => a.toLowerCase())];
  const sampleTool = approval.find((a) => !a.includes("*")) ?? s.agentTools[0]?.toolName ?? "send_email";
  const sampleArgs = JSON.stringify({ to: "ops@example.com", [masked[masked.length - 1]]: "***" });
  return (
    <Field label={t("strandsBuilder.guards.label")} hint={t("strandsBuilder.guards.hint")}>
      <div className="sb2-guards" data-testid="sb-tool-guards">
        <div className="sb2-guard">
          <span className="sb2-guard-k">{t("strandsBuilder.guards.approval")}</span>
          <input
            className="v2-input mono"
            value={s.approvalTools}
            placeholder="send_email, crm___*"
            aria-label={t("strandsBuilder.guards.approval")}
            onChange={(e) => patch({ approvalTools: e.target.value })}
            data-testid="sb-approval-tools"
          />
          <span className="sb2-guard-when">{t("strandsBuilder.guards.approvalWhen")}</span>
          {issue("approvalTools") && <span className="sb-err">{issue("approvalTools")}</span>}
          {approval.length > 0 && (
            <div className="sb-chips">
              {approval.map((name) => (
                <span key={name} className="sb-chip mono sb2-pause">
                  {name}
                </span>
              ))}
            </div>
          )}
        </div>
        <div className="sb2-guard">
          <span className="sb2-guard-k">{t("strandsBuilder.guards.cap")}</span>
          <input
            className="v2-input mono sb-narrow"
            inputMode="numeric"
            value={s.maxToolCalls}
            placeholder={t("strandsBuilder.guards.capNone")}
            aria-label={t("strandsBuilder.guards.cap")}
            onChange={(e) => patch({ maxToolCalls: e.target.value })}
            data-testid="sb-max-tool-calls"
          />
          <span className="sb2-guard-when">{t("strandsBuilder.guards.capWhen")}</span>
          {issue("maxToolCalls") && <span className="sb-err">{issue("maxToolCalls")}</span>}
        </div>
        <label className="sb-toggle sb2-guard-wide">
          <input
            type="checkbox"
            checked={s.logToolCalls}
            onChange={(e) => patch({ logToolCalls: e.target.checked })}
            data-testid="sb-log-tool-calls"
          />
          <span className="sb-toggle-text">
            <span className="sb-toggle-name">{t("strandsBuilder.guards.log")}</span>
            <span className="sb-toggle-when">{t("strandsBuilder.guards.logWhen")}</span>
          </span>
        </label>
        {(s.logToolCalls || approval.length > 0) && (
          <div className="sb-reveal sb2-guard-wide">
            <Field label={t("strandsBuilder.guards.mask")} hint={t("strandsBuilder.guards.maskWhen")} error={issue("maskArgs")}>
              <input
                className="v2-input mono"
                value={s.maskArgs}
                placeholder="card_number, ssn"
                onChange={(e) => patch({ maskArgs: e.target.value })}
                data-testid="sb-mask-args"
              />
            </Field>
            {s.logToolCalls && (
              <pre className="sb2-logline" aria-label={t("strandsBuilder.guards.sample")}>
                <span className="sb2-logline-tag">[launchpad-tool]</span> {sampleTool} status=success ms=412 args={sampleArgs}
              </pre>
            )}
          </div>
        )}
      </div>
    </Field>
  );
}

/* ── Model behaviour: model provider (SB-2) ──────────────────────────────── */

function ProviderPanel({ form, set, touched }: Pick<SectionProps, "form" | "set"> & { touched: boolean }) {
  const { t } = useTranslation();
  const s = sb2Form(form);
  const patch = useStrands(set);
  const issue = useSb2Issue(form, touched);
  const applies = providerApplies(form);
  const external = applies && s.provider !== "bedrock";
  const load = useLoad(() => (external ? api.listConnections() : Promise.resolve({ connections: [] })), `sb2-conn-${external}`);
  const keys = (load.data?.connections ?? []).filter((c) => c.kind === "api_key");
  const options = keys.map((c) => ({ value: c.name, label: c.name }));
  if (s.providerConnection && !keys.some((c) => c.name === s.providerConnection)) {
    options.unshift({ value: s.providerConnection, label: s.providerConnection });
  }
  // A2A and Mantle agents keep their Bedrock surface; a choice left over from a
  // protocol / source change says why it is not sent
  if (!applies) return s.provider !== "bedrock" ? <p className="sb-fine">{t("strandsBuilder.provider.notHere")}</p> : null;
  return (
    <div className="sb2-provider" data-testid="sb-model-provider">
      <Field label={t("strandsBuilder.provider.label")} hint={t(`strandsBuilder.provider.when.${s.provider}`)}>
        <Segmented<ProviderChoice>
          value={s.provider}
          ariaLabel={t("strandsBuilder.provider.label")}
          options={PROVIDERS.map((value) => ({ value, label: t(`strandsBuilder.provider.kind.${value}`) }))}
          onChange={(provider) => patch({ provider })}
        />
      </Field>
      {external && (
        <div className="sb-reveal">
          <div className="sb-grid-3 sb2-provider-grid">
            <Field label={t("strandsBuilder.provider.connection")} required error={issue("providerConnection")}>
              <Select
                value={s.providerConnection}
                placeholder={load.loading ? t("strandsBuilder.agents.loading") : t("strandsBuilder.provider.pickConnection")}
                options={options}
                onChange={(providerConnection) => patch({ providerConnection })}
                mono
                ariaLabel={t("strandsBuilder.provider.connection")}
                testId="sb-provider-connection"
              />
            </Field>
            <Field
              label={t("strandsBuilder.provider.baseUrl")}
              required={s.provider === "litellm"}
              hint={t(`strandsBuilder.provider.baseUrlWhen.${s.provider}`)}
              error={issue("providerBaseUrl")}
            >
              <input
                className="v2-input mono"
                value={s.providerBaseUrl}
                placeholder={s.provider === "litellm" ? "https://litellm.internal:4000" : "https://api.anthropic.com"}
                onChange={(e) => patch({ providerBaseUrl: e.target.value })}
                data-testid="sb-provider-base-url"
              />
            </Field>
          </div>
          {!load.loading && !keys.length && <p className="sb-fine">{t("strandsBuilder.provider.noKeys")}</p>}
          {issue("providerConverse") && <p className="sb-err">{issue("providerConverse")}</p>}
          <ol className="sb2-keypath" aria-label={t("strandsBuilder.provider.keyPath")}>
            <li>
              <span className="sb2-keypath-k">{t("strandsBuilder.provider.path.connection")}</span>
              <span className="mono">{s.providerConnection || "—"}</span>
            </li>
            <li>
              <span className="sb2-keypath-k">{t("strandsBuilder.provider.path.vault")}</span>
              <span className="mono">GetResourceApiKey</span>
            </li>
            <li>
              <span className="sb2-keypath-k">{t("strandsBuilder.provider.path.runtime")}</span>
              <span className="mono">{form.modelId.trim() || "model"}</span>
            </li>
          </ol>
          <p className="sb-fine">{t("strandsBuilder.provider.modelNote")}</p>
        </div>
      )}
    </div>
  );
}

/* ── Model behaviour: throttling retry (SB-2) ────────────────────────────── */

function RetryPanel({ form, set, touched }: Pick<SectionProps, "form" | "set"> & { touched: boolean }) {
  const { t } = useTranslation();
  const s = sb2Form(form);
  const patch = useStrands(set);
  const issue = useSb2Issue(form, touched);
  const n = (v: string, d: number) => (Number.isFinite(Number(v)) && v.trim() ? Number(v) : d);
  const delays = backoffDelays(n(s.retryAttempts, RETRY_DEFAULTS.attempts), n(s.retryInitial, RETRY_DEFAULTS.initial), n(s.retryMax, RETRY_DEFAULTS.max));
  const peak = Math.max(1, ...delays);
  const total = delays.reduce((a, b) => a + b, 0);
  const field = (key: "retryAttempts" | "retryInitial" | "retryMax") => (
    <Field label={t(`strandsBuilder.retry.${key}`)} error={issue(key)}>
      <input
        className="v2-input mono sb-narrow"
        inputMode="numeric"
        value={s[key]}
        onChange={(e) => patch({ [key]: e.target.value })}
        data-testid={`sb-${key}`}
      />
    </Field>
  );
  return (
    <div className="sb2-retry" data-testid="sb-retry">
      <label className="sb-toggle">
        <input type="checkbox" checked={s.retry} onChange={(e) => patch({ retry: e.target.checked })} data-testid="sb-retry-on" />
        <span className="sb-toggle-text">
          <span className="sb-toggle-name">{t("strandsBuilder.retry.label")}</span>
          <span className="sb-toggle-when">{t(s.retry ? "strandsBuilder.retry.when" : "strandsBuilder.retry.default")}</span>
        </span>
      </label>
      {s.retry && (
        <div className="sb-reveal">
          <div className="sb-grid-3">
            {field("retryAttempts")}
            {field("retryInitial")}
            {field("retryMax")}
          </div>
          <div className="sb2-ladder" aria-label={t("strandsBuilder.retry.ladder", { total })}>
            {delays.length ? (
              delays.map((d, i) => (
                <span key={i} className="sb2-rung" style={{ ["--h" as string]: `${Math.max(8, (d / peak) * 100)}%`, ["--i" as string]: i }}>
                  <span className="sb2-rung-bar" aria-hidden="true" />
                  <span className="sb2-rung-v">{d}s</span>
                </span>
              ))
            ) : (
              <span className="sb-fine">{t("strandsBuilder.retry.noRetry")}</span>
            )}
          </div>
          {delays.length > 0 && <p className="sb-fine">{t("strandsBuilder.retry.ladder", { total })}</p>}
        </div>
      )}
    </div>
  );
}

/* ── Model behaviour: inference parameters ───────────────────────────────── */

export function ModelParamsCard({ form, set, touched }: Pick<SectionProps, "form" | "set"> & { touched: boolean }) {
  const { t } = useTranslation();
  const s = sb(form);
  const patch = useStrands(set);
  const issue = useIssue(form, touched);
  const converse = bedrockOnlyApplies(form);
  const claude = isClaudeModel(form.modelId);
  const numField = (key: "temperature" | "topP" | "maxTokens", placeholder: string) => (
    <Field label={t(`strandsBuilder.params.${key}`)} hint={t(`strandsBuilder.params.${key}When`)} error={issue(key)}>
      <input
        className="v2-input mono"
        inputMode="decimal"
        value={s[key]}
        placeholder={placeholder}
        onChange={(e) => patch({ [key]: e.target.value })}
        data-testid={`sb-${key}`}
      />
    </Field>
  );
  return (
    <Card title={t("strandsBuilder.params.title")} sub={t("strandsBuilder.params.sub")} testId="sb-model-params">
      <div className="v2-form">
        <ProviderPanel form={form} set={set} touched={touched} />
        <div className="sb-grid-3">
          {numField("temperature", "0 – 1")}
          {numField("topP", "0 – 1")}
          {numField("maxTokens", t("strandsBuilder.params.modelDefault"))}
        </div>
        {issue("claudeBoth") && <p className="sb-err">{issue("claudeBoth")}</p>}
        {converse ? (
          <Field label={t("strandsBuilder.params.stop")} hint={t("strandsBuilder.params.stopWhen")} error={issue("stopSequences")}>
            <textarea
              className="v2-textarea code"
              rows={2}
              value={s.stopSequences}
              placeholder={"###\nEND"}
              onChange={(e) => patch({ stopSequences: e.target.value })}
            />
          </Field>
        ) : (
          <p className="sb-fine">{t("strandsBuilder.params.mantleNote")}</p>
        )}
        <div className="sb-toggles">
          <label className={["sb-toggle", claude && converse ? "" : "off"].filter(Boolean).join(" ")}>
            <input
              type="checkbox"
              checked={s.thinking && claude && converse}
              disabled={!claude || !converse}
              onChange={(e) => patch({ thinking: e.target.checked })}
              data-testid="sb-thinking"
            />
            <span className="sb-toggle-text">
              <span className="sb-toggle-name">{t("strandsBuilder.params.thinking")}</span>
              <span className="sb-toggle-when">
                {claude && converse ? t("strandsBuilder.params.thinkingWhen") : t("strandsBuilder.params.thinkingClaudeOnly")}
              </span>
            </span>
          </label>
          <label className={["sb-toggle", converse ? "" : "off"].filter(Boolean).join(" ")}>
            <input
              type="checkbox"
              checked={s.promptCaching && converse}
              disabled={!converse}
              onChange={(e) => patch({ promptCaching: e.target.checked })}
              data-testid="sb-caching"
            />
            <span className="sb-toggle-text">
              <span className="sb-toggle-name">{t("strandsBuilder.params.caching")}</span>
              <span className="sb-toggle-when">{t("strandsBuilder.params.cachingWhen")}</span>
            </span>
          </label>
        </div>
        {s.thinking && claude && converse && (
          <div className="sb-reveal">
            <Field
              label={t("strandsBuilder.params.budget")}
              hint={t("strandsBuilder.params.budgetWhen")}
              error={issue("thinkingBudget") ?? issue("thinkingTemperature") ?? issue("thinkingTopP") ?? issue("thinkingMaxTokens")}
            >
              <input
                className="v2-input mono sb-narrow"
                inputMode="numeric"
                value={s.thinkingBudget}
                onChange={(e) => patch({ thinkingBudget: e.target.value })}
                data-testid="sb-thinking-budget"
              />
            </Field>
          </div>
        )}
        <RetryPanel form={form} set={set} touched={touched} />
      </div>
    </Card>
  );
}

/* ── Model behaviour: prompt template ────────────────────────────────────── */

export function PromptTemplateCard({ form, set, touched }: Pick<SectionProps, "form" | "set"> & { touched: boolean }) {
  const { t } = useTranslation();
  const s = sb(form);
  const patch = useStrands(set);
  const issue = useIssue(form, touched);
  const found = promptPlaceholders(form.systemPrompt);
  const declared = s.variables.map((v) => v.name.trim());
  const undeclared = found.filter((n) => !declared.includes(n));
  const setVar = (i: number, change: Partial<StrandsForm["variables"][number]>) =>
    patch((prev) => ({ variables: prev.variables.map((v, j) => (j === i ? { ...v, ...change } : v)) }));
  const setShot = (i: number, change: Partial<StrandsForm["fewShot"][number]>) =>
    patch((prev) => ({ fewShot: prev.fewShot.map((v, j) => (j === i ? { ...v, ...change } : v)) }));
  return (
    <Card title={t("strandsBuilder.prompt.title")} sub={t("strandsBuilder.prompt.sub")} testId="sb-prompt-template">
      <div className="v2-form">
        <Field label={t("strandsBuilder.prompt.variables")} hint={t("strandsBuilder.prompt.variablesWhen", { ph: "{{city}}" })} error={issue("variables")}>
          {undeclared.length > 0 && (
            <div className="sb-chips">
              <span className="sb-fine">{t("strandsBuilder.prompt.found")}</span>
              {undeclared.map((name) => (
                <button
                  key={name}
                  type="button"
                  className="sb-chip add mono"
                  onClick={() => patch((prev) => ({ variables: [...prev.variables, { name, value: "" }] }))}
                  data-testid={`sb-declare-${name}`}
                >
                  <Plus size={12} aria-hidden="true" />
                  {`{{${name}}}`}
                </button>
              ))}
            </div>
          )}
          {s.variables.length > 0 && (
            <div className="sb-rows">
              {s.variables.map((v, i) => (
                <div key={i} className="sb-row">
                  <input
                    className="v2-input mono"
                    value={v.name}
                    placeholder="city"
                    aria-label={t("strandsBuilder.prompt.varName")}
                    onChange={(e) => setVar(i, { name: e.target.value })}
                  />
                  <input
                    className="v2-input"
                    value={v.value}
                    placeholder={t("strandsBuilder.prompt.varDefault")}
                    aria-label={t("strandsBuilder.prompt.varDefault")}
                    onChange={(e) => setVar(i, { value: e.target.value })}
                  />
                  <button
                    type="button"
                    className="sb-x"
                    aria-label={t("strandsBuilder.remove")}
                    onClick={() => patch((prev) => ({ variables: prev.variables.filter((_, j) => j !== i) }))}
                  >
                    <X size={14} aria-hidden="true" />
                  </button>
                </div>
              ))}
            </div>
          )}
          <div>
            <Button size="sm" onClick={() => patch((prev) => ({ variables: [...prev.variables, { name: "", value: "" }] }))}>
              {t("strandsBuilder.prompt.addVariable")}
            </Button>
          </div>
        </Field>
        <Field label={t("strandsBuilder.prompt.fewShot")} hint={t("strandsBuilder.prompt.fewShotWhen")} error={issue("fewShot")}>
          {s.fewShot.length > 0 && (
            <ol className="sb-shots">
              {s.fewShot.map((shot, i) => (
                <li key={i} className="sb-shot">
                  <span className="sb-shot-n" aria-hidden="true">
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <textarea
                    className="v2-textarea"
                    rows={2}
                    value={shot.question}
                    placeholder={t("strandsBuilder.prompt.question")}
                    aria-label={t("strandsBuilder.prompt.question")}
                    onChange={(e) => setShot(i, { question: e.target.value })}
                  />
                  <textarea
                    className="v2-textarea"
                    rows={2}
                    value={shot.answer}
                    placeholder={t("strandsBuilder.prompt.answer")}
                    aria-label={t("strandsBuilder.prompt.answer")}
                    onChange={(e) => setShot(i, { answer: e.target.value })}
                  />
                  <button
                    type="button"
                    className="sb-x"
                    aria-label={t("strandsBuilder.remove")}
                    onClick={() => patch((prev) => ({ fewShot: prev.fewShot.filter((_, j) => j !== i) }))}
                  >
                    <X size={14} aria-hidden="true" />
                  </button>
                </li>
              ))}
            </ol>
          )}
          <div>
            <Button
              size="sm"
              disabled={s.fewShot.length >= 10}
              onClick={() => patch((prev) => ({ fewShot: [...prev.fewShot, { question: "", answer: "" }] }))}
              testId="sb-add-shot"
            >
              {t("strandsBuilder.prompt.addExample")}
            </Button>
          </div>
        </Field>
      </div>
    </Card>
  );
}

/* ── Conversation & output ───────────────────────────────────────────────── */

export function ConversationOutputCard({ form, set, touched }: Pick<SectionProps, "form" | "set"> & { touched: boolean }) {
  const { t } = useTranslation();
  const s = sb(form);
  const patch = useStrands(set);
  const issue = useIssue(form, touched);
  const a2a = form.protocol === "a2a";
  const schemaText = s.outputSchema.trim();
  const schemaProblem = schemaText ? outputSchemaProblem(s.outputSchema) : null;
  const fields = schemaText && schemaProblem === null ? schemaFields(s.outputSchema) : [];
  const schemaIssue = issue("outputSchemaA2a") ?? issue("outputSchemaGuardrail") ?? issue("outputSchema");
  return (
    <Card title={t("strandsBuilder.output.title")} sub={t("strandsBuilder.output.sub")} testId="sb-conversation-output">
      <div className="v2-form">
        <Field label={t("strandsBuilder.output.conversation")} hint={t(`strandsBuilder.output.when.${s.conversation}`)}>
          <Segmented<ConversationChoice>
            value={s.conversation}
            ariaLabel={t("strandsBuilder.output.conversation")}
            options={(["default", "sliding_window", "summarizing"] as const).map((value) => ({
              value,
              label: t(`strandsBuilder.output.strategy.${value}`),
            }))}
            onChange={(conversation) => patch({ conversation })}
          />
        </Field>
        {s.conversation !== "default" && (
          <div className="sb-reveal sb-grid-2">
            <Field
              label={t(s.conversation === "summarizing" ? "strandsBuilder.output.keepRecent" : "strandsBuilder.output.window")}
              hint={t("strandsBuilder.output.windowWhen")}
              error={issue("windowSize")}
            >
              <input
                className="v2-input mono sb-narrow"
                inputMode="numeric"
                value={s.windowSize}
                onChange={(e) => patch({ windowSize: e.target.value })}
                data-testid="sb-window"
              />
            </Field>
            {s.conversation === "summarizing" && bedrockOnlyApplies(form) && (
              <Field label={t("strandsBuilder.output.summaryModel")} hint={t("strandsBuilder.output.summaryModelWhen")}>
                <input
                  className="v2-input mono"
                  value={s.summaryModel}
                  placeholder={t("strandsBuilder.output.summaryModelPlaceholder")}
                  onChange={(e) => patch({ summaryModel: e.target.value })}
                />
              </Field>
            )}
          </div>
        )}
        <Field label={t("strandsBuilder.output.schema")} hint={a2a ? undefined : t("strandsBuilder.output.schemaWhen")} error={schemaIssue}>
          <div className={["sb-schema", schemaText ? (schemaProblem === null ? "ok" : "bad") : ""].filter(Boolean).join(" ")}>
            <textarea
              className="v2-textarea code"
              rows={schemaText ? 10 : 3}
              value={s.outputSchema}
              disabled={a2a && !schemaText}
              placeholder={a2a ? t("strandsBuilder.output.schemaA2a") : '{ "type": "object", "properties": { … } }'}
              onChange={(e) => patch({ outputSchema: e.target.value })}
              spellCheck={false}
              data-testid="sb-output-schema"
            />
            <div className="sb-schema-foot" aria-live="polite">
              <span className="sb-lamp" aria-hidden="true" />
              <span className="sb-schema-state">
                {!schemaText
                  ? t("strandsBuilder.output.schemaEmpty")
                  : schemaProblem === null
                    ? t("strandsBuilder.output.schemaOk", { fields: fields.join(", ") })
                    : schemaProblem}
              </span>
              {!a2a && (
                <Button size="sm" onClick={() => patch({ outputSchema: EXAMPLE_OUTPUT_SCHEMA })}>
                  {t("strandsBuilder.output.example")}
                </Button>
              )}
            </div>
          </div>
        </Field>
      </div>
    </Card>
  );
}
