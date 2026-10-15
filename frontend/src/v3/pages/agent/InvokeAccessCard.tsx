import { Copy, Download } from "lucide-react";
import { type ReactNode, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { ErrorCallout } from "../../../components/ErrorCallout";
import {
  type AgentInvokeAccess,
  INVOKE_INCLUDES,
  type InvokeInclude,
  type InvokeQualifier,
  invokeAccessApi,
  policyFileName,
  policyText,
  SAMPLE_LANGS,
  type SampleLang,
  saveBlob,
} from "../../../lib/api/invokeAccess";
import { useProject } from "../../../workspace/project-context";
import { useWorkspace } from "../../../workspace/workspace-context";
import { useFailure } from "../../failure";
import { useLoad, useToast } from "../../hooks";
import { Reasoned } from "../../reason";
import { Btn, Chip, Filters, Lamp, Notice, Panel, Skeleton } from "../../ui";
import { Copyable } from "./Copyable";
import "./invokeAccess.css";

export type Scope = InvokeQualifier | "all";

/**
 * 在你的应用中调用 (invoke-access §0): what a backend developer needs to give an IAM
 * role, in three stations — 01 the resources (ARN, endpoints, account, region),
 * 02 the permission (the least-privilege policy, or for a JWT agent the Bearer
 * configuration instead), 03 the call (CLI / Python / TypeScript, values filled in).
 * Every ARN and the policy come from the backend; nothing is assembled here.
 */
export function InvokeAccessCard({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const { current: ws } = useWorkspace();
  const { current: project } = useProject();
  const [scope, setScope] = useState<Scope>("all");
  const [include, setInclude] = useState<InvokeInclude[]>([]);
  const [lang, setLang] = useState<SampleLang>("python");
  const qualifier = scope === "all" ? null : scope;
  const key = `v3-ia:${ws?.id ?? ""}:${project?.id ?? ""}:${agentId}:${scope}:${include.join(",")}`;
  const load = useLoad(() => invokeAccessApi.agent(agentId, { qualifier, include }), key);
  // keep the last answer on screen while a changed scope reloads (no flash of skeleton)
  const last = useRef<AgentInvokeAccess | null>(null);
  if (load.data && load.data.agent_id === agentId) last.current = load.data;
  else if (last.current?.agent_id !== agentId) last.current = null;
  const data = load.data ?? last.current;

  const copy = (text: string, label: string) =>
    navigator.clipboard
      .writeText(text)
      .then(() => {
        failed.clear();
        toast("ok", t("v3.invokeAccess.copied", { label }));
      })
      .catch(() => failed.fail(t("v3.invokeAccess.copyFailed")));

  const toggle = (opt: InvokeInclude) =>
    setInclude((prev) => (prev.includes(opt) ? prev.filter((o) => o !== opt) : [...prev, opt].sort()));

  return (
    <Panel
      title={t("v3.invokeAccess.title")}
      end={data && <Chip s={data.mode === "jwt" ? "wait" : "info"}>{t(`v3.invokeAccess.mode.${data.mode}`)}</Chip>}
    >
      <div className="v3-ia" data-testid="v3-invoke-access">
        <p className="v3-hint" style={{ margin: "0 0 18px" }}>{t("v3.invokeAccess.sub")}</p>
        <ErrorCallout error={failed.error} params={{ agent_id: agentId }} onDismiss={failed.clear} data-testid="v3-invoke-access-error" />
        {load.loading && !data && <Skeleton rows={4} />}
        {load.failure != null && !data && <Notice s="act"><ApiErrorDetail error={load.failure} /></Notice>}
        {data && (
          <InvokeAccessView data={data} scope={scope} onScope={setScope} include={include} onToggle={toggle}
            lang={lang} onLang={setLang} docKey={key} onCopy={(text, label) => void copy(text, label)} />
        )}
      </div>
    </Panel>
  );
}

/** The card's body for one answer (rendered statically in the tests). */
export function InvokeAccessView({ data, scope, onScope, include, onToggle, lang, onLang, docKey, onCopy }: {
  data: AgentInvokeAccess;
  scope: Scope;
  onScope: (s: Scope) => void;
  include: InvokeInclude[];
  onToggle: (opt: InvokeInclude) => void;
  lang: SampleLang;
  onLang: (l: SampleLang) => void;
  docKey: string;
  onCopy: (text: string, label: string) => void;
}) {
  const { t } = useTranslation();
  const qualifier = scope === "all" ? null : scope;
  return (
    <>
        <Station n="01" label={t("v3.invokeAccess.resources")} hint={t(`v3.invokeAccess.kind.${data.kind}`)} done>
          <dl className="v3-kv">
            <dt>{t("v3.invokeAccess.arn")}</dt>
            <dd><Copyable value={data.arn} label="ARN" /></dd>
            <dt>{t("v3.invokeAccess.account")}</dt>
            <dd className="mono">{data.account}</dd>
            <dt>{t("v3.invokeAccess.region")}</dt>
            <dd className="mono">{data.region}</dd>
          </dl>
          <EndpointBoard data={data} scope={scope} onScope={onScope} />
          {data.resources
            .filter((r) => r.kind === "runtime_endpoint" && r.arn)
            .map((r) => (
              <div key={r.qualifier} className="v3-hint" style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <Lamp s={r.production ? "ok" : "off"} />
                <span className="mono" style={{ minWidth: 80 }}>{r.qualifier}</span>
                <Copyable value={r.arn ?? ""} label={r.qualifier ?? "ARN"} />
              </div>
            ))}
        </Station>

        <Station n="02" label={t("v3.invokeAccess.permission")}
          hint={data.mode === "jwt" ? t("v3.invokeAccess.jwtHint") : t("v3.invokeAccess.policyHint")} done>
          {data.mode === "jwt" ? (
            <JwtBlock data={data} />
          ) : (
            <>
              <div className="v3-ia-checks" role="group" aria-label={t("v3.invokeAccess.options")}>
                {INVOKE_INCLUDES.map((opt) => {
                  const off = opt === "stop_session" && data.kind === "harness";
                  return (
                    <label key={opt} aria-disabled={off} title={off ? t("v3.invokeAccess.include.harnessNoStop") : undefined}>
                      <Reasoned disabledReason={t("v3.invokeAccess.include.harnessNoStop")}>
                        <input type="checkbox" checked={include.includes(opt)} disabled={off} onChange={() => onToggle(opt)} />
                      </Reasoned>
                      {t(`v3.invokeAccess.include.${opt}`)}
                    </label>
                  );
                })}
              </div>
              {data.policy && (
                <Code
                  key={docKey}
                  name={policyFileName(data.name, qualifier)}
                  text={policyText(data.policy)}
                  testId="v3-invoke-access-policy"
                  onCopy={() => onCopy(policyText(data.policy!), t("v3.invokeAccess.policy"))}
                  onDownload={() =>
                    saveBlob(new Blob([policyText(data.policy!)], { type: "application/json" }), policyFileName(data.name, qualifier))
                  }
                />
              )}
            </>
          )}
        </Station>

        <Station n="03" label={t("v3.invokeAccess.call")} hint={t("v3.invokeAccess.callHint", { qualifier: data.sample_qualifier })}>
          <div className="v3-ia-tools">
            <Filters value={lang} onChange={onLang} options={SAMPLE_LANGS.map((l) => ({ value: l, label: t(`v3.invokeAccess.lang.${l}`) }))} />
          </div>
          <Code
            key={`${docKey}:${lang}`}
            name={t(`v3.invokeAccess.file.${lang}`)}
            text={data.samples[lang]}
            testId="v3-invoke-access-sample"
            onCopy={() => onCopy(data.samples[lang], t(`v3.invokeAccess.lang.${lang}`))}
          />
          <Notes notes={data.notes} />
        </Station>
    </>
  );
}

function Station({ n, label, hint, done, children }: { n: string; label: string; hint?: string; done?: boolean; children: ReactNode }) {
  return (
    <section className="v3-ia-station" data-done={done ? "true" : undefined}>
      <span className="v3-ia-num" aria-hidden="true">{n}</span>
      <div className="v3-ia-body">
        <div className="v3-ia-label">
          {label}
          {hint && <small>{hint}</small>}
        </div>
        {children}
      </div>
    </section>
  );
}

function EndpointBoard({ data, scope, onScope }: { data: AgentInvokeAccess; scope: Scope; onScope: (s: Scope) => void }) {
  const { t } = useTranslation();
  const keys: Scope[] = data.available_qualifiers.length > 1 ? ["all", ...data.available_qualifiers] : data.available_qualifiers;
  return (
    <div className="v3-ia-board" role="group" aria-label={t("v3.invokeAccess.endpoints")}>
      {keys.map((q) => {
        const prod = q === data.production_qualifier;
        return (
          <button key={q} type="button" className="v3-ia-key" data-prod={prod} aria-pressed={scope === q || keys.length === 1}
            onClick={() => onScope(q)}>
            <Lamp s={q === "all" ? "info" : prod ? "ok" : "off"} live={prod} />
            <span className="q">{q === "all" ? t("v3.invokeAccess.allEndpoints") : q}</span>
            <span className="role">{t(`v3.invokeAccess.role.${q === "all" ? "all" : prod ? "production" : q}`)}</span>
          </button>
        );
      })}
    </div>
  );
}

function JwtBlock({ data }: { data: AgentInvokeAccess }) {
  const { t } = useTranslation();
  const jwt = data.jwt;
  const list = (values: string[] | undefined) => (values && values.length ? values.join(", ") : "—");
  return (
    <>
      <Notice s="wait">{t("v3.invokeAccess.jwtNoIam")}</Notice>
      {jwt && !jwt.external ? (
        <dl className="v3-kv">
          <dt>{t("v3.invokeAccess.jwt.issuer")}</dt>
          <dd>{jwt.issuer ? <Copyable value={jwt.issuer} label={t("v3.invokeAccess.jwt.issuer")} /> : "—"}</dd>
          <dt>{t("v3.invokeAccess.jwt.discovery")}</dt>
          <dd className="mono">{jwt.discovery_url ?? "—"}</dd>
          <dt>{t("v3.invokeAccess.jwt.clients")}</dt>
          <dd className="mono">{list(jwt.allowed_clients)}</dd>
          <dt>{t("v3.invokeAccess.jwt.audience")}</dt>
          <dd className="mono">{list(jwt.allowed_audience)}</dd>
          <dt>{t("v3.invokeAccess.jwt.scopes")}</dt>
          <dd className="mono">{list(jwt.allowed_scopes)}</dd>
        </dl>
      ) : (
        <span className="v3-hint">{t("v3.invokeAccess.jwt.external")}</span>
      )}
    </>
  );
}

function Code({ name, text, testId, onCopy, onDownload }: {
  name: string; text: string; testId: string; onCopy: () => void; onDownload?: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="v3-ia-code v3-ia-fresh">
      <header>
        <span>{name}</span>
        <span className="end">
          <Btn size="sm" kind="ghost" onClick={onCopy}><Copy size={13} /> {t("v3.invokeAccess.copy")}</Btn>
          {onDownload && <Btn size="sm" kind="ghost" onClick={onDownload}><Download size={13} /> {t("v3.invokeAccess.download")}</Btn>}
        </span>
      </header>
      <pre className="v3-pre" data-testid={testId}>{text}</pre>
    </div>
  );
}

const KNOWN_NOTES = ["cross_account", "harness_action_unverified", "harness_no_stop_session", "memory_unresolved"];

function Notes({ notes }: { notes: string[] }) {
  const { t } = useTranslation();
  const shown = notes.filter((n) => KNOWN_NOTES.includes(n));
  if (shown.length === 0) return null;
  return (
    <ul className="v3-ia-notes">
      {shown.map((n) => <li key={n}>{t(`v3.invokeAccess.note.${n}`)}</li>)}
    </ul>
  );
}
