import { useTranslation } from "react-i18next";

import {
  type CheckResult,
  costSummary,
  groupResults,
  inboundEndpoints,
  type NetworkProfile,
  probeTargets,
  profileProjects,
  resultsOf,
} from "../../../lib/api/networkProfiles";
import { Chip, Lamp, Notice, Panel } from "../../ui";
import { when } from "../agent/format";
import { SEVERITY_SIGNAL } from "./model";

function Rows({ rows, testId }: { rows: CheckResult[]; testId: string }) {
  return (
    <ul className="v3-net-results" data-testid={testId} style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 6 }}>
      {rows.map((r) => (
        <li key={r.check} style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
          <Lamp s={SEVERITY_SIGNAL[r.severity] ?? "off"} title={r.severity} />
          <span className="mono" style={{ minWidth: 200 }}>{r.check}</span>
          <span>{r.message}</span>
        </li>
      ))}
    </ul>
  );
}

/**
 * A profile's detail (§12.12): basic facts, the preflight check by check, the endpoint
 * requirement matrix, the inbound PrivateLink endpoint (§12.8), the probe (§12.7), the
 * cost hint (§12.9) and who may use it — "shared by several projects" when more than
 * one project's agents land behind the same security groups (§12.3).
 */
export function ProfileDetail({ profile }: { profile: NetworkProfile }) {
  const { t } = useTranslation();
  const results = resultsOf(profile);
  const groups = groupResults(results);
  const cost = costSummary(results);
  const inbound = inboundEndpoints(results);
  const targets = probeTargets(groups.probe);
  const projects = profileProjects(profile);
  return (
    <div style={{ display: "grid", gap: 12 }} data-testid="v3-network-detail">
      <Panel title={t("v3.network.detail.basic")}>
        <dl className="v3-kv">
          <dt>ID</dt><dd className="mono">{profile.id}</dd>
          <dt>VPC</dt><dd className="mono">{profile.vpc_id || "—"}</dd>
          <dt>{t("v3.network.col.subnets")}</dt><dd className="mono">{profile.subnet_ids.join(", ")}</dd>
          <dt>{t("v3.network.form.groups")}</dt><dd className="mono">{profile.security_group_ids.join(", ")}</dd>
          <dt>{t("v3.network.col.purpose")}</dt><dd>{t(`v3.network.purpose.${profile.purpose}`)}</dd>
          <dt>{t("v3.network.form.internet")}</dt><dd>{t(`v3.network.internet.${profile.internet}`)}</dd>
          <dt>{t("v3.network.form.reachable")}</dt>
          <dd className="mono">{profile.reachable.length ? profile.reachable.map((r) => `${r.cidr ?? r.host}:${r.port}`).join(", ") : "—"}</dd>
          <dt>{t("v3.network.detail.checkedAt")}</dt><dd>{profile.checked_at ? when(profile.checked_at) : t("v3.network.never")}</dd>
        </dl>
      </Panel>

      <Panel title={t("v3.network.detail.checks")}>
        {results.length === 0 ? <Notice s="info">{t("v3.network.detail.notChecked")}</Notice> : <Rows rows={groups.checks} testId="v3-network-checks" />}
      </Panel>

      {groups.endpoints.length > 0 && (
        <Panel title={t("v3.network.detail.endpoints")}>
          <table className="v3-table" data-testid="v3-network-endpoints">
            <thead><tr><th>{t("v3.network.detail.endpoint")}</th><th>{t("v3.network.detail.neededBy")}</th><th>{t("v3.network.col.preflight")}</th></tr></thead>
            <tbody>
              {groups.endpoints.map((r) => (
                <tr key={r.check}>
                  <td className="mono">{r.check.slice("endpoint.".length)}</td>
                  <td>{String(r.details?.needed_by ?? "—")}</td>
                  <td><Lamp s={SEVERITY_SIGNAL[r.severity] ?? "off"} /> {r.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}

      {groups.inbound && (
        <Panel title={t("v3.network.detail.inbound")}>
          <p style={{ margin: "0 0 8px" }}>{groups.inbound.message}</p>
          {inbound.map((e) => (
            <div key={e.endpoint_id} style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
              <Chip>{e.service}</Chip>
              <span className="mono">{e.endpoint_id}</span>
              <Chip s={e.private_dns ? "ok" : "wait"}>{t(e.private_dns ? "v3.network.detail.privateDnsOn" : "v3.network.detail.privateDnsOff")}</Chip>
              <small>{e.any_principal ? t("v3.network.detail.anyPrincipal") : e.principals.join(", ")}</small>
            </div>
          ))}
          <Notice s="info">{t("v3.network.detail.oauthNote")}</Notice>
        </Panel>
      )}

      {groups.probe && (
        <Panel title={t("v3.network.detail.probe")}>
          <p style={{ margin: "0 0 8px" }}>
            <Lamp s={groups.probe.status === "running" ? "wait" : SEVERITY_SIGNAL[groups.probe.severity] ?? "off"} live={groups.probe.status === "running"} />{" "}
            {groups.probe.message}
          </p>
          {targets.length > 0 && (
            <table className="v3-table" data-testid="v3-network-probe">
              <thead><tr><th>{t("v3.network.detail.target")}</th><th>DNS</th><th>TCP</th><th>TLS</th></tr></thead>
              <tbody>
                {targets.map((x) => (
                  <tr key={`${x.target}:${x.port}`}>
                    <td className="mono">{x.target}:{x.port}</td>
                    <td>{x.dns ?? "—"}</td><td>{x.tcp ?? "—"}</td><td>{x.tls ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <small className="v3-hint">{t("v3.network.detail.notVerified")}</small>
        </Panel>
      )}

      {cost && (
        <Panel title={t("v3.network.detail.cost")}>
          <p style={{ margin: 0 }} data-testid="v3-network-cost">
            {t("v3.network.detail.costLine", { endpoints: cost.interfaceEndpoints, azs: cost.availabilityZones, hours: cost.endpointAzHours, nat: cost.natGateways })}
          </p>
          <small className="v3-hint">
            {t("v3.network.detail.costHint")}{" "}
            {Object.entries(cost.pricing).map(([k, url]) => (
              <a key={k} href={url} target="_blank" rel="noreferrer" className="v3-inline-link" style={{ marginRight: 8 }}>{t(`v3.network.detail.pricing.${k}`, { defaultValue: k })}</a>
            ))}
          </small>
        </Panel>
      )}

      <Panel title={t("v3.network.detail.projects")}>
        {projects.length === 0 ? (
          <small className="v3-hint">{t("v3.network.detail.noProjects")}</small>
        ) : (
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {projects.map((p) => <Chip key={p.project_id}>{p.project_id}{p.is_default ? ` · ${t("v3.network.access.default")}` : ""}</Chip>)}
          </div>
        )}
        {projects.length > 1 && <Notice s="wait">{t("v3.network.detail.shared", { count: projects.length })}</Notice>}
      </Panel>
    </div>
  );
}
