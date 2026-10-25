import { type ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { classicDetailPath } from "../../../lib/agent-admin/edit";
import { inboundFacts, useInboundSwitch } from "../../../lib/agent-admin/inbound";
import type { AgentPermissions } from "../../../lib/agent-admin/permissions";
import type { AgentInfo, InboundAuthDefaultResult } from "../../../lib/api";
import {
  issuerMismatch,
  type JwtFormState,
  jwtFormFromConfig,
  jwtFormProblem,
  jwtSummary,
  m2mCurlExample,
  REACHABILITY_KEYS,
  switchDialogInitial,
  withDiscoveryUrl,
} from "../../../lib/inbound-auth";
import { useToast } from "../../hooks";
import { Btn, Chip, Confirm, Dialog, Notice, Panel } from "../../ui";

type Switch = ReturnType<typeof useInboundSwitch>;

function Reachability({ mismatch }: { mismatch: NonNullable<ReturnType<typeof issuerMismatch>> }) {
  const { t } = useTranslation();
  const keys = REACHABILITY_KEYS.agent;
  return (
    <Notice s="wait">
      <strong>{t(keys.title)}</strong>{" "}
      {t(keys.body, { issuer: mismatch.agentIssuer, workspaceIssuer: mismatch.workspaceIssuer })}
    </Notice>
  );
}

/**
 * The switch-to-JWT dialog, prefilled from the workspace default (else the
 * Cognito preset). The discovery URL and the three allowed lists are editable
 * here; custom-claim rules from the prefill are carried over as they are — the
 * classic page has the full claim editor.
 */
function JwtDialog({
  agentId,
  defaults,
  sw,
  onClose,
}: {
  agentId: string;
  defaults: InboundAuthDefaultResult;
  sw: Switch;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [initial] = useState(() => switchDialogInitial(defaults.default, defaults.cognito));
  const [form, setForm] = useState<JwtFormState>(initial.form);
  const [touched, setTouched] = useState(false);
  const problem = jwtFormProblem(form);
  const mismatch = issuerMismatch(form.discovery_url, defaults.cognito_issuer);
  const prefillKey = { default: "fromDefault", cognito: "fromCognito", empty: "fromEmpty" }[initial.prefill];
  const submit = async () => {
    setTouched(true);
    if (problem) return;
    if (await sw.switchToJwt(form)) onClose();
  };
  const list = (key: "allowed_clients" | "allowed_audience" | "allowed_scopes", label: string) => (
    <label className="v3-field">
      <span>{t(label)}</span>
      <input className="v3-input mono" value={form[key]} onChange={(e) => setForm({ ...form, [key]: e.target.value })} />
    </label>
  );
  return (
    <Dialog
      wide
      title={t("inboundAuth.switch.confirmTitle.jwt")}
      onClose={() => !sw.saving && onClose()}
      foot={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={sw.saving}>{t("v3.agent.action.cancel")}</Btn>
          <Btn kind="primary" disabled={sw.saving} onClick={() => void submit()}>{t("inboundAuth.switch.to.jwt")}</Btn>
        </>
      }
    >
      <div style={{ display: "grid", gap: 12 }} data-testid="v3-agent-inbound-jwt">
        <p style={{ margin: 0 }}>{t("inboundAuth.switch.inPlace")}</p>
        <Notice s="wait">{t("inboundAuth.switch.effect.jwt")}</Notice>
        <span className="v3-hint">{t("inboundAuth.switch.prefillNote", { source: t(`inboundAuth.switch.${prefillKey}`) })}</span>
        {defaults.cognito && (
          <div>
            <Btn size="sm" onClick={() => setForm(jwtFormFromConfig(defaults.cognito))}>{t("inboundAuth.useCognito")}</Btn>
          </div>
        )}
        <label className="v3-field">
          <span>{t("inboundAuth.discoveryUrl")}</span>
          <input
            className="v3-input mono"
            value={form.discovery_url}
            placeholder="https://…/.well-known/openid-configuration"
            onChange={(e) => setForm(withDiscoveryUrl(form, e.target.value))}
            data-testid="v3-agent-inbound-discovery"
          />
        </label>
        {mismatch && <Reachability mismatch={mismatch} />}
        {list("allowed_clients", "inboundAuth.allowedClients")}
        {list("allowed_audience", "inboundAuth.allowedAudience")}
        {list("allowed_scopes", "inboundAuth.allowedScopes")}
        <span className="v3-hint">{t("inboundAuth.listPlaceholder")}</span>
        {form.custom_claims.length > 0 && (
          <span className="v3-hint">{t("v3.agent.inbound.claimsKept", { count: form.custom_claims.length })}</span>
        )}
        <Link to={classicDetailPath(agentId)} className="v3-inline-link">{t("v3.agent.inbound.classic")}</Link>
        {touched && problem && <Notice s="act">{t(problem)}</Notice>}
        {sw.error != null && <Notice s="act"><ApiErrorDetail error={sw.error} data-testid="v3-agent-inbound-error" /></Notice>}
      </div>
    </Dialog>
  );
}

/**
 * 入站认证 on the agent page: the live mode, the pin, the caller example, and
 * the in-place switch (a new version on the SAME runtime).
 */
export function InboundCard({ agent, perms, onSwitched }: { agent: AgentInfo; perms: AgentPermissions; onSwitched: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const facts = inboundFacts(agent);
  const [confirming, setConfirming] = useState(false);
  const sw = useInboundSwitch(
    { agentId: agent.id, mode: facts.mode, jwtDiscoveryUrl: facts.jwt?.discovery_url ?? null, busy: agent.status === "deploying" },
    {
      onSwitched: (mode) => {
        toast("ok", t("inboundAuth.switch.started", { mode: t(`inboundAuth.mode.${mode}`) }));
        onSwitched();
      },
    },
  );
  const canSwitch = perms.canEdit(agent);
  const close = () => {
    setConfirming(false);
    sw.clearError();
  };
  const jwt = facts.jwt;
  const rows: [string, ReactNode][] = [
    [t("inboundAuth.modeLabel"), <Chip key="mode" s={facts.mode === "jwt" ? "wait" : "info"}>{t(`inboundAuth.mode.${facts.mode}`)}</Chip>],
    [t("inboundAuth.pinLabel"), facts.pinned ? t(`inboundAuth.pin.${facts.pinned}`) : t("inboundAuth.pin.inherit")],
    ...(facts.mode === "jwt" && jwt
      ? ([
          [
            t("inboundAuth.discoveryUrl"),
            <span key="url" style={{ display: "inline-flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
              <span className="mono">{jwt.discovery_url}</span>
              {jwt.source_connection && <Chip s="info">{t("inboundAuth.idpSource.fromConnection", { name: jwt.source_connection })}</Chip>}
            </span>,
          ],
          [t("inboundAuth.restrictions"), <span key="r" className="mono">{jwtSummary(jwt) || "—"}</span>],
        ] as [string, ReactNode][])
      : []),
  ];
  return (
    <Panel
      title={t("inboundAuth.title")}
      end={
        canSwitch ? (
          <Btn
            size="sm"
            kind={sw.target === "jwt" ? "primary" : undefined}
            disabled={sw.switchBlocked || perms.locked}
            disabledReason={perms.locked ? perms.lockTitle ?? t("story.reason.locked")
              : sw.defaultsFailed ? t("inboundAuth.switch.defaultsFailed") : t("story.reason.unavailable")}
            onClick={() => setConfirming(true)}
          >
            {t(`inboundAuth.switch.to.${sw.target}`)}
          </Btn>
        ) : undefined
      }
    >
      <div style={{ display: "grid", gap: 12 }} data-testid="v3-agent-inbound">
        <span className="v3-hint">{t("inboundAuth.detailSub")}</span>
        <dl className="v3-kv">
          {rows.map(([label, value]) => (
            <div key={label} style={{ display: "contents" }}>
              <dt>{label}</dt>
              <dd>{value}</dd>
            </div>
          ))}
        </dl>
        {sw.mismatch && <Reachability mismatch={sw.mismatch} />}
        {canSwitch && sw.defaultsFailed && (
          <Notice s="act">
            {t("inboundAuth.switch.defaultsFailed")} {sw.defaults.error}{" "}
            <button type="button" className="v3-btn sm" onClick={sw.defaults.reload}>{t("v3.ux.retry")}</button>
          </Notice>
        )}
        {facts.mode === "jwt" && facts.invokeUrl && jwt && (
          <>
            <strong>{t("inboundAuth.callerTitle")}</strong>
            <span className="v3-hint">{t("inboundAuth.callerNote")}</span>
            <pre className="v3-pre">{m2mCurlExample(facts.invokeUrl, jwt.discovery_url, jwt.allowed_scopes ?? [])}</pre>
          </>
        )}
      </div>
      {confirming && sw.target === "iam" && (
        <Confirm
          title={t("inboundAuth.switch.confirmTitle.iam")}
          confirmLabel={t("inboundAuth.switch.to.iam")}
          cancelLabel={t("v3.agent.action.cancel")}
          danger
          busy={sw.saving}
          onCancel={close}
          onConfirm={() => void sw.switchToIam().then((ok) => ok && close())}
        >
          <p style={{ marginTop: 0 }}>{t("inboundAuth.switch.inPlace")}</p>
          <p>{t("inboundAuth.switch.effect.iam")}</p>
          {sw.error != null && <Notice s="act"><ApiErrorDetail error={sw.error} data-testid="v3-agent-inbound-error" /></Notice>}
        </Confirm>
      )}
      {confirming && sw.target === "jwt" && sw.defaults.data && (
        <JwtDialog agentId={agent.id} defaults={sw.defaults.data} sw={sw} onClose={close} />
      )}
    </Panel>
  );
}
