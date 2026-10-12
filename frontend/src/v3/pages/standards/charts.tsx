/**
 * Agent-DLC charts on the V3 theme. Same rule as V2's: every chart exists to make a
 * judgment possible, so the interval, the baseline and the denominator are drawn,
 * never hidden behind a single number. Plain SVG, `--v3-*` colours only.
 */
import { useTranslation } from "react-i18next";

import {
  CASE_TIERS,
  type CaseTier,
  type CoverageRow,
  type DimensionPoint,
  type DlcTier,
  type LadderRung,
  type ScorecardDimension,
} from "../../../lib/dlc";
import { Chip } from "../../ui";
import { kappaSignal, rate, tierSignal } from "./common";

/** The criterion's tier, with the demotion made visible when a judge is uncalibrated. */
export function TierChip({ tier, effective }: { tier: DlcTier; effective?: DlcTier }) {
  const { t } = useTranslation();
  const demoted = effective !== undefined && effective !== tier;
  const shown = demoted ? (effective as DlcTier) : tier;
  return (
    <span className="v3-std-tier">
      <Chip s={tierSignal(shown)}>{t(`v2.dlc.tier.${shown}`)}</Chip>
      {demoted && (
        <span className="v3-std-demoted" title={t("v2.dlc.criteria.demotedHint")}>
          {t("v2.dlc.criteria.demotedFrom", { tier: t(`v2.dlc.tier.${tier}`) })}
        </span>
      )}
    </span>
  );
}

/**
 * A rate as a point with its Wilson interval, against the threshold. When the
 * threshold sits inside the interval the bar says `undecided`: the honest reading is
 * "this sample cannot tell", a different decision from "it failed".
 */
export function CiBar({
  value,
  low,
  high,
  threshold,
  undecided,
  n,
}: {
  value: number | null;
  low?: number | null;
  high?: number | null;
  threshold?: number | null;
  undecided?: boolean;
  n?: number | null;
}) {
  const { t } = useTranslation();
  if (value === null || value === undefined) return <span className="v3-std-muted">{t("v2.dlc.noResult")}</span>;
  const x = (v: number) => `${Math.min(100, Math.max(0, v * 100))}%`;
  const hasCi = typeof low === "number" && typeof high === "number";
  const ok = typeof threshold === "number" ? value >= threshold : null;
  return (
    <div className={undecided ? "v3-std-ci undecided" : "v3-std-ci"}>
      <div className="track">
        {hasCi && <div className="band" style={{ left: x(low as number), width: `${Math.max(0.5, ((high as number) - (low as number)) * 100)}%` }} />}
        {typeof threshold === "number" && <div className="thr" style={{ left: x(threshold) }} />}
        <div className={`dot${ok === false ? " bad" : ok ? " good" : ""}`} style={{ left: x(value) }} />
      </div>
      <div className="legend">
        <b>{rate(value)}</b>
        {hasCi && <span>{t("v2.dlc.ci", { low: rate(low, 0), high: rate(high, 0) })}</span>}
        {typeof n === "number" && <span>{t("v2.dlc.nOf", { n })}</span>}
        {undecided && <Chip s="wait">{t("v2.dlc.undecided")}</Chip>}
      </div>
    </div>
  );
}

/** A bare trend line for a table cell or a tile — only the shape matters. */
export function MiniSpark({ values, bad }: { values: (number | null)[]; bad?: boolean }) {
  const points = values.filter((v): v is number => typeof v === "number");
  if (points.length < 2) return <span className="v3-std-muted">—</span>;
  const W = 96;
  const H = 22;
  const min = Math.min(...points);
  const span = Math.max(...points) - min || 1;
  const path = points
    .map((v, i) => `${((i / (points.length - 1)) * W).toFixed(1)},${(H - ((v - min) / span) * (H - 4) - 2).toFixed(1)}`)
    .join(" ");
  return (
    <svg className="v3-std-spark" viewBox={`0 0 ${W} ${H}`} aria-hidden="true">
      <polyline points={path} fill="none" className={bad ? "bad" : undefined} strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

/**
 * A dimension's pass rate over time against its rolling-median baseline. Until the
 * baseline has enough points it is drawn dashed and labelled, so a dip is not read
 * as a regression on three days of history.
 */
export function BandChart({ points, minBaselinePoints = 8 }: { points: DimensionPoint[]; minBaselinePoints?: number }) {
  const { t } = useTranslation();
  if (points.length === 0) return <p className="v3-std-empty">{t("v2.dlc.watch.noSeries")}</p>;
  const W = 640;
  const H = 160;
  const TOP = 10;
  const BASE = H - 16;
  const rates = points.map((p) => p.rate);
  const baselines = points.map((p) => p.baseline).filter((b): b is number => typeof b === "number");
  const lo = Math.min(...rates, ...baselines, 0.5);
  const hi = Math.max(...rates, ...baselines, 1);
  const span = hi - lo || 1;
  const x = (i: number) => (points.length > 1 ? (i / (points.length - 1)) * W : W / 2);
  const y = (v: number) => BASE - ((v - lo) / span) * (BASE - TOP);
  const have = points[points.length - 1]?.baseline_points ?? 0;
  const ready = have >= minBaselinePoints;
  const line = points.map((p, i) => `${x(i).toFixed(1)},${y(p.rate).toFixed(1)}`).join(" ");
  const band = points
    .map((p, i) => (typeof p.baseline === "number" ? `${x(i).toFixed(1)},${y(p.baseline).toFixed(1)}` : null))
    .filter(Boolean)
    .join(" ");
  return (
    <>
      <svg className="v3-std-band" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
        <line x1="0" y1={BASE} x2={W} y2={BASE} className="axis" />
        <line x1="0" y1={TOP} x2={W} y2={TOP} className="grid" />
        {band && <polyline points={band} fill="none" className={ready ? "baseline" : "baseline thin"} strokeWidth="1.5" vectorEffect="non-scaling-stroke" />}
        <polyline points={line} fill="none" className="rate" strokeWidth="2" vectorEffect="non-scaling-stroke" />
        {points.map((p, i) => (
          <circle key={p.run_id} cx={x(i)} cy={y(p.rate)} r="2.5" className="pt" />
        ))}
      </svg>
      <div className="v3-std-axis">
        <span>{rate(lo, 0)}</span>
        {!ready && <span className="warn">{t("v2.dlc.watch.baselineThin", { have, need: minBaselinePoints })}</span>}
        <span>{rate(hi, 0)}</span>
      </div>
    </>
  );
}

/** Judge vs human, 2×2 — where the judge is wrong matters more than how often. */
export function ConfusionMatrix({
  confusion,
  kappa,
  ci,
}: {
  confusion: Record<string, number>;
  kappa: number | null;
  ci?: [number, number] | null;
}) {
  const { t } = useTranslation();
  // the backend keys cells `"<judge>/<human>"` (app/evaluation/stats.py::confusion)
  const cell = (judge: string, human: string): number => confusion[`${judge}/${human}`] ?? 0;
  const labels = ["pass", "fail"] as const;
  return (
    <div className="v3-std-confusion">
      <table>
        <thead>
          <tr>
            <th />
            {labels.map((human) => (
              <th key={human}>{t("v2.dlc.calibration.human", { label: t(`v2.dlc.label.${human}`) })}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {labels.map((judge) => (
            <tr key={judge}>
              <th>{t("v2.dlc.calibration.judge", { label: t(`v2.dlc.label.${judge}`) })}</th>
              {labels.map((human) => (
                <td key={human} className={judge === human ? "agree" : "disagree"}>
                  {cell(judge, human)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      <div className="kappa">
        <Chip s={kappaSignal(kappa)}>κ {kappa === null ? "—" : kappa.toFixed(2)}</Chip>
        {ci && <span className="v3-std-muted">{t("v2.dlc.ciRaw", { low: ci[0].toFixed(2), high: ci[1].toFixed(2) })}</span>}
      </div>
    </div>
  );
}

/** criteria × case tier. A thin row means that criterion's rate is noise. */
export function CoverageMatrix({ rows }: { rows: CoverageRow[] }) {
  const { t } = useTranslation();
  if (rows.length === 0) return <p className="v3-std-empty">{t("v2.dlc.golden.noCoverage")}</p>;
  const max = Math.max(1, ...rows.map((r) => r.total));
  return (
    <table className="v3-table v3-std-coverage">
      <thead>
        <tr>
          <th>{t("v2.dlc.criteria.key")}</th>
          {CASE_TIERS.map((tier) => (
            <th key={tier} className="num">{t(`v2.dlc.caseTier.${tier}`)}</th>
          ))}
          <th className="num">{t("v2.dlc.golden.items")}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.key}>
            <td className="mono">{row.key}</td>
            {CASE_TIERS.map((tier) => {
              const value = row[tier as CaseTier] as number;
              return (
                <td key={tier} className="num">
                  <span className="cell" style={{ opacity: value === 0 ? 0.2 : 0.35 + (value / max) * 0.65 }}>{value}</span>
                </td>
              );
            })}
            <td className="num">
              {row.total} {row.thin && <Chip s="wait">{t("v2.dlc.golden.thin")}</Chip>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * The fix ladder: one rung per run, with what changed between rungs. A rung whose
 * predecessor changed more than one spec layer is marked, because a gain across two
 * changes cannot be attributed to either.
 */
export function LadderChart({ rungs }: { rungs: LadderRung[] }) {
  const { t } = useTranslation();
  if (rungs.length === 0) return <p className="v3-std-empty">{t("v2.dlc.compare.noRuns")}</p>;
  return (
    <ol className="v3-std-ladder">
      {rungs.map((rung, index) => {
        const previous = index > 0 ? rungs[index - 1] : null;
        const delta =
          previous && rung.mean_gate_rate !== null && previous.mean_gate_rate !== null
            ? rung.mean_gate_rate - previous.mean_gate_rate
            : null;
        return (
          <li key={rung.run_id}>
            <div className="rung">
              <div className="bar" style={{ width: `${Math.max(2, (rung.mean_gate_rate ?? 0) * 100)}%` }} />
              <span className="val">{rate(rung.mean_gate_rate)}</span>
              {delta !== null && (
                <span className={delta >= 0 ? "delta up" : "delta down"}>
                  {delta >= 0 ? "+" : ""}
                  {(delta * 100).toFixed(1)}pp
                </span>
              )}
            </div>
            <div className="meta">
              <span className="mono">{rung.run_id.slice(0, 8)}</span>
              <span>{t("v2.dlc.compare.version", { v: rung.agent_version ?? "—" })}</span>
              <span>{t("v2.dlc.compare.criteriaV", { v: rung.criteria_set_version ?? "—" })}</span>
              {rung.redline_violations > 0 && <Chip s="act">{t("v2.dlc.compare.redlines", { n: rung.redline_violations })}</Chip>}
              {rung.layers_changed.length > 0 && (
                <Chip s={rung.layers_changed.length > 1 ? "wait" : undefined}>{rung.layers_changed.join(" + ")}</Chip>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

/** The five dimensions, in the methodology's fixed order, standard vs current. */
export function DimensionTiles({ dimensions }: { dimensions: ScorecardDimension[] }) {
  const { t } = useTranslation();
  return (
    <div className="v3-std-dims">
      {dimensions.map((d) => {
        const short = d.current !== null && d.standard !== null && d.current < d.standard;
        const signal = d.not_applicable ? undefined : d.redline_violations > 0 || short ? "act" : d.current !== null ? "ok" : undefined;
        return (
          <section key={d.dimension} className="v3-panel v3-std-dim" data-signal={signal}>
            <div className="head">
              <span className="name">{t(`v2.dlc.dimension.${d.dimension}`)}</span>
              {d.redline_violations > 0 && <Chip s="act">{t("v2.dlc.scorecard.violations", { n: d.redline_violations })}</Chip>}
            </div>
            {d.not_applicable ? (
              <p className="v3-std-empty">{t("v2.dlc.scorecard.notApplicable")}</p>
            ) : d.current === null && d.metrics.length > 0 ? (
              d.metrics.map((m) => (
                <div key={m.key} className="metric">
                  <div className={m.verdict === "fail" ? "big short" : "big"}>
                    {m.value === null ? "—" : Math.round(m.value).toLocaleString()}
                  </div>
                  <div className="sub">
                    {t(`v2.dlc.metric.${m.metric}`, { defaultValue: m.metric ?? m.key })} {m.op}{" "}
                    {m.bound === null ? "—" : m.bound.toLocaleString()}
                    {typeof m.n === "number" ? ` · ${t("v2.dlc.nOf", { n: m.n })}` : ""}
                  </div>
                </div>
              ))
            ) : (
              <>
                <div className={short ? "big short" : "big"}>{rate(d.current)}</div>
                <div className="sub">{t("v2.dlc.scorecard.standard", { v: d.standard === null ? "—" : rate(d.standard, 0) })}</div>
                <MiniSpark values={d.series.map((p) => p.rate)} bad={short} />
                <div className="sub">{t("v2.dlc.scorecard.gates", { effective: d.effective_gates, declared: d.declared_gates })}</div>
              </>
            )}
          </section>
        );
      })}
    </div>
  );
}
