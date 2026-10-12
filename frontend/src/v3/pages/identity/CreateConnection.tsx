import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  api,
  type ConnectionInfo,
  type ConnectionTemplate,
  type ConnectionTemplateField,
  errorMessage,
} from "../../../lib/api";
import { splitScopes } from "../../../lib/agent-spec";
import { EMPTY_OBO_FORM, oboAvailability, oboFromForm, type OboFormState } from "../../../lib/obo";
import { useLoad } from "../../hooks";
import { Btn, Chip, Dialog, Filters, Notice, Skeleton } from "../../ui";

const NAME_RE = /^[a-zA-Z0-9\-_]{1,128}$/;

type Values = Partial<Record<ConnectionTemplateField | "name" | "description", string>>;

/**
 * Create a Connection — V2's CreateConnection on V3: the same templates,
 * validation, request bodies and on-behalf-of offer. Secrets are typed into
 * password inputs and go straight to the token vault; nothing echoes them.
 */
export function CreateConnection({ onClose, onCreated }: { onClose: () => void; onCreated: (created: ConnectionInfo) => void }) {
  const { t } = useTranslation();
  const templates = useLoad(() => api.connectionTemplates(), "v3-connection-templates");
  const list = useMemo(() => templates.data?.templates ?? [], [templates.data]);
  const [templateId, setTemplateId] = useState("cognito");
  const template: ConnectionTemplate | undefined = list.find((x) => x.id === templateId) ?? list[0];
  const [values, setValues] = useState<Values>({});
  const [obo, setObo] = useState<OboFormState>({ ...EMPTY_OBO_FORM });
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const val = (key: keyof Values) => values[key] ?? "";
  const put = (key: keyof Values, value: string) => setValues((prev) => ({ ...prev, [key]: value }));

  const fields = template?.fields ?? [];
  const oboOffer = oboAvailability(template);
  const required: ConnectionTemplateField[] = fields.filter((f) => f !== "scopes");
  const problems: Partial<Record<keyof Values, string>> = {};
  if (!NAME_RE.test(val("name"))) problems.name = t("v2.connections.errName");
  for (const field of required) if (!val(field).trim()) problems[field] = t("v2.connections.errRequired");
  if (fields.includes("discovery_url") && val("discovery_url") &&
      !/^https:\/\/.+\/\.well-known\/(openid-configuration|oauth-authorization-server)$/.test(val("discovery_url").trim()))
    problems.discovery_url = t("v2.connections.errDiscovery");
  const err = (key: keyof Values) => (touched ? problems[key] : undefined);

  const submit = async () => {
    setTouched(true);
    if (!template || Object.keys(problems).length) return;
    setBusy(true);
    setError(null);
    const opt = (key: keyof Values) => val(key).trim() || undefined;
    try {
      const created =
        template.kind === "api_key"
          ? await api.createApiKeyConnection({ name: val("name"), description: opt("description"), api_key: val("api_key") })
          : await api.createOauth2Connection({
              name: val("name"),
              vendor: template.vendor,
              template: template.id,
              description: opt("description"),
              client_id: val("client_id").trim(),
              client_secret: val("client_secret"),
              discovery_url: opt("discovery_url"),
              issuer: opt("issuer"),
              authorization_endpoint: opt("authorization_endpoint"),
              token_endpoint: opt("token_endpoint"),
              scopes: splitScopes(val("scopes")),
              obo: oboOffer === "offered" ? oboFromForm(obo) : undefined,
            });
      onCreated(created);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const input = (field: ConnectionTemplateField) => {
    const secret = field === "client_secret" || field === "api_key";
    const hint = field === "discovery_url" ? template?.discovery_hint : field === "scopes" ? t("v2.connections.scopesHint") : secret ? t("v2.connections.secretHint") : undefined;
    return (
      <label key={field} className="v3-field">
        <span>{t(`v2.connections.field.${field}`)}{field !== "scopes" ? " *" : ""}</span>
        <input className="v3-input mono" type={secret ? "password" : "text"} autoComplete={secret ? "new-password" : "off"}
          value={val(field)} aria-invalid={!!err(field)} onChange={(e) => put(field, e.target.value)} />
        {err(field) ? <small className="v3-err">{err(field)}</small> : hint ? <small className="v3-hint">{hint}</small> : null}
      </label>
    );
  };
  const setOboPatch = (patch: Partial<OboFormState>) => setObo((prev) => ({ ...prev, ...patch }));

  return (
    <Dialog
      wide
      title={t("v2.connections.createTitle")}
      onClose={onClose}
      foot={
        <>
          <Btn kind="ghost" onClick={onClose}>{t("v3.common.cancel")}</Btn>
          <Btn kind="primary" disabled={busy || !template} onClick={() => void submit()}>{t("v2.connections.create")}</Btn>
        </>
      }
    >
      <div style={{ display: "grid", gap: 14, maxHeight: "62vh", overflowY: "auto", paddingRight: 4 }}>
        <div className="v3-field">
          <span>{t("v2.connections.field.template")}</span>
          {templates.loading && !templates.data ? <Skeleton rows={1} /> : (
            <div className="v3-idn-templates" role="radiogroup">
              {list.map((x) => (
                <button key={x.id} type="button" role="radio" aria-checked={x.id === template?.id}
                  className={x.id === template?.id ? "v3-idn-template on" : "v3-idn-template"} onClick={() => setTemplateId(x.id)}>
                  <b>{t(`v2.connections.template.${x.id}`, x.vendor)}</b>
                  <Chip s={x.kind === "api_key" ? "wait" : "info"}>{t(`identity.kind.${x.kind}`, x.kind)}</Chip>
                </button>
              ))}
            </div>
          )}
        </div>
        <label className="v3-field">
          <span>{t("v2.connections.field.name")} *</span>
          <input className="v3-input mono" value={val("name")} aria-invalid={!!err("name")} onChange={(e) => put("name", e.target.value)} />
          {err("name") ? <small className="v3-err">{err("name")}</small> : <small className="v3-hint">{t("v2.connections.nameHint")}</small>}
        </label>
        <label className="v3-field">
          <span>{t("v2.connections.field.description")}</span>
          <input className="v3-input" value={val("description")} maxLength={200} onChange={(e) => put("description", e.target.value)} />
        </label>
        {fields.map((field) => input(field))}
        {oboOffer === "cognito" && <Notice>{t("v2.connections.obo.cognito")}</Notice>}
        {oboOffer === "offered" && (
          <div className="v3-field">
            <span>{t("v2.connections.obo.title")}</span>
            <label style={{ display: "inline-flex", gap: 8, alignItems: "center", textTransform: "none", letterSpacing: 0, fontFamily: "var(--v3-body)", fontSize: 13, color: "var(--v3-text)" }}>
              <input type="checkbox" checked={obo.enabled} onChange={(e) => setOboPatch({ enabled: e.target.checked })} />
              {t("v2.connections.obo.enable")}
            </label>
            <small className="v3-hint">{t("v2.connections.obo.hint")}</small>
            {obo.enabled && (
              <div style={{ display: "grid", gap: 10, marginTop: 6 }}>
                <Filters<OboFormState["grant_type"]>
                  value={obo.grant_type}
                  onChange={(grant_type) => setOboPatch({ grant_type })}
                  options={[
                    { value: "TOKEN_EXCHANGE", label: t("v2.connections.obo.grantExchange") },
                    { value: "JWT_AUTHORIZATION_GRANT", label: t("v2.connections.obo.grantJwtBearer") },
                  ]}
                />
                {obo.grant_type === "TOKEN_EXCHANGE" && (
                  <>
                    <Filters<OboFormState["actor_token_content"]>
                      value={obo.actor_token_content}
                      onChange={(actor_token_content) => setOboPatch({ actor_token_content })}
                      options={[
                        { value: "NONE", label: t("v2.connections.obo.actorNone") },
                        { value: "M2M", label: t("v2.connections.obo.actorM2m") },
                      ]}
                    />
                    <small className="v3-hint">{t("v2.connections.obo.actorHint")}</small>
                  </>
                )}
                {obo.grant_type === "TOKEN_EXCHANGE" && obo.actor_token_content === "M2M" && (
                  <label className="v3-field">
                    <span>{t("v2.connections.obo.actorScopes")}</span>
                    <input className="v3-input mono" value={obo.actor_token_scopes} onChange={(e) => setOboPatch({ actor_token_scopes: e.target.value })} />
                    <small className="v3-hint">{t("v2.connections.scopesHint")}</small>
                  </label>
                )}
              </div>
            )}
          </div>
        )}
        {error && <Notice s="act">{error}</Notice>}
      </div>
    </Dialog>
  );
}
