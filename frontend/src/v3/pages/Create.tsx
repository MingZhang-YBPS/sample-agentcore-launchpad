import { ArrowRight, Boxes, Code2, Download, Layers, Rocket, Sparkles, Workflow } from "lucide-react";
import { type ReactNode, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { api, type AgentTemplateInfo, ApiError, errorMessage } from "../../lib/api";
import {
  AGENT_NAME_RE,
  type AgentForm,
  type AgentFormCatalogs,
  agentFormValid,
  buildAgentSpec,
  emptyAgentForm,
  randomAgentSlug,
  resolveKb,
  slugFromDisplayName,
} from "../../lib/agent-spec";
import { formFromTemplate } from "../../v2/pages/agents/templateForms";
import { useProdLock } from "../../workspace/useProdLock";
import { useWorkspace } from "../../workspace/workspace-context";
import { useLoad, useToast } from "../hooks";
import { Btn, Chip, Notice, PageHead, Panel, Skeleton } from "../ui";
import { Term } from "../onboarding/Glossary";

const BLANK = "blank";

/** The slug follows the display name until the member edits it by hand. */
function followSlug(displayName: string, current: string): string {
  return slugFromDisplayName(displayName) ?? (/^agent-[a-z0-9]{6}$/.test(current) ? current : randomAgentSlug());
}

function WayCard({ to, icon, title, desc }: { to: string; icon: ReactNode; title: string; desc: string }) {
  return (
    <Link to={to} className="v3-way">
      <span className="icon" aria-hidden="true">{icon}</span>
      <span className="text">
        <b>{title}</b>
        <small>{desc}</small>
      </span>
      <ArrowRight size={14} className="go" aria-hidden="true" />
    </Link>
  );
}

/**
 * Launch — one screen from "I want an agent" to a deploy in flight. Scenario
 * templates and a blank start fill a quick form for a managed Harness agent
 * (name, instructions, knowledge) that deploys from here; every other way to
 * build — another method, the full form, the assistant, the canvas, an import —
 * is one click away and opens where it is handled.
 */
export function V3Create() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useToast();
  const { can } = useAuth();
  const prodLock = useProdLock();
  const { current } = useWorkspace();
  const canDeploy = can("agents.deploy") && !prodLock.locked;
  const templates = useLoad(() => api.agentTemplates(), "v3-templates");
  const kbs = useLoad(() => api.listAttachableKnowledgeBases(), `v3-create-kbs:${current?.id ?? ""}`);
  const [pick, setPick] = useState<string>(BLANK);
  const [form, setForm] = useState<AgentForm>(() => emptyAgentForm("harness"));
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const formRef = useRef<HTMLFormElement>(null);

  const rows = useMemo(() => templates.data?.templates ?? [], [templates.data]);
  const template: AgentTemplateInfo | null = rows.find((r) => r.key === pick) ?? null;
  // only a managed Harness agent deploys from the quick form; a template for
  // another method (a Strands toolkit demo) continues in the full form
  const quick = !template || template.method === "harness";
  const activeKbs = (kbs.data?.items ?? []).filter((kb) => String(kb.status ?? "ACTIVE").toUpperCase() === "ACTIVE");
  const needsKb = template?.knowledge === "required";

  const choose = (key: string) => {
    setPick(key);
    setTouched(false);
    setError(null);
    const entry = rows.find((r) => r.key === key);
    // keep what the member typed for the name: picking a scenario sets the defaults only
    setForm((prev) => ({
      ...(entry ? formFromTemplate(entry) : emptyAgentForm("harness")),
      displayName: prev.displayName,
      name: prev.name,
      nameEdited: prev.nameEdited,
    }));
  };

  const catalogs: AgentFormCatalogs = {
    gatewayTargets: [],
    remoteMcp: [],
    storedGatewayConfig: {},
    kbInfo: (id) => resolveKb(id, kbs.data?.items ?? [], []),
  };
  const nameError = !AGENT_NAME_RE.test(form.name) ? t("v2.agents.wizard.errName") : null;
  const promptError = !form.systemPrompt.trim() ? t("v2.agents.wizard.errPrompt") : null;
  const kbError = needsKb && form.selectedKbs.length === 0 ? t("v3.create.errKb") : null;
  const valid = agentFormValid(form, catalogs, { knobIssues: [], byocUploading: false, connections: [] }) && !kbError;
  const fullForm = template ? `/v2/agents?view=new&scenario=${encodeURIComponent(template.key)}` : "/v2/agents?view=new&method=harness";

  const deploy = async () => {
    setTouched(true);
    if (!valid) {
      window.requestAnimationFrame(() => formRef.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus());
      return;
    }
    if (busy || !canDeploy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await api.createAgent(buildAgentSpec(form, catalogs));
      toast("ok", t("v2.agents.wizard.started", { name: res.agent.display_name || res.agent.name }));
      navigate(`/v3/agents?id=${encodeURIComponent(res.agent.id)}`);
    } catch (err) {
      setError(err instanceof ApiError ? `${err.message}` : errorMessage(err));
      setBusy(false);
    }
  };

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead eyebrow={t("v3.create.eyebrow")} title={t("v3.create.title")} sub={t("v3.create.sub")} />
      {current && <Notice s={current.tier === "prod" ? "wait" : "info"}>{t("v3.ux.deployTarget", { name: current.name, region: current.region })}</Notice>}
      {!canDeploy && <Notice s="wait">{prodLock.title ?? t("v2.agents.noPermission")}</Notice>}

      <div className="v3-grid v3-split" style={{ alignItems: "start" }}>
        <div style={{ display: "grid", gap: 16 }}>
          <Panel title={t("v3.create.startFrom")}>
            {templates.error ? <Notice s="act">{templates.error} <button type="button" className="v3-btn sm" onClick={templates.reload}>{t("v3.ux.retry")}</button></Notice> : templates.loading && !templates.data ? (
              <Skeleton rows={2} />
            ) : (
              <div className="v3-scenarios" role="radiogroup" aria-label={t("v3.create.startFrom")}
                onKeyDown={(e) => {
                  if (!["ArrowRight", "ArrowDown", "ArrowLeft", "ArrowUp", "Home", "End"].includes(e.key)) return;
                  const options = [...e.currentTarget.querySelectorAll<HTMLButtonElement>('[role="radio"]')];
                  const index = options.indexOf(e.target as HTMLButtonElement);
                  if (index < 0 || busy) return;
                  e.preventDefault();
                  const next = e.key === "Home" ? 0 : e.key === "End" ? options.length - 1 :
                    (index + (e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
                  options[next].focus();
                  options[next].click();
                }}>
                {rows.map((r) => (
                  <button key={r.key} type="button" role="radio" aria-checked={pick === r.key} tabIndex={pick === r.key ? 0 : -1} disabled={busy}
                    className={pick === r.key ? "v3-scenario on" : "v3-scenario"} onClick={() => choose(r.key)}>
                    <b>{t(r.label_key)}</b>
                    <small>{t(r.description_key)}</small>
                    <span className="tags">
                      {r.method !== "harness" && <Chip>{t("v3.create.strands")}</Chip>}
                      {r.knowledge === "required" && <Chip s="info">{t("templates.needsDocs")}</Chip>}
                      {r.guardrail && <Chip s="ok">{t("templates.piiOn")}</Chip>}
                    </span>
                  </button>
                ))}
                {/* the catalogue carries its own blank start; older backends may not */}
                {!rows.some((r) => r.key === BLANK) && (
                  <button type="button" role="radio" aria-checked={pick === BLANK} tabIndex={pick === BLANK ? 0 : -1} disabled={busy}
                    className={pick === BLANK ? "v3-scenario on" : "v3-scenario"} onClick={() => choose(BLANK)}>
                    <b>{t("v3.create.blank")}</b>
                    <small>{t("v3.create.blankSub")}</small>
                  </button>
                )}
              </div>
            )}
          </Panel>

          <Panel title={quick ? <Term term="harness">{t("v3.create.quick")}</Term> : t("v3.create.notQuick")} signal={quick ? "ok" : undefined}
            end={<Link to={fullForm} className="v3-btn ghost sm">{t("v3.create.fullForm")} <ArrowRight size={13} /></Link>}>
            {!quick ? (
              <div style={{ display: "grid", gap: 12 }}>
                <p style={{ margin: 0, color: "var(--v3-text-2)" }}>{t("v3.create.notQuickSub")}</p>
                <div><Link to={fullForm} className="v3-btn primary">{t("v3.create.continueFull")} <ArrowRight size={14} /></Link></div>
              </div>
            ) : (
              <form
                ref={formRef}
                aria-busy={busy}
                onSubmit={(e) => {
                  e.preventDefault();
                  void deploy();
                }}
                style={{ display: "grid", gap: 16 }}
              >
                <fieldset disabled={busy} className="v3-form-fields">
                <div className="v3-grid c2" style={{ alignItems: "start" }}>
                  <label className="v3-field">
                    <span>{t("v2.agents.wizard.displayName")}</span>
                    <input className="v3-input" value={form.displayName} maxLength={64}
                      placeholder={t("v2.agents.wizard.displayNamePlaceholder")}
                      onChange={(e) => {
                        const value = e.target.value;
                        setForm((prev) => ({
                          ...prev,
                          displayName: value,
                          ...(prev.nameEdited ? {} : { name: value.trim() ? followSlug(value, prev.name) : "" }),
                        }));
                      }} />
                  </label>
                  <label className="v3-field">
                    <span>{t("v2.agents.wizard.resourceName")}</span>
                    <input className="v3-input mono" value={form.name} maxLength={48} placeholder="hr-assistant"
                      aria-describedby="v3-resource-name-hint"
                      aria-invalid={touched && !!nameError}
                      onChange={(e) => setForm((prev) => ({ ...prev, name: e.target.value.toLowerCase(), nameEdited: true }))} />
                    <small id="v3-resource-name-hint" className={touched && nameError ? "v3-err" : "v3-hint"}>{touched && nameError ? nameError : t("v2.agents.wizard.nameHint")}</small>
                  </label>
                </div>
                <label className="v3-field">
                  <span>{t("v2.agents.wizard.quick.promptLabel")}</span>
                  <textarea className="v3-input" rows={7} maxLength={20000} value={form.systemPrompt}
                    placeholder={t("v2.agents.wizard.promptPlaceholder")} aria-invalid={touched && !!promptError}
                    aria-describedby={touched && promptError ? "v3-prompt-error" : undefined}
                    onChange={(e) => setForm((prev) => ({ ...prev, systemPrompt: e.target.value }))} />
                  {touched && promptError && <small id="v3-prompt-error" className="v3-err">{promptError}</small>}
                </label>
                <div className="v3-field" role="group" aria-label={t("v3.create.knowledge")} tabIndex={-1}
                  aria-invalid={touched && !!kbError} aria-describedby={touched && kbError ? "v3-kb-error" : undefined}>
                  <span>{t("v3.create.knowledge")}{needsKb ? ` · ${t("v3.create.required")}` : ""}</span>
                  {kbs.error ? <Notice s="act">{kbs.error} <button type="button" className="v3-btn sm" onClick={kbs.reload}>{t("v3.ux.retry")}</button></Notice> : kbs.loading && !kbs.data ? (
                    <Skeleton rows={1} />
                  ) : activeKbs.length === 0 ? (
                    <small className="v3-hint">
                      {t("v3.create.noKb")}{" "}
                      <Link to="/v2/knowledge-bases?view=new" style={{ textDecoration: "underline" }}>{t("v3.kb.create")}</Link>
                    </small>
                  ) : (
                    <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                      {activeKbs.map((kb) => {
                        const on = form.selectedKbs.includes(kb.kb_id);
                        return (
                          <button key={kb.kb_id} type="button" className={`v3-btn sm${on ? "" : " ghost"}`} aria-pressed={on}
                            onClick={() => setForm((prev) => ({
                              ...prev,
                              selectedKbs: on ? prev.selectedKbs.filter((x) => x !== kb.kb_id) : [...prev.selectedKbs, kb.kb_id],
                            }))}>
                            <Layers size={13} /> {kb.name}
                          </button>
                        );
                      })}
                    </div>
                  )}
                  {touched && kbError && <small id="v3-kb-error" className="v3-err">{kbError}</small>}
                </div>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", color: "var(--v3-text-3)", fontSize: 13 }}>
                  <span>{t("v3.create.carries")}</span>
                  <Chip>{form.modelId}</Chip>
                  {form.longTerm && <Chip s="info">{t("v3.create.longTerm")}</Chip>}
                  {form.guardrail !== "off" && <Chip s="ok">{t("templates.piiOn")}</Chip>}
                  <span>· {t("v3.create.carriesHint")}</span>
                </div>
                {error && <Notice s="act">{error}</Notice>}
                <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                  <Btn kind="primary" type="submit" disabled={busy || !canDeploy}><Rocket size={14} /> {busy ? t("v3.create.deploying") : t("v3.create.deploy")}</Btn>
                  <span style={{ color: "var(--v3-text-3)", fontSize: 13 }}>{t("v3.create.deployHint")}</span>
                </div>
                </fieldset>
              </form>
            )}
          </Panel>
        </div>

        <Panel title={t("v3.create.otherWays")}>
          <div style={{ display: "grid", gap: 8 }}>
            <WayCard to="/v2/agents?view=new&method=zip_runtime" icon={<Workflow size={16} />} title={t("v3.create.wayStrands")} desc={t("v3.create.wayStrandsSub")} />
            <WayCard to="/v2/agents?view=new&method=container" icon={<Boxes size={16} />} title={t("v3.create.waySdk")} desc={t("v3.create.waySdkSub")} />
            <WayCard to="/v2/agents?view=new&method=byoc" icon={<Code2 size={16} />} title={t("v3.create.wayByoc")} desc={t("v3.create.wayByocSub")} />
            <div style={{ height: 1, background: "var(--v3-line)", margin: "6px 0" }} aria-hidden="true" />
            <WayCard to="/v3/assistant" icon={<Sparkles size={16} />} title={t("v3.create.wayAssistant")} desc={t("v3.create.wayAssistantSub")} />
            <WayCard to="/create/studio" icon={<Workflow size={16} />} title={t("v3.create.wayStudio")} desc={t("v3.create.wayStudioSub")} />
            <WayCard to="/agents/import" icon={<Download size={16} />} title={t("v3.create.wayImport")} desc={t("v3.create.wayImportSub")} />
            <WayCard to="/agents/new" icon={<Layers size={16} />} title={t("v3.create.wayPresets")} desc={t("v3.create.wayPresetsSub")} />
          </div>
        </Panel>
      </div>
    </div>
  );
}
