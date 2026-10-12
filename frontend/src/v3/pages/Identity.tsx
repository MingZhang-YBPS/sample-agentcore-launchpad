import "./identity.css";

import { Copy, Plus, RefreshCw, ShieldCheck, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { api, type ConnectionInfo, errorMessage, type IdentityGatewayTarget, type TargetWarning } from "../../lib/api";
import { ago } from "../format";
import { useLoad, useToast } from "../hooks";
import { Btn, Chip, Confirm, Dialog, Empty, Filters, Lamp, Notice, PageHead, Panel, type Signal, Skeleton, Stat } from "../ui";
import { CreateConnection } from "./identity/CreateConnection";
import { CreateTarget } from "./identity/CreateTarget";

type Tab = "connections" | "targets";

function connectionSignal(c: ConnectionInfo): Signal {
  if (c.status === "missing") return "act";
  return c.source === "external" ? "info" : "ok";
}

const TARGET_SIGNAL: Record<string, Signal> = { READY: "ok", CREATING: "wait", UPDATING: "wait", FAILED: "act" };

function KindChip({ kind }: { kind: string }) {
  const { t } = useTranslation();
  return <Chip s={kind === "api_key" ? "wait" : "info"}>{t(`identity.kind.${kind}`, kind)}</Chip>;
}

/* ── connections ─────────────────────────────────────────────────────────── */

function CallbackDialog({ connection, onClose }: { connection: ConnectionInfo; onClose: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const url = connection.callback_url ?? "";
  return (
    <Dialog title={t("v2.connections.callbackTitle", { name: connection.name })} onClose={onClose}
      foot={<Btn kind="primary" onClick={onClose}>{t("v2.connections.callbackDone")}</Btn>}>
      <div style={{ display: "grid", gap: 12 }}>
        <Notice>{t("v2.connections.callbackBody")}</Notice>
        {url ? (
          <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
            <pre className="v3-pre" style={{ flex: 1, color: "var(--v3-text)" }}>{url}</pre>
            <Btn size="sm" title={t("v2.connections.copy")}
              onClick={() => void navigator.clipboard?.writeText(url).then(() => toast("ok", t("v2.connections.copied"))).catch(() => undefined)}>
              <Copy size={13} />
            </Btn>
          </div>
        ) : (
          <Notice s="wait">{t("v2.connections.callbackMissing")}</Notice>
        )}
        <p style={{ margin: 0, color: "var(--v3-text-3)", fontSize: 13 }}>{t("v2.connections.callbackM2m")}</p>
      </div>
    </Dialog>
  );
}

function Connections() {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useToast();
  const manage = can("identity.manage");
  const list = useLoad(() => api.listConnections(), "v3-connections-admin");
  const [creating, setCreating] = useState(false);
  const [callback, setCallback] = useState<ConnectionInfo | null>(null);
  const [removing, setRemoving] = useState<ConnectionInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const rows = list.data?.connections ?? [];
  const missing = rows.filter((c) => c.status === "missing");

  const showCallback = async (row: ConnectionInfo) => {
    try {
      // re-read: the list's value may be the create-time snapshot
      setCallback(await api.getConnection(row.kind, row.name));
    } catch (err) {
      toast("act", errorMessage(err));
    }
  };
  const remove = async () => {
    if (!removing) return;
    setBusy(true);
    try {
      await api.deleteConnection(removing.kind, removing.name);
      toast("ok", t("v2.connections.deleted", { name: removing.name }));
      setRemoving(null);
      list.reload();
    } catch (err) {
      toast("act", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="v3-grid c4">
        <Panel><Stat label={t("v3.identity.connections")} value={list.data ? rows.length : "—"} /></Panel>
        <Panel><Stat label={t("identity.kind.oauth2")} value={list.data ? rows.filter((c) => c.kind === "oauth2").length : "—"} /></Panel>
        <Panel><Stat label={t("identity.kind.api_key")} value={list.data ? rows.filter((c) => c.kind === "api_key").length : "—"} /></Panel>
        <Panel signal={missing.length ? "act" : undefined}>
          <Stat label={t("v3.identity.missing")} value={list.data ? missing.length : "—"} signal={missing.length ? "act" : undefined}
            foot={t("v3.identity.missingFoot")} />
        </Panel>
      </div>
      {missing.length > 0 && <Notice s="act">{t("v3.identity.missingNotice", { names: missing.map((c) => c.name).join(", ") })}</Notice>}

      <Panel title={t("v2.connections.listTitle")} flush
        end={
          <span style={{ display: "inline-flex", gap: 8 }}>
            <Btn size="sm" kind="ghost" onClick={list.reload} title={t("v3.identity.refresh")}><RefreshCw size={13} /></Btn>
            {manage && <Btn size="sm" kind="primary" onClick={() => setCreating(true)}><Plus size={13} /> {t("v2.connections.new")}</Btn>}
          </span>
        }>
        <p className="v3-idn-sub">{t("v2.connections.listSub")}</p>
        {list.loading && !list.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
        ) : list.error ? (
          <div style={{ padding: 20 }}><Notice s="act">{list.error}</Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={t("v2.connections.empty")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v2.connections.colName")}</th>
                <th>{t("v2.connections.colKind")}</th>
                <th>{t("v2.connections.colVendor")}</th>
                <th>{t("v2.connections.colSource")}</th>
                <th>{t("v2.connections.colRefs")}</th>
                <th className="num">{t("v2.connections.colCreated")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const s = connectionSignal(row);
                return (
                  <tr key={`${row.kind}:${row.name}`}>
                    <td style={{ width: 30 }}><Lamp s={s} /></td>
                    <td><div className="v3-name"><div><b className="mono">{row.name}</b>{row.description && <small>{row.description}</small>}</div></div></td>
                    <td><KindChip kind={row.kind} /></td>
                    <td style={{ color: "var(--v3-text-2)" }}>{row.template ? t(`v2.connections.template.${row.template}`, row.vendor) : row.vendor || "—"}</td>
                    <td>
                      {row.status === "missing" ? (
                        <Chip s="act" title={t("v2.connections.missingHint")}>{t("v2.connections.status.missing")}</Chip>
                      ) : (
                        <Chip s={row.source === "launchpad" ? "ok" : row.source === "external" ? "info" : undefined}>{t(`v2.connections.source.${row.source}`)}</Chip>
                      )}
                    </td>
                    <td style={{ color: "var(--v3-text-2)" }}
                      title={row.referenced_by.map((r) => `${t(`v2.connections.refType.${r.type}`)}: ${r.name}`).join("\n") || undefined}>
                      {row.referenced_by.length === 0 ? "—" : t("v2.connections.refCount", { count: row.referenced_by.length })}
                    </td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{row.created_at ? ago(row.created_at) : "—"}</td>
                    <td style={{ width: 1, whiteSpace: "nowrap" }}>
                      <span style={{ display: "inline-flex", gap: 4 }}>
                        {row.kind === "oauth2" && row.status === "ready" && (
                          <Btn size="sm" kind="ghost" onClick={() => void showCallback(row)}>{t("v2.connections.callback")}</Btn>
                        )}
                        {/* only Connections Launchpad created: an external vault provider may back
                            another team's runtime, and the backend refuses it (409) */}
                        {manage && !row.system && row.source === "launchpad" && (
                          <Btn size="sm" kind="ghost" onClick={() => setRemoving(row)} title={t("v3.identity.delete")}><Trash2 size={13} /></Btn>
                        )}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>

      {creating && (
        <CreateConnection
          onClose={() => setCreating(false)}
          onCreated={(created) => {
            setCreating(false);
            list.reload();
            if (created.kind === "oauth2") setCallback(created);
            else toast("ok", t("v2.connections.created", { name: created.name }));
          }}
        />
      )}
      {callback && <CallbackDialog connection={callback} onClose={() => setCallback(null)} />}
      {removing && (
        <Confirm title={t("v2.connections.deleteTitle", { name: removing.name })} confirmLabel={t("v3.identity.delete")}
          cancelLabel={t("v3.common.cancel")} danger busy={busy} onCancel={() => setRemoving(null)} onConfirm={() => void remove()}>
          {removing.referenced_by.length ? (
            <Notice s="wait">{t("v2.connections.deleteReferenced", { names: removing.referenced_by.map((r) => r.name).join(", ") })}</Notice>
          ) : (
            t("v2.connections.deleteBody")
          )}
        </Confirm>
      )}
    </>
  );
}

/* ── gateway targets ─────────────────────────────────────────────────────── */

/** Console copy for a create warning; an unknown code keeps the backend's English. */
function warningText(t: (key: string, options?: Record<string, unknown>) => string, warning: TargetWarning): string {
  if (warning.code !== "identity.obo_issuer_mismatch") return warning.message;
  return t("v2.connections.targets.oboIssuerMismatch", {
    connection: warning.detail.connection ?? "",
    connectionIssuer: warning.detail.connection_issuer ?? "",
    gatewayIssuer: warning.detail.gateway_issuer ?? "",
  });
}

function Targets() {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useToast();
  const manage = can("identity.manage");
  const list = useLoad(() => api.listIdentityGatewayTargets(), "v3-identity-targets");
  const [creating, setCreating] = useState(false);
  const [removing, setRemoving] = useState<IdentityGatewayTarget | null>(null);
  const [busy, setBusy] = useState(false);
  // the last create's non-blocking hints, kept until dismissed
  const [warnings, setWarnings] = useState<TargetWarning[]>([]);
  const data = list.data;
  const rows = data?.targets ?? [];
  const noGateway = data !== null && !data.gateway_id;
  const failing = rows.filter((r) => r.status === "FAILED");

  const remove = async () => {
    if (!removing) return;
    setBusy(true);
    try {
      await api.deleteIdentityGatewayTarget(removing.target_id);
      toast("ok", t("v2.connections.targets.deleted", { name: removing.name }));
      setRemoving(null);
      list.reload();
    } catch (err) {
      toast("act", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };
  // tool-level Cedar: the governance editor, prefilled with this target's exact actions
  const policyHref = (row: IdentityGatewayTarget) =>
    data?.gateway_id ? `/v2/governance?${new URLSearchParams({ view: "policy", gateway: data.gateway_id, target: row.name })}` : null;

  return (
    <>
      <div className="v3-grid c3">
        <Panel><Stat label={t("v3.identity.targets")} value={data ? rows.length : "—"} foot={data?.gateway_id ?? undefined} /></Panel>
        <Panel signal={rows.length && !failing.length ? "ok" : undefined}>
          <Stat label={t("v3.identity.ready")} value={data ? rows.filter((r) => r.status === "READY").length : "—"} />
        </Panel>
        <Panel signal={failing.length ? "act" : undefined}>
          <Stat label={t("v3.identity.failed")} value={data ? failing.length : "—"} signal={failing.length ? "act" : undefined} />
        </Panel>
      </div>
      {noGateway && <Notice s="wait">{t("v2.connections.targets.noGateway")}</Notice>}
      {warnings.map((w) => (
        <Notice key={w.code} s="wait">
          {warningText(t, w)}{" "}
          <button type="button" className="v3-btn sm ghost" onClick={() => setWarnings([])}>{t("v3.identity.dismiss")}</button>
        </Notice>
      ))}
      <Panel title={t("v2.connections.targets.title")} flush
        end={
          <span style={{ display: "inline-flex", gap: 8 }}>
            <Btn size="sm" kind="ghost" onClick={list.reload} title={t("v3.identity.refresh")}><RefreshCw size={13} /></Btn>
            {manage && <Btn size="sm" kind="primary" disabled={noGateway} onClick={() => setCreating(true)}><Plus size={13} /> {t("v2.connections.targets.new")}</Btn>}
          </span>
        }>
        <p className="v3-idn-sub">
          {data?.gateway_id ? t("v2.connections.targets.sub", { id: data.gateway_id }) : t("v2.connections.targets.subNone")}
        </p>
        {list.loading && !data ? (
          <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
        ) : list.error ? (
          <div style={{ padding: 20 }}><Notice s="act">{list.error}</Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={t("v2.connections.targets.empty")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v2.connections.targets.colName")}</th>
                <th>{t("v2.connections.targets.colSource")}</th>
                <th>{t("v2.connections.targets.colConnection")}</th>
                <th>{t("v2.connections.targets.colMode")}</th>
                <th>{t("v2.connections.targets.colStatus")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const s = TARGET_SIGNAL[row.status] ?? "off";
                const href = policyHref(row);
                return (
                  <tr key={row.target_id}>
                    <td style={{ width: 30 }}><Lamp s={s} live={s === "wait"} /></td>
                    <td className="mono">{row.name}</td>
                    <td style={{ color: "var(--v3-text-2)" }}>{t(`v2.connections.targets.source.${row.source}`, row.source)}</td>
                    <td>
                      {row.connection ? (
                        <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}><span className="mono">{row.connection}</span><KindChip kind={row.auth} /></span>
                      ) : (
                        <span style={{ color: "var(--v3-text-3)" }}>{t(`v2.connections.targets.auth.${row.auth}`, row.auth)}</span>
                      )}
                    </td>
                    <td>{row.mode ? <Chip s="info">{t(`identity.mode.${row.mode}`, row.mode)}</Chip> : "—"}</td>
                    <td><Chip s={s === "off" ? undefined : s} title={row.status_reasons.join("\n") || undefined}>{row.status || "—"}</Chip></td>
                    <td style={{ width: 1, whiteSpace: "nowrap" }}>
                      <span style={{ display: "inline-flex", gap: 4, alignItems: "center" }}>
                        {href && <Link to={href} className="v3-btn sm ghost"><ShieldCheck size={13} /> {t("v2.connections.targets.policy")}</Link>}
                        {row.system ? (
                          <Chip>{t("v2.connections.source.system")}</Chip>
                        ) : manage && row.connection ? (
                          <Btn size="sm" kind="ghost" onClick={() => setRemoving(row)} title={t("v3.identity.delete")}><Trash2 size={13} /></Btn>
                        ) : null}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>
      {creating && (
        <CreateTarget
          onClose={() => setCreating(false)}
          onCreated={(created) => {
            setCreating(false);
            setWarnings(created.warnings ?? []);
            toast("ok", t("v2.connections.targets.created", { name: created.name }));
            list.reload();
          }}
        />
      )}
      {removing && (
        <Confirm title={t("v2.connections.targets.deleteTitle", { name: removing.name })} confirmLabel={t("v3.identity.delete")}
          cancelLabel={t("v3.common.cancel")} danger busy={busy} onCancel={() => setRemoving(null)} onConfirm={() => void remove()}>
          {t("v2.connections.targets.deleteBody")}
        </Confirm>
      )}
    </>
  );
}

/**
 * Identity connections: AgentCore Identity Connections (credential providers in
 * the workspace token vault) and the Gateway targets bound to one. Reads are open
 * to members; create / delete need `identity.manage`, exactly as V2. `?view=targets`
 * is the targets tab, as in V2.
 */
export function V3Identity() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const tab: Tab = params.get("view") === "targets" ? "targets" : "connections";
  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.identity.eyebrow")}
        title={t("v2.connections.title")}
        sub={t("v2.connections.desc")}
        end={
          <Filters<Tab>
            value={tab}
            onChange={(next) => setParams(next === "targets" ? { view: "targets" } : {})}
            options={(["connections", "targets"] as Tab[]).map((value) => ({ value, label: t(`v2.connections.tab.${value}`) }))}
          />
        }
      />
      {tab === "targets" ? <Targets /> : <Connections />}
    </div>
  );
}
