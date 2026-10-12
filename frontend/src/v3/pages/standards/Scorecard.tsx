/**
 * The agent scorecard: are we meeting the standard we signed, what is the standard's
 * own debt, and is anything moving. Led by whatever needs a person.
 */
import { useTranslation } from "react-i18next";

import { dlcApi } from "../../../lib/api";
import type { Scorecard as ScorecardData } from "../../../lib/dlc";
import { useLoad } from "../../hooks";
import { Chip, Notice, Panel, Skeleton, Stat } from "../../ui";
import { CoverageMatrix, DimensionTiles } from "./charts";
import { attentionOf, gateSignal, stamp } from "./common";

export function Scorecard({ agentId, onGo }: { agentId: string; onGo: (view: string) => void }) {
  const { t } = useTranslation();
  const card = useLoad<ScorecardData>(() => dlcApi.scorecard(agentId), `v3-std-card:${agentId}`);
  const data = card.data;
  if (card.loading && !data) return <Skeleton rows={6} />;
  if (!data) return <Notice s="act">{card.error ?? t("v2.dlc.scorecard.notFound")}</Notice>;
  const attention = attentionOf(data, t);
  const thin = (data.coverage?.criteria ?? []).filter((c) => c.thin).length;

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <Panel title={t("v3.standards.attention")} signal={attention.some((a) => a.s === "act") ? "act" : attention.length ? "wait" : "ok"}
        end={<span className="mono">{attention.length}</span>}>
        {attention.length === 0 ? (
          <p className="v3-std-muted" style={{ margin: 0 }}>{t("v3.standards.allClear")}</p>
        ) : (
          <ul className="v3-std-att">
            {attention.map((a) => (
              <li key={a.key}>
                <Chip s={a.s}>{t(`v3.standards.sev.${a.s}`)}</Chip>
                <span>{a.text}</span>
                <button type="button" className="v3-btn sm ghost" onClick={() => onGo(a.view)}>{t(`v2.dlc.view.${a.view}`)}</button>
              </li>
            ))}
          </ul>
        )}
      </Panel>

      <div className="v3-grid c4">
        <Panel signal={data.criteria_set?.signed_by ? "ok" : "wait"}>
          <Stat label={t("v3.standards.standard")} value={data.criteria_set ? `v${data.criteria_set.version}` : "—"}
            foot={data.criteria_set ? data.criteria_set.signed_by ?? t("v2.dlc.criteria.unsigned") : t("v2.dlc.scorecard.noCriteria")} />
        </Panel>
        <Panel signal={data.last_gate ? gateSignal(data.last_gate) : undefined}>
          <Stat label={t("v2.dlc.scorecard.lastGate")} value={data.last_gate ?? "—"}
            foot={data.last_gate
              ? `${t(`v2.dlc.release.decision.${data.last_gate_decision ?? "open"}`)} · ${stamp(data.last_gate_at)}`
              : t("v2.dlc.scorecard.neverGated")} />
        </Panel>
        <Panel signal={data.calibration_debt.length ? "wait" : undefined}>
          <Stat label={t("v2.dlc.scorecard.uncalibrated")} value={data.calibration_debt.length} foot={t("v2.dlc.scorecard.uncalibratedSub")} />
        </Panel>
        <Panel signal={data.alerts.quiet ? "ok" : "act"}>
          <Stat label={t("v2.dlc.scorecard.alerts")} value={data.alerts.quiet ? 0 : data.alerts.firing}
            foot={data.alerts.quiet ? t("v2.dlc.watch.noneFiring") : undefined} />
        </Panel>
      </div>

      <DimensionTiles dimensions={data.dimensions} />

      <div className="v3-grid c2" style={{ alignItems: "start" }}>
        <Panel title={t("v3.standards.release")}>
          <dl className="v3-kv">
            <dt>{t("v2.dlc.release.liveVersion")}</dt>
            <dd className="mono">{data.release.live_version ?? data.release.ledger_version ?? "—"}</dd>
            <dt>{t("v2.dlc.release.endpointModeLabel")}</dt>
            <dd>{t(`v2.dlc.release.endpointMode.${data.release.endpoint_mode}`)}</dd>
            <dt>{t("v2.dlc.scorecard.lastRun")}</dt>
            <dd>{stamp(data.last_run?.at)}</dd>
          </dl>
        </Panel>
        <Panel title={t("v2.dlc.scorecard.debt")} flush={data.calibration_debt.length + data.open_waivers.length > 0}>
          {data.calibration_debt.length + data.open_waivers.length === 0 ? (
            <p className="v3-std-muted" style={{ margin: 0 }}>{t("v3.standards.noDebt", { thin })}</p>
          ) : (
            <table className="v3-table">
              <tbody>
                {data.calibration_debt.map((row) => (
                  <tr key={`c:${row.criterion_key}`}>
                    <td className="mono">{row.criterion_key}</td>
                    <td><Chip s="wait">{t("v2.dlc.scorecard.uncalibrated")}</Chip></td>
                    <td className="v3-std-muted">{t(`v2.dlc.calibration.reason.${row.reason ?? "never_calibrated"}`, { defaultValue: row.reason ?? "" })}</td>
                  </tr>
                ))}
                {data.open_waivers.map((row) => (
                  <tr key={`w:${row.id}`}>
                    <td className="mono">{row.criterion_key}</td>
                    <td><Chip s="info">{t("v2.dlc.scorecard.openWaivers")}</Chip></td>
                    <td className="v3-std-muted">{row.risk_owner} · {row.expires_on?.slice(0, 10) ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      </div>

      {data.coverage && (
        <Panel title={t("v2.dlc.golden.coverage")} flush>
          <CoverageMatrix rows={data.coverage.criteria} />
        </Panel>
      )}
    </div>
  );
}
