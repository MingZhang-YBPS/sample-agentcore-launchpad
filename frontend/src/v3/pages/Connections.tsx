import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { api, errorMessage, type UserGrantInfo } from "../../lib/api";
import { revokeDisabled, runRevoke } from "../../lib/user-grants";
import { ago } from "../format";
import { useLoad, useToast } from "../hooks";
import { Btn, Chip, Confirm, Empty, Filters, Lamp, Notice, PageHead, Panel, type Signal, Skeleton, Stat } from "../ui";

/** authorized = usable; a forced re-auth or pending consent waits on the person; revoked is off. */
function grantSignal(g: Pick<UserGrantInfo, "status" | "force_reauth">): Signal {
  if (g.status === "authorized") return g.force_reauth ? "wait" : "ok";
  if (g.status === "pending") return "wait";
  return "off";
}

/**
 * My connections — the signed-in user's own as_user (3LO) grants: which agent
 * may call which Connection as them. Revoking forces fresh consent on that
 * Connection's next call for every agent (AgentCore Identity has no revoke API;
 * the platform sends forceAuthentication until the user re-authorizes). Consent
 * itself happens where the agent asks for it, in chat.
 */
export function V3Connections() {
  const { t } = useTranslation();
  const toast = useToast();
  const { can } = useAuth();
  const mayRevoke = can("identity.grant");
  const grants = useLoad(() => api.listMyGrants(), "v3-my-grants");
  const [state, setState] = useState<"all" | Signal>("all");
  const [confirm, setConfirm] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);
  const all = useMemo(() => grants.data?.grants ?? [], [grants.data]);
  const rows = all.filter((g) => state === "all" || grantSignal(g) === state);
  const count = (s: Signal) => all.filter((g) => grantSignal(g) === s).length;
  const connections = new Set(all.map((g) => g.connection)).size;
  const affected = confirm ? all.filter((g) => g.connection === confirm).length : 0;

  const revoke = (connection: string) =>
    runRevoke(connection, {
      revoke: api.revokeUserToken,
      setBusy: setRevoking,
      onDone: (provider) => {
        toast("ok", t("v2.myConnections.revoked", { connection: provider }));
        setConfirm(null);
        grants.reload();
      },
      onError: (err) => toast("act", errorMessage(err)),
    });

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead eyebrow={t("v3.connections.eyebrow")} title={t("v3.connections.title")} sub={t("v3.connections.sub")} />
      {!mayRevoke && <Notice>{t("v2.myConnections.noPermission")}</Notice>}
      <div className="v3-grid c4">
        <Panel><Stat label={t("v3.connections.connections")} value={grants.data ? connections : "—"} foot={t("v3.connections.connectionsFoot")} /></Panel>
        <Panel signal={count("ok") ? "ok" : undefined}><Stat label={t("v3.connections.authorized")} value={grants.data ? count("ok") : "—"} foot={t("v3.connections.authorizedFoot")} /></Panel>
        <Panel signal={count("wait") ? "wait" : undefined}>
          <Stat label={t("v3.connections.needConsent")} value={grants.data ? count("wait") : "—"} signal={count("wait") ? "wait" : undefined} foot={t("v3.connections.needConsentFoot")} />
        </Panel>
        <Panel><Stat label={t("v3.connections.revoked")} value={grants.data ? count("off") : "—"} foot={t("v3.connections.revokedFoot")} /></Panel>
      </div>
      <Filters
        value={state}
        onChange={setState}
        options={[
          { value: "all", label: t("v3.connections.all"), count: all.length },
          { value: "ok", label: t("v2.myConnections.status.authorized"), s: "ok", count: count("ok") },
          { value: "wait", label: t("v3.connections.needConsent"), s: "wait", count: count("wait") },
          { value: "off", label: t("v2.myConnections.status.revoked"), s: "off", count: count("off") },
        ]}
      />
      <Panel flush>
        {grants.loading && !grants.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
        ) : grants.error ? (
          <div style={{ padding: 20 }}><Notice s="act">{grants.error}</Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={all.length ? t("v3.connections.none") : t("v2.myConnections.empty")}>{!all.length && t("v3.connections.emptySub")}</Empty>
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th /><th>{t("v2.myConnections.colConnection")}</th><th>{t("v2.myConnections.colAgent")}</th>
                <th>{t("v2.myConnections.colScopes")}</th><th>{t("v2.myConnections.colStatus")}</th>
                <th className="num">{t("v2.myConnections.colAuthorized")}</th><th />
              </tr>
            </thead>
            <tbody>
              {rows.map((g) => {
                const s = grantSignal(g);
                return (
                  <tr key={`${g.connection}/${g.agent_id}`}>
                    <td style={{ width: 30 }}><Lamp s={s} live={s === "ok"} /></td>
                    <td><div className="v3-name"><div><b className="mono">{g.connection}</b><small className="mono">{g.tool || "—"}</small></div></div></td>
                    <td>{g.agent_id ? <Link to={`/v3/agents?id=${encodeURIComponent(g.agent_id)}`} style={{ color: "var(--v3-info)" }}>{g.agent_name ?? g.agent_id}</Link> : "—"}</td>
                    <td><span style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>{g.scopes.length ? g.scopes.map((sc) => <Chip key={sc}>{sc}</Chip>) : "—"}</span></td>
                    <td>
                      <span style={{ display: "inline-flex", gap: 6 }}>
                        <Chip s={s === "off" ? undefined : s}>{t(`v2.myConnections.status.${g.status}`)}</Chip>
                        {g.force_reauth && g.status !== "revoked" && <Chip s="wait">{t("v2.myConnections.forceReauth")}</Chip>}
                      </span>
                    </td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(g.authorized_at)}</td>
                    <td style={{ width: 1, whiteSpace: "nowrap" }}>
                      <Btn size="sm" kind="ghost" disabled={revokeDisabled(g, mayRevoke, revoking)}
                        title={mayRevoke ? undefined : t("v2.myConnections.noPermission")} onClick={() => setConfirm(g.connection)}>
                        {t("v2.myConnections.revoke")}
                      </Btn>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>
      {confirm && (
        <Confirm
          title={t("v2.myConnections.confirmTitle", { connection: confirm })}
          confirmLabel={revoking ? t("v2.myConnections.revoking") : t("v2.myConnections.revoke")}
          cancelLabel={t("v3.common.cancel")}
          danger
          busy={revoking !== null}
          onCancel={() => revoking === null && setConfirm(null)}
          onConfirm={() => void revoke(confirm)}
        >
          {t("v2.myConnections.confirmBody", { connection: confirm, count: affected })}
        </Confirm>
      )}
    </div>
  );
}
