import { useState } from "react";
import { useTranslation } from "react-i18next";

import { splitScopes } from "../../../lib/agent-spec";
import {
  type ActingMode,
  api,
  type ConnectionInfo,
  type CreateIdentityGatewayTargetInput,
  errorMessage,
  type IdentityGatewayTarget,
} from "../../../lib/api";
import { targetModes } from "../../../lib/obo";
import { useLoad } from "../../hooks";
import { Btn, Chip, Dialog, Filters, Notice } from "../../ui";

const TARGET_NAME_RE = /^([0-9a-zA-Z][-]?){1,100}$/;

/** Create a Gateway target — V2's CreateTarget on V3: same validation and request. */
export function CreateTarget({ onClose, onCreated }: { onClose: () => void; onCreated: (created: IdentityGatewayTarget) => void }) {
  const { t } = useTranslation();
  const conns = useLoad(() => api.listConnections(), "v3-connections-for-target");
  const [source, setSource] = useState<"openapi" | "mcp">("openapi");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [schema, setSchema] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [connection, setConnection] = useState("");
  const [scopes, setScopes] = useState("");
  const [location, setLocation] = useState<"HEADER" | "QUERY_PARAMETER">("HEADER");
  const [parameter, setParameter] = useState("Authorization");
  const [prefix, setPrefix] = useState("");
  const [mode, setMode] = useState<ActingMode>("as_agent");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // an MCP server target takes OAuth only
  const usable = (conns.data?.connections ?? []).filter(
    (c: ConnectionInfo) => c.status === "ready" && (source === "openapi" || c.kind === "oauth2"),
  );
  const picked = usable.find((c) => `${c.kind}:${c.name}` === connection) ?? null;
  const modes = targetModes(picked?.kind);
  const actingMode: ActingMode = modes.includes(mode) ? mode : "as_agent";

  const problems: Record<string, string> = {};
  if (!TARGET_NAME_RE.test(name)) problems.name = t("v2.connections.targets.errName");
  if (source === "openapi" && !schema.trim()) problems.schema = t("v2.connections.errRequired");
  if (source === "mcp" && !/^https:\/\//.test(endpoint.trim())) problems.endpoint = t("v2.connections.targets.errEndpoint");
  if (!picked) problems.connection = t("v2.connections.targets.errConnection");
  const err = (key: string) => (touched ? problems[key] : undefined);

  const submit = async () => {
    setTouched(true);
    if (Object.keys(problems).length || !picked) return;
    setBusy(true);
    setError(null);
    const input: CreateIdentityGatewayTargetInput = {
      name,
      description: description.trim() || undefined,
      source,
      ...(source === "openapi" ? { openapi_schema: schema } : { mcp_endpoint: endpoint.trim() }),
      connection: picked.name,
      kind: picked.kind,
      mode: actingMode,
      scopes: picked.kind === "oauth2" ? splitScopes(scopes) : [],
      ...(picked.kind === "api_key"
        ? { api_key: { location, parameter_name: parameter.trim() || "Authorization", prefix: prefix.trim() || undefined } }
        : {}),
    };
    try {
      onCreated(await api.createIdentityGatewayTarget(input));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const fieldErr = (key: string, hint?: string) =>
    err(key) ? <small className="v3-err">{err(key)}</small> : hint ? <small className="v3-hint">{hint}</small> : null;

  return (
    <Dialog
      wide
      title={t("v2.connections.targets.createTitle")}
      onClose={onClose}
      foot={
        <>
          <Btn kind="ghost" onClick={onClose}>{t("v3.common.cancel")}</Btn>
          <Btn kind="primary" disabled={busy} onClick={() => void submit()}>{t("v2.connections.create")}</Btn>
        </>
      }
    >
      <div style={{ display: "grid", gap: 14, maxHeight: "62vh", overflowY: "auto", paddingRight: 4 }}>
        <div className="v3-field">
          <span>{t("v2.connections.targets.colSource")}</span>
          <Filters<"openapi" | "mcp">
            value={source}
            onChange={(next) => {
              setSource(next);
              setConnection("");
            }}
            options={(["openapi", "mcp"] as const).map((value) => ({ value, label: t(`v2.connections.targets.source.${value}`) }))}
          />
        </div>
        <label className="v3-field">
          <span>{t("v2.connections.targets.colName")} *</span>
          <input className="v3-input mono" value={name} aria-invalid={!!err("name")} onChange={(e) => setName(e.target.value)} />
          {fieldErr("name", t("v2.connections.targets.nameHint"))}
        </label>
        <label className="v3-field">
          <span>{t("v2.connections.field.description")}</span>
          <input className="v3-input" value={description} maxLength={200} onChange={(e) => setDescription(e.target.value)} />
        </label>
        {source === "openapi" ? (
          <label className="v3-field">
            <span>{t("v2.connections.targets.schema")} *</span>
            <textarea className="v3-input mono" rows={8} value={schema} aria-invalid={!!err("schema")} onChange={(e) => setSchema(e.target.value)} />
            {fieldErr("schema", t("v2.connections.targets.schemaHint"))}
          </label>
        ) : (
          <label className="v3-field">
            <span>{t("v2.connections.targets.endpoint")} *</span>
            <input className="v3-input mono" value={endpoint} placeholder="https://mcp.example.com/mcp" aria-invalid={!!err("endpoint")}
              onChange={(e) => setEndpoint(e.target.value)} />
            {fieldErr("endpoint")}
          </label>
        )}
        <label className="v3-field">
          <span>{t("v2.connections.targets.colConnection")} *</span>
          <select className="v3-select" value={connection} aria-invalid={!!err("connection")} onChange={(e) => setConnection(e.target.value)}>
            <option value="">{t("v2.common.choose")}</option>
            {usable.map((c) => (
              <option key={`${c.kind}:${c.name}`} value={`${c.kind}:${c.name}`}>{c.name} · {t(`identity.kind.${c.kind}`)}</option>
            ))}
          </select>
          {fieldErr("connection", source === "mcp" ? t("v2.connections.targets.mcpOauthOnly") : undefined)}
        </label>
        <div className="v3-field">
          <span>{t("v2.connections.targets.colMode")}</span>
          {modes.length > 1 ? (
            <Filters<ActingMode> value={actingMode} onChange={setMode}
              options={modes.map((value) => ({ value, label: t(`identity.mode.${value}`) }))} />
          ) : (
            <span style={{ textTransform: "none", letterSpacing: 0 }}><Chip s="info">{t("identity.mode.as_agent")}</Chip></span>
          )}
          <small className="v3-hint">{t(`v2.connections.targets.modeHints.${actingMode}`)}</small>
        </div>
        {actingMode === "obo" && <Notice>{t("v2.connections.targets.oboNeeds")}</Notice>}
        {picked?.kind === "oauth2" && (
          <label className="v3-field">
            <span>{t("v2.connections.field.scopes")}</span>
            <input className="v3-input mono" value={scopes} onChange={(e) => setScopes(e.target.value)} />
            <small className="v3-hint">{t("v2.connections.scopesHint")}</small>
          </label>
        )}
        {picked?.kind === "api_key" && (
          <div className="v3-grid c3">
            <label className="v3-field">
              <span>{t("identity.tools.keyIn")}</span>
              <select className="v3-select" value={location} onChange={(e) => setLocation(e.target.value as typeof location)}>
                <option value="HEADER">{t("identity.tools.keyInHeader")}</option>
                <option value="QUERY_PARAMETER">{t("identity.tools.keyInQuery")}</option>
              </select>
            </label>
            <label className="v3-field">
              <span>{t("identity.tools.keyName")}</span>
              <input className="v3-input mono" value={parameter} onChange={(e) => setParameter(e.target.value)} />
            </label>
            <label className="v3-field">
              <span>{t("v2.connections.targets.prefix")}</span>
              <input className="v3-input mono" value={prefix} placeholder="Bearer" onChange={(e) => setPrefix(e.target.value)} />
            </label>
          </div>
        )}
        {error && <Notice s="act">{error}</Notice>}
      </div>
    </Dialog>
  );
}
