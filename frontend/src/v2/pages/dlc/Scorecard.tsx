/**
 * The agent scorecard — §7.9: one agent, five dimensions, standard vs current.
 *
 * This is the page a business owner reads, so it answers their three questions and
 * nothing else: are we meeting the standard we signed, what is the standard's own
 * debt (uncalibrated judges, thin coverage, live waivers), and is anything moving.
 * Dimensions nobody wrote a criterion for say "not applicable" rather than showing
 * a reassuring blank.
 */
import { useTranslation } from "react-i18next";

import { dlcApi } from "../../../lib/api";
import { type Scorecard as ScorecardData, gateTone } from "../../../lib/dlc";
import { useLoad } from "../../hooks";
import { Alert, Card, Descriptions, Kpi, Spin, Table, Tag } from "../../ui";
import { CoverageMatrix, DimensionTiles } from "./charts";

export function Scorecard({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const card = useLoad<ScorecardData>(() => dlcApi.scorecard(agentId), `scorecard:${agentId}`);
  const data = card.data;
  if (card.loading && !data) return <Spin />;
  if (!data) return <Alert tone="error">{card.error ?? t("v2.dlc.scorecard.notFound")}</Alert>;

  return (
    <>
      <Card
        title={t("v2.dlc.scorecard.title", { name: data.agent_name })}
        sub={
          data.criteria_set
            ? t("v2.dlc.scorecard.against", {
                version: data.criteria_set.version,
                who: data.criteria_set.signed_by ?? t("v2.dlc.criteria.unsigned"),
              })
            : t("v2.dlc.scorecard.noCriteria")
        }
        end={data.last_gate ? <Tag tone={gateTone(data.last_gate)}>{data.last_gate}</Tag> : undefined}
      >
        <DimensionTiles dimensions={data.dimensions} />
        <Descriptions
          items={[
            { label: t("v2.dlc.release.liveVersion"), value: data.release.live_version ?? data.release.ledger_version ?? "—" },
            { label: t("v2.dlc.release.endpointModeLabel"), value: t(`v2.dlc.release.endpointMode.${data.release.endpoint_mode}`) },
            { label: t("v2.dlc.scorecard.lastRun"), value: data.last_run?.at?.slice(0, 16).replace("T", " ") ?? "—" },
            {
              label: t("v2.dlc.scorecard.lastGate"),
              value: data.last_gate
                ? `${data.last_gate} · ${t(`v2.dlc.release.decision.${data.last_gate_decision ?? "open"}`)}${
                    data.last_gate_at ? ` · ${data.last_gate_at.slice(0, 16).replace("T", " ")}` : ""
                  }`
                : t("v2.dlc.scorecard.neverGated"),
            },
            {
              label: t("v2.dlc.scorecard.alerts"),
              value: data.alerts.quiet ? t("v2.dlc.watch.noneFiring") : String(data.alerts.firing),
            },
          ]}
        />
      </Card>

      <Card title={t("v2.dlc.scorecard.debt")} sub={t("v2.dlc.scorecard.debtSub")}>
        <div className="v2-dlc-kpis">
          <Kpi
            label={t("v2.dlc.scorecard.uncalibrated")}
            value={String(data.calibration_debt.length)}
            tone={data.calibration_debt.length > 0 ? "bad" : undefined}
            sub={t("v2.dlc.scorecard.uncalibratedSub")}
          />
          <Kpi
            label={t("v2.dlc.scorecard.openWaivers")}
            value={String(data.open_waivers.length)}
            tone={data.open_waivers.length > 0 ? "bad" : undefined}
          />
          <Kpi
            label={t("v2.dlc.scorecard.thinCriteria")}
            value={String((data.coverage?.criteria ?? []).filter((c) => c.thin).length)}
            sub={t("v2.dlc.scorecard.thinSub")}
          />
        </div>
        {data.calibration_debt.length > 0 && (
          <Table
            rows={data.calibration_debt}
            rowKey={(row) => row.criterion_key}
            columns={[
              { key: "key", title: t("v2.dlc.criteria.key"), render: (row) => <span className="mono">{row.criterion_key}</span> },
              {
                key: "reason",
                title: t("v2.dlc.scorecard.reason"),
                render: (row) =>
                  t(`v2.dlc.calibration.reason.${row.reason ?? "never_calibrated"}`, {
                    defaultValue: row.reason ?? "",
                  }),
              },
            ]}
          />
        )}
        {data.open_waivers.length > 0 && (
          <Table
            rows={data.open_waivers}
            rowKey={(row) => row.id}
            columns={[
              { key: "key", title: t("v2.dlc.criteria.key"), render: (row) => <span className="mono">{row.criterion_key}</span> },
              { key: "owner", title: t("v2.dlc.waiver.riskOwner"), render: (row) => row.risk_owner },
              { key: "expires", title: t("v2.dlc.waiver.expires"), render: (row) => row.expires_on?.slice(0, 10) ?? "—" },
            ]}
          />
        )}
      </Card>

      {data.coverage && (
        <Card title={t("v2.dlc.golden.coverage")} sub={t("v2.dlc.golden.coverageSub")}>
          <CoverageMatrix rows={data.coverage.criteria} />
        </Card>
      )}
    </>
  );
}
