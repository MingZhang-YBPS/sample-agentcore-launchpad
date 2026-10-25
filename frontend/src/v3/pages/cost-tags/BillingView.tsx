import "./costTags.css";

import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { api } from "../../../lib/api";
import { type BillingGranularity, costTagsApi, type ServiceCost } from "../../../lib/api/costTags";
import { useWorkspace } from "../../../workspace/workspace-context";
import { ago } from "../../format";
import { useLoad } from "../../hooks";
import { Btn, Chip, Empty, Filters, Notice, Panel, Skeleton, Stat } from "../../ui";
import { gap, pointTotals, usd } from "./model";

const services = (rows: ServiceCost[]) => rows.slice(0, 3).map((s) => `${s.service} ${usd(s.usd)}`).join(" · ");

function Gap({ billed, estimate }: { billed: number; estimate: number | null | undefined }) {
  const g = gap(billed, estimate);
  if (g === null) return <span style={{ color: "var(--v3-text-4)" }}>—</span>;
  const pct = Math.round(g * 100);
  return <span className="v3-bill-gap mono" data-dir={pct > 0 ? "up" : pct < 0 ? "down" : undefined}>{pct > 0 ? "+" : ""}{pct}%</span>;
}

/**
 * The costs page's billing basis (cost-tags design §1.4-2): what AWS billed in this
 * workspace's region per `launchpad:project` and service, daily over 30 days or
 * monthly over 6 months, next to the token × price estimate of the same 30 days. The
 * shared infrastructure row is allocated by invocations and marked so; the untagged
 * row is nobody's. Cost Explorer is read at most every 6 hours.
 */
export function BillingView() {
  const { t } = useTranslation();
  const { current } = useWorkspace();
  const ws = current?.id ?? "";
  const [granularity, setGranularity] = useState<BillingGranularity>("daily");
  const [tick, setTick] = useState(0);
  const report = useLoad(() => costTagsApi.billing(granularity, tick > 0), `v3-billing:${ws}:${granularity}:${tick}`);
  // the estimate over the same window as the daily view, for the side-by-side column
  const estimate = useLoad(() => api.costs("30d", false).catch(() => null), `v3-billing-est:${ws}`);
  const data = report.data;
  const est = new Map((estimate.data?.by_project ?? []).map((r) => [r.project_id ?? "", r.est_cost_usd]));
  const daily = granularity === "daily";
  const totals = data ? pointTotals(data) : [];
  const peak = Math.max(...totals, 0) || 1;
  const max = Math.max(...(data?.by_project ?? []).map((r) => r.total_usd), data?.shared.usd ?? 0, data?.untagged?.usd ?? 0, 0) || 1;

  return (
    <div style={{ display: "grid", gap: 16 }} data-testid="v3-costs-billing">
      {data && data.project_tag_active === false && (
        <Notice s="wait">
          {t("v3.costTags.billing.inactive")}{" "}
          <Link to="/v3/cost-tags">{t("v3.costTags.billing.activateLink")}</Link>
        </Notice>
      )}
      <Notice s="info">{t("v3.costTags.billing.basisDiff")}</Notice>

      <div className="v3-grid c4">
        <Panel signal="info"><Stat label={t("v3.costTags.billing.total")} value={data ? usd(data.total_usd) : "—"}
          foot={data ? t("v3.costTags.billing.window", { start: data.start, end: data.end }) : undefined} /></Panel>
        <Panel><Stat label={t("v3.costTags.billing.estimate30")} value={estimate.data ? usd(estimate.data.total_est_cost_usd) : "—"}
          foot={t("v3.costTags.billing.estimateFoot")} /></Panel>
        <Panel><Stat label={t("v3.costTags.billing.shared")} value={data ? usd(data.shared.usd) : "—"}
          foot={data?.shared.allocated ? t("v3.costTags.billing.sharedAllocated") : t("v3.costTags.billing.sharedUnallocated")} /></Panel>
        <Panel><Stat label={t("v3.costTags.billing.untagged")} value={data?.untagged ? usd(data.untagged.usd) : "—"}
          foot={t("v3.costTags.billing.untaggedFoot")} /></Panel>
      </div>

      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <Filters value={granularity} onChange={setGranularity}
          options={(["daily", "monthly"] as const).map((value) => ({ value, label: t(`v3.costTags.billing.${value}`) }))} />
        <Btn size="sm" kind="ghost" disabled={report.loading} onClick={() => setTick((n) => n + 1)}>
          <RefreshCw size={13} /> {t("v3.costTags.billing.refresh")}
        </Btn>
        {data?.estimated && <Chip s="wait">{t("v3.costTags.billing.provisional")}</Chip>}
        {data && (
          <span style={{ marginLeft: "auto", color: "var(--v3-text-3)", fontSize: 12 }}>
            {t(data.cache?.hit ? "v3.costTags.billing.cached" : "v3.costTags.billing.fresh", { when: ago(data.generated_at), region: data.region })}
          </span>
        )}
      </div>

      {report.loading && !data ? (
        <Panel><Skeleton rows={5} /></Panel>
      ) : !data ? (
        <Notice s="act"><ApiErrorDetail error={report.failure ?? report.error} /></Notice>
      ) : (
        <>
          {totals.length > 1 && (
            <Panel title={t(daily ? "v3.costTags.billing.perDay" : "v3.costTags.billing.perMonth")}>
              <div className="v3-bill-strip" role="img" aria-label={t("v3.costTags.billing.stripLabel")}>
                {data.series.map((p, i) => (
                  <i key={p.start} data-est={p.estimated} title={`${p.start} · ${usd(totals[i])}`}
                    style={{ height: `${Math.max(2, (totals[i] / peak) * 100)}%`, animationDelay: `${i * 12}ms` }} />
                ))}
              </div>
              <div className="v3-bill-axis"><span>{data.start}</span><span>{data.series[data.series.length - 1]?.start}</span></div>
            </Panel>
          )}
          <Panel flush title={t("v3.costTags.billing.byProject")}>
            {!data.by_project.length && !data.shared.usd && !data.untagged?.usd ? (
              <Empty title={t("v3.costTags.billing.empty")} />
            ) : (
              <div className="v3-table-scroll">
                <table className="v3-table" data-testid="v3-billing-by-project">
                  <thead>
                    <tr>
                      <th>{t("v3.costs.colProject")}</th>
                      <th className="num">{t("v3.costTags.billing.colInvocations")}</th>
                      {daily && <th className="num">{t("v3.costTags.billing.colEstimate")}</th>}
                      {daily && <th className="num">{t("v3.costTags.billing.colGap")}</th>}
                      <th>{t("v3.costTags.billing.colServices")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.by_project.map((row) => (
                      <tr key={row.project_id} className="v3-bill-row">
                        <td>
                          <div className="v3-cost-bar">
                            <span style={{ display: "inline-flex", gap: 8, alignItems: "baseline", minWidth: 0 }}>
                              <span className="mono">{row.project_id}</span>
                              {row.allocated_shared_usd > 0 && (
                                <span className="v3-bill-alloc" title={t("v3.costTags.billing.allocTitle")}>{usd(row.allocated_shared_usd)} {t("v3.costTags.billing.alloc")}</span>
                              )}
                            </span>
                            <span className="mono">{usd(row.total_usd)}</span>
                            <span className="track"><i style={{ width: `${(row.total_usd / max) * 100}%` }} /></span>
                          </div>
                        </td>
                        <td className="num">{row.invocations.toLocaleString()}</td>
                        {daily && <td className="num mono">{usd(est.get(row.project_id))}</td>}
                        {daily && <td className="num"><Gap billed={row.total_usd} estimate={est.get(row.project_id)} /></td>}
                        <td><div className="v3-bill-services" title={services(row.services)}>{services(row.services)}</div></td>
                      </tr>
                    ))}
                    {data.shared.usd > 0 && (
                      <tr className="v3-bill-row shared">
                        <td>
                          <div className="v3-cost-bar">
                            <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
                              {t("v3.costTags.billing.sharedRow")}
                              <Chip s={data.shared.allocated ? "info" : undefined}>{t(data.shared.allocated ? "v3.costTags.billing.allocated" : "v3.costTags.billing.unallocated")}</Chip>
                            </span>
                            <span className="mono">{usd(data.shared.usd)}</span>
                            <span className="track"><i className="none" style={{ width: `${(data.shared.usd / max) * 100}%` }} /></span>
                          </div>
                        </td>
                        <td className="num">—</td>
                        {daily && <td className="num">—</td>}
                        {daily && <td className="num">—</td>}
                        <td><div className="v3-bill-services">{services(data.shared.services)}</div></td>
                      </tr>
                    )}
                    {data.untagged && data.untagged.usd > 0 && (
                      <tr className="v3-bill-row untagged">
                        <td>
                          <div className="v3-cost-bar">
                            <span>{t("v3.costTags.billing.untaggedRow")}</span>
                            <span className="mono">{usd(data.untagged.usd)}</span>
                            <span className="track"><i className="none" style={{ width: `${(data.untagged.usd / max) * 100}%` }} /></span>
                          </div>
                        </td>
                        <td className="num">—</td>
                        {daily && <td className="num mono">{usd(est.get(""))}</td>}
                        {daily && <td className="num">—</td>}
                        <td><div className="v3-bill-services">{services(data.untagged.services)}</div></td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
          {data.shared.weights_error && <Notice s="wait">{t("v3.costTags.billing.weightsError", { error: data.shared.weights_error })}</Notice>}
        </>
      )}
    </div>
  );
}
