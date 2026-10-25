/**
 * Agent-DLC chart primitives (`docs/agent-dlc-design.md` §7).
 *
 * Every one of these exists to make a *judgment* possible, not to decorate:
 * a rate without its interval invites a decision the sample cannot support, and a
 * pass rate without its baseline band cannot say whether today is unusual. So the
 * interval, the baseline and the denominator are drawn, never hidden behind a
 * single number.
 *
 * All plain SVG (no chart dependency), scoped under `.v2` in `dlc.css`.
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
  kappaTone,
  tierTone,
} from "../../../lib/dlc";
import { Tag, type TagTone } from "../../ui";

const pct = (value: number | null | undefined, digits = 1): string =>
  value === null || value === undefined ? "—" : `${(value * 100).toFixed(digits)}%`;

/** The criterion's tier, with the demotion made visible when a judge is uncalibrated. */
export function TierChip({
  tier,
  effective,
}: {
  tier: DlcTier;
  effective?: DlcTier;
}) {
  const { t } = useTranslation();
  const demoted = effective !== undefined && effective !== tier;
  return (
    <span className="v2-dlc-tierchip">
      <Tag tone={tierTone(demoted ? (effective as DlcTier) : tier)}>
        {t(`v2.dlc.tier.${demoted ? effective : tier}`)}
      </Tag>
      {demoted && (
        <span className="v2-dlc-demoted" title={t("v2.dlc.criteria.demotedHint")}>
          {t("v2.dlc.criteria.demotedFrom", { tier: t(`v2.dlc.tier.${tier}`) })}
        </span>
      )}
    </span>
  );
}

/**
 * A rate as a point with its Wilson interval, against the threshold.
 *
 * When the threshold falls inside the interval the bar is marked `undecided`: the
 * honest reading is "this sample cannot tell", which is a different decision from
 * "it failed".
 */
export function CiBar({
  rate,
  low,
  high,
  threshold,
  undecided,
  n,
}: {
  rate: number | null;
  low?: number | null;
  high?: number | null;
  threshold?: number | null;
  undecided?: boolean;
  n?: number | null;
}) {
  const { t } = useTranslation();
  if (rate === null || rate === undefined) {
    return <span className="v2-muted">{t("v2.dlc.noResult")}</span>;
  }
  const x = (v: number) => `${Math.min(100, Math.max(0, v * 100))}%`;
  const hasCi = typeof low === "number" && typeof high === "number";
  const ok = threshold === null || threshold === undefined ? null : rate >= threshold;
  return (
    <div className={`v2-dlc-cibar${undecided ? " undecided" : ""}`}>
      <div className="track">
        {hasCi && (
          <div
            className="ci"
            style={{ left: x(low as number), width: `${Math.max(0.5, ((high as number) - (low as number)) * 100)}%` }}
          />
        )}
        <div className={`dot${ok === false ? " bad" : ok ? " good" : ""}`} style={{ left: x(rate) }} />
        {typeof threshold === "number" && <div className="thr" style={{ left: x(threshold) }} />}
      </div>
      <div className="legend">
        <b>{pct(rate)}</b>
        {hasCi && (
          <span className="ciText">
            {t("v2.dlc.ci", { low: pct(low, 0), high: pct(high, 0) })}
          </span>
        )}
        {typeof n === "number" && <span className="n">{t("v2.dlc.nOf", { n })}</span>}
        {undecided && <Tag tone="orange">{t("v2.dlc.undecided")}</Tag>}
      </div>
    </div>
  );
}

/** A bare trend line — used in table cells where only the shape matters. */
export function Sparkline({
  values,
  tone = "primary",
}: {
  values: (number | null)[];
  tone?: "primary" | "danger" | "success";
}) {
  const points = values.filter((v): v is number => typeof v === "number");
  if (points.length < 2) return <span className="v2-muted">—</span>;
  const W = 72;
  const H = 20;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const path = points
    .map((v, i) => `${((i / (points.length - 1)) * W).toFixed(1)},${(H - ((v - min) / span) * (H - 2) - 1).toFixed(1)}`)
    .join(" ");
  return (
    <svg className="v2-dlc-spark" viewBox={`0 0 ${W} ${H}`} aria-hidden="true">
      <polyline points={path} fill="none" stroke={`var(--v2-${tone === "primary" ? "primary" : tone})`} strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

/**
 * A dimension's pass rate over time against its rolling-median baseline band.
 *
 * Until the baseline has enough points the band is drawn hollow and labelled, so a
 * dip is not read as a regression on the strength of three days of history.
 */
export function BandChart({
  points,
  minBaselinePoints = 8,
  height = 140,
}: {
  points: DimensionPoint[];
  minBaselinePoints?: number;
  height?: number;
}) {
  const { t } = useTranslation();
  if (points.length === 0) {
    return <p className="v2-dlc-empty">{t("v2.dlc.watch.noSeries")}</p>;
  }
  const W = 560;
  const TOP = 10;
  const BASE = height - 18;
  const rates = points.map((p) => p.rate);
  const baselines = points.map((p) => p.baseline).filter((b): b is number => typeof b === "number");
  const lo = Math.min(...rates, ...baselines, 0.5);
  const hi = Math.max(...rates, ...baselines, 1);
  const span = hi - lo || 1;
  const x = (i: number) => (points.length > 1 ? (i / (points.length - 1)) * W : W / 2);
  const y = (v: number) => BASE - ((v - lo) / span) * (BASE - TOP);
  const ready = (points[points.length - 1]?.baseline_points ?? 0) >= minBaselinePoints;
  const line = points.map((p, i) => `${x(i).toFixed(1)},${y(p.rate).toFixed(1)}`).join(" ");
  const band = points
    .filter((p) => typeof p.baseline === "number")
    .map((p) => `${x(points.indexOf(p)).toFixed(1)},${y(p.baseline as number).toFixed(1)}`)
    .join(" ");
  return (
    <>
      <svg className="v2-dlc-band" viewBox={`0 0 ${W} ${height}`} preserveAspectRatio="none" aria-hidden="true">
        <line x1="0" y1={BASE} x2={W} y2={BASE} className="axis" />
        <line x1="0" y1={TOP} x2={W} y2={TOP} className="grid" />
        {band && (
          <polyline
            points={band}
            fill="none"
            className={ready ? "baseline" : "baseline thin"}
            strokeWidth="1.5"
            vectorEffect="non-scaling-stroke"
          />
        )}
        <polyline points={line} fill="none" className="rate" strokeWidth="2" vectorEffect="non-scaling-stroke" />
        {points.map((p, i) => (
          <circle key={p.run_id} cx={x(i)} cy={y(p.rate)} r="2.5" className="pt" />
        ))}
      </svg>
      <div className="v2-dlc-axis">
        <span>{pct(lo, 0)}</span>
        {!ready && (
          <span className="v2-dlc-warn">
            {t("v2.dlc.watch.baselineThin", {
              have: points[points.length - 1]?.baseline_points ?? 0,
              need: minBaselinePoints,
            })}
          </span>
        )}
        <span>{pct(hi, 0)}</span>
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
    <div className="v2-dlc-confusion">
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
      <div className="v2-dlc-kappa">
        <Tag tone={kappaTone(kappa)}>
          κ {kappa === null ? "—" : kappa.toFixed(2)}
        </Tag>
        {ci && <span className="v2-muted">{t("v2.dlc.ciRaw", { low: ci[0].toFixed(2), high: ci[1].toFixed(2) })}</span>}
      </div>
    </div>
  );
}

/** criteria × case tier. A thin row means that criterion's rate is noise. */
export function CoverageMatrix({ rows }: { rows: CoverageRow[] }) {
  const { t } = useTranslation();
  if (rows.length === 0) return <p className="v2-dlc-empty">{t("v2.dlc.golden.noCoverage")}</p>;
  const max = Math.max(1, ...rows.map((r) => r.total));
  return (
    <table className="v2-dlc-coverage">
      <thead>
        <tr>
          <th>{t("v2.dlc.criteria.key")}</th>
          {CASE_TIERS.map((tier) => (
            <th key={tier}>{t(`v2.dlc.caseTier.${tier}`)}</th>
          ))}
          <th>{t("v2.dlc.golden.items")}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.key} className={row.thin ? "thin" : undefined}>
            <td className="mono">{row.key}</td>
            {CASE_TIERS.map((tier) => {
              const value = row[tier as CaseTier] as number;
              return (
                <td key={tier}>
                  <span
                    className="cell"
                    style={{ opacity: value === 0 ? 0.15 : 0.25 + (value / max) * 0.75 }}
                  >
                    {value}
                  </span>
                </td>
              );
            })}
            <td>
              {row.total}
              {row.thin && (
                <Tag tone="orange">{t("v2.dlc.golden.thin")}</Tag>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * The fix ladder: one rung per run, with what changed between rungs.
 *
 * A rung whose predecessor changed more than one spec layer is marked, because a
 * gain across two changes cannot be attributed to either.
 */
export function LadderChart({ rungs }: { rungs: LadderRung[] }) {
  const { t } = useTranslation();
  if (rungs.length === 0) return <p className="v2-dlc-empty">{t("v2.dlc.compare.noRuns")}</p>;
  return (
    <ol className="v2-dlc-ladder">
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
              <span className="val">{pct(rung.mean_gate_rate)}</span>
              {delta !== null && (
                <span className={`delta ${delta >= 0 ? "up" : "down"}`}>
                  {delta >= 0 ? "+" : ""}
                  {(delta * 100).toFixed(1)}pp
                </span>
              )}
            </div>
            <div className="meta">
              <span className="mono">{rung.run_id.slice(0, 8)}</span>
              <span>{t("v2.dlc.compare.version", { v: rung.agent_version ?? "—" })}</span>
              <span>{t("v2.dlc.compare.criteriaV", { v: rung.criteria_set_version ?? "—" })}</span>
              {rung.redline_violations > 0 && (
                <Tag tone="red">{t("v2.dlc.compare.redlines", { n: rung.redline_violations })}</Tag>
              )}
              {rung.layers_changed.length > 0 && (
                <Tag tone={rung.layers_changed.length > 1 ? "orange" : "gray"}>
                  {rung.layers_changed.join(" + ")}
                </Tag>
              )}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

const DIM_TONE: Record<string, TagTone> = {
  cognition: "blue",
  quality: "green",
  responsibility: "red",
  cost: "orange",
  performance: "gray",
};

/** The five dimensions, in the methodology's fixed order, standard vs current. */
export function DimensionTiles({ dimensions }: { dimensions: ScorecardDimension[] }) {
  const { t } = useTranslation();
  return (
    <div className="v2-dlc-dims">
      {dimensions.map((d) => {
        const short = d.current !== null && d.standard !== null && d.current < d.standard;
        return (
          <div key={d.dimension} className={`tile${d.not_applicable ? " na" : ""}`}>
            <div className="head">
              <Tag tone={DIM_TONE[d.dimension]}>{t(`v2.dlc.dimension.${d.dimension}`)}</Tag>
              {d.redline_violations > 0 && <Tag tone="red">{t("v2.dlc.scorecard.violations", { n: d.redline_violations })}</Tag>}
            </div>
            {d.not_applicable ? (
              <p className="v2-dlc-empty">{t("v2.dlc.scorecard.notApplicable")}</p>
            ) : d.current === null && d.metrics.length > 0 ? (
              // a measured metric, not a pass rate: show the value against its bound
              <>
                {d.metrics.map((m) => (
                  <div key={m.key} className="metric">
                    <div className={`big${m.verdict === "fail" ? " short" : ""}`}>
                      {m.value === null ? "—" : Math.round(m.value).toLocaleString()}
                    </div>
                    <div className="sub">
                      {t(`v2.dlc.metric.${m.metric}`, { defaultValue: m.metric ?? m.key })}{" "}
                      {m.op} {m.bound === null ? "—" : m.bound.toLocaleString()}
                      {typeof m.n === "number" ? ` · ${t("v2.dlc.nOf", { n: m.n })}` : ""}
                    </div>
                  </div>
                ))}
              </>
            ) : (
              <>
                <div className={`big${short ? " short" : ""}`}>{pct(d.current)}</div>
                <div className="sub">
                  {t("v2.dlc.scorecard.standard", { v: d.standard === null ? "—" : pct(d.standard, 0) })}
                </div>
                <Sparkline values={d.series.map((p) => p.rate)} tone={short ? "danger" : "primary"} />
                <div className="sub">
                  {t("v2.dlc.scorecard.gates", { effective: d.effective_gates, declared: d.declared_gates })}
                </div>
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}
