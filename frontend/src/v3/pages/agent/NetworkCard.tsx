import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { editPath } from "../../../lib/agent-admin/edit";
import type { AgentInfo } from "../../../lib/api";
import { type AgentNetwork, getAgentNetwork } from "../../../lib/api/agent-network";
import { networkApi, resultsOf, telemetryGaps } from "../../../lib/api/networkProfiles";
import { useLoad } from "../../hooks";
import { Chip, Notice, Panel, type Signal, Skeleton } from "../../ui";
import { when } from "./format";

const STATUS_SIGNAL: Record<string, Signal> = { ok: "ok", warn: "wait", blocked: "act", unchecked: "off" };

/**
 * 网络 (projects §12.12): PUBLIC or the VPC profile the agent runs in — its subnets and
 * security groups, the ENIs AgentCore holds there (per profile), the last preflight,
 * and a telemetry hint when the profile has no usable logs / X-Ray endpoint (§12.9).
 * "Change network…" is a re-publish (§12.6), so it opens the edit wizard.
 */
export function NetworkCard({ agent }: { agent: AgentInfo }) {
  const net = useLoad(() => getAgentNetwork(agent.id), `v3-agent-network:${agent.id}`);
  const profileId = net.data?.profile && !net.data.profile.missing ? net.data.profile.id : "";
  // the stored check results (member read, allowed set); a refusal just hides the hint
  const profile = useLoad(
    async () => (profileId ? networkApi.get(profileId).catch(() => null) : null),
    `v3-agent-network-profile:${profileId}`,
  );
  const gaps = profile.data ? telemetryGaps(resultsOf(profile.data)) : [];
  return <NetworkCardView agent={agent} loading={net.loading} error={net.error} data={net.data} gaps={gaps} />;
}

/** The card itself, from what the container read (`gaps`: logs / xray not reachable). */
export function NetworkCardView({
  agent,
  loading,
  error,
  data: loaded,
  gaps,
}: {
  agent: AgentInfo;
  loading: boolean;
  error: string | null;
  data: AgentNetwork | null;
  gaps: string[];
}) {
  const { t } = useTranslation();
  const change = (
    <Link to={editPath(agent)} className="v3-inline-link">{t("v3.network.card.change")}</Link>
  );

  if (loading && !loaded) {
    return <Panel title={t("v3.network.card.title")}><Skeleton rows={2} /></Panel>;
  }
  if (error || !loaded) {
    return <Panel title={t("v3.network.card.title")}><Notice s="act">{error ?? t("v3.network.card.unavailable")}</Notice></Panel>;
  }
  const data = loaded;
  if (data.mode === "PUBLIC") {
    return (
      <Panel title={t("v3.network.card.title")} end={change}>
        <dl className="v3-kv" data-testid="v3-agent-network">
          <dt>{t("v3.network.card.mode")}</dt>
          <dd><Chip>{t("v3.network.card.public")}</Chip></dd>
        </dl>
      </Panel>
    );
  }
  const p = data.profile;
  const enis = data.enis;
  return (
    <Panel title={t("v3.network.card.title")} end={change}>
      <dl className="v3-kv" data-testid="v3-agent-network">
        <dt>{t("v3.network.card.mode")}</dt>
        <dd><Chip s="info">VPC</Chip></dd>
        <dt>{t("v3.network.card.profile")}</dt>
        <dd>
          {p == null ? t("v3.network.card.inline") : p.missing ? (
            <span className="mono">{p.id} · {t("v3.network.card.missing")}</span>
          ) : (
            <>
              <b>{p.name}</b> <small className="mono">{p.id}</small>
              {p.archived && <> <Chip s="off">{t("v3.network.card.archived")}</Chip></>}
            </>
          )}
        </dd>
        {p?.vpc_id && (<><dt>VPC</dt><dd className="mono">{p.vpc_id}</dd></>)}
        <dt>{t("v3.network.col.subnets")}</dt>
        <dd className="mono">{(data.vpc?.subnets ?? []).join(", ") || "—"}</dd>
        <dt>{t("v3.network.form.groups")}</dt>
        <dd className="mono">{(data.vpc?.security_groups ?? []).join(", ") || "—"}</dd>
        {p && !p.missing && (
          <>
            <dt>{t("v3.network.card.preflight")}</dt>
            <dd>
              <Chip s={STATUS_SIGNAL[p.status ?? "unchecked"] ?? "off"}>{t(`v3.network.status.${p.status ?? "unchecked"}`)}</Chip>{" "}
              <small>{p.checked_at ? when(p.checked_at) : t("v3.network.never")}</small>
              {(p.blocking ?? 0) + (p.warnings ?? 0) > 0 && (
                <small> · {t("v3.network.counts", { block: p.blocking ?? 0, warn: p.warnings ?? 0 })}</small>
              )}
            </dd>
          </>
        )}
        <dt>{t("v3.network.card.enis")}</dt>
        <dd>
          {enis == null ? "—" : enis.status === "unavailable" ? (
            <small>{t("v3.network.card.enisUnavailable", { code: enis.error ?? "" })}</small>
          ) : (
            <span title={t("v3.network.card.enisScope")}>
              {t("v3.network.card.enisCount", { count: enis.count ?? 0, inUse: enis.in_use ?? 0 })}
            </span>
          )}
        </dd>
      </dl>
      {gaps.length > 0 && (
        <Notice s="wait">{t("v3.network.card.telemetry", { services: gaps.join(" / ") })}</Notice>
      )}
    </Panel>
  );
}
