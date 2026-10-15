import { ChevronDown, Plus, X } from "lucide-react";
import { type ReactNode, useEffect, useId, useRef } from "react";
import { useTranslation } from "react-i18next";

import { type AgentForm, strandsContext } from "../../../lib/agent-spec";
import {
  bedrockOnlyApplies,
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
import { Button, Card, Field, Segmented } from "../../ui";
import { ISSUE_KEYS_SHOWN_ON_CONTINUE, sb, type StrandsGroupKey } from "./strandsGroups";
import type { SectionProps } from "./wizardKit";
import "./strands-sections.css";

/**
 * The Strands (zip_runtime) configure step as five collapsible groups — Basics /
 * Tools / Model behaviour / Conversation & output / Advanced (strands-builder §2,
 * "共同要求") — plus the SB-1 cards that fill them. Every group and option carries
 * one "when to use" line; the closed head shows a mono readout of what is set, so
 * a collapsed group still says what it holds.
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
