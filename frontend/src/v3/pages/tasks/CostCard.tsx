/**
 * Evaluation cost (eval-cost §1.3), one instrument everywhere:
 *
 * - `EstimateCostCard` — "Estimated cost" before submit, the same card in the task
 *   wizard, the release gate and online evaluation create: the mid total, its
 *   low–high range, the four parts (agent / runtime / LLM judges / managed
 *   evaluators), the basis, unpriced models; over the limit blocks, over the
 *   confirm threshold asks.
 * - `RunCostCard` — the task detail's "This run's cost": measured total against the
 *   estimate, its parts and source; while the run replays, the live spend.
 * - `CostCell` — the task list's cost column; `OnlineCostCard` — cost per day.
 */
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  type CostPart,
  type CostSummary,
  deltaPct,
  type EvalCostEstimate,
  estimateParts,
  evalCostApi,
  partShares,
  rangePosition,
  usd,
} from "../../../lib/api/evalCost";
import { ago } from "../../format";
import { useLoad } from "../../hooks";
import { Chip, Lamp, Notice, Panel, type Signal, Skeleton } from "../../ui";
import "./cost.css";

const LIVE_POLL_MS = 8000;

type T = ReturnType<typeof useTranslation>["t"];

function PartBar({ parts }: { parts: { key: string; usd: number | null }[] }) {
  const shares = partShares(parts).filter((p) => p.share > 0);
  return (
    <div className="v3-cost-bar" role="presentation">
      {shares.map((p) => (
        <i key={p.key} data-part={p.key} style={{ width: `${(p.share * 100).toFixed(2)}%` }} />
      ))}
    </div>
  );
}

function RangeScale({ range, t }: { range: { low: number; mid: number; high: number }; t: T }) {
  const at = rangePosition(range);
  return (
    <div className="v3-cost-range" aria-label={t("v3.evalCost.rangeAria", { low: usd(range.low), high: usd(range.high) })}>
      <div className="scale">
        <b style={{ left: 0 }} />
        <b style={{ right: 0 }} />
        <i style={{ left: `${(at * 100).toFixed(1)}%` }} />
      </div>
      <div className="ends">
        <span>{`${t("v3.evalCost.low")} ${usd(range.low)}`}</span>
        <span>{`${t("v3.evalCost.high")} ${usd(range.high)}`}</span>
      </div>
    </div>
  );
}

function basisLabel(t: T, part: CostPart): string {
  return t(`v3.evalCost.basis.${part.basis}`, { defaultValue: part.basis });
}

function PartList({ parts, t, why }: { parts: { key: string; usd: number | null }[]; t: T; why?: (key: string) => string | null }) {
  return (
    <ul className="v3-cost-parts">
      {parts.map((p) => (
        <li key={p.key} data-part={p.key} data-zero={!p.usd}>
          <span className="nm">{t(`v3.evalCost.part.${p.key}`)}</span>
          <span className="amt">{p.usd === null ? t("v3.evalCost.unpriced") : usd(p.usd)}</span>
          {why?.(p.key) && <span className="why">{why(p.key)}</span>}
        </li>
      ))}
    </ul>
  );
}

/** "Estimated cost" — before any evaluation is submitted. */
export function EstimateCostCard({
  estimate,
  loading,
  error,
  confirmed,
  onConfirm,
  title,
}: {
  estimate: EvalCostEstimate | null;
  loading: boolean;
  error?: string | null;
  confirmed?: boolean;
  onConfirm?: (value: boolean) => void;
  title?: string;
}) {
  const { t } = useTranslation();
  const perDay = estimate?.period === "day";
  const signal: Signal | undefined = estimate?.over_limit ? "act" : estimate?.confirm_required ? "wait" : undefined;
  const parts = estimateParts(estimate);
  const unpriced = estimate?.unpriced_models ?? [];
  const prices = estimate?.basis?.prices;
  return (
    <Panel
      title={title ?? t("v3.evalCost.estimateTitle")}
      end={<span>{perDay ? t("v3.evalCost.perDay") : t("v3.evalCost.perRun")}</span>}
      signal={signal}
    >
      {loading && !estimate ? (
        <Skeleton rows={2} />
      ) : error && !estimate ? (
        <Notice s="wait">{t("v3.evalCost.estimateUnavailable")}</Notice>
      ) : !estimate ? (
        <span style={{ color: "var(--v3-text-3)" }}>—</span>
      ) : (
        <div className="v3-cost" data-s={signal} aria-busy={loading}>
          <div className="v3-cost-head">
            <div className="v3-cost-fig">
              <span className="k">{t("v3.evalCost.mid")}</span>
              <span className="v">
                {estimate.total_usd === null ? t("v3.evalCost.unknown") : usd(estimate.total_usd)}
                {perDay && estimate.total_usd !== null && <small>{t("v3.evalCost.slashDay")}</small>}
              </span>
              <span className="sub">
                {perDay
                  ? t("v3.evalCost.sessionsPerDay", { count: estimate.sessions })
                  : t("v3.evalCost.sessionsRun", { count: estimate.sessions, minutes: estimate.duration_minutes ?? 0 })}
                {estimate.sessions_basis === "history_7d" && ` · ${t("v3.evalCost.sessionsFromHistory")}`}
              </span>
            </div>
            {estimate.range && <RangeScale range={estimate.range} t={t} />}
            <div className="v3-cost-side">
              {estimate.over_limit && <Chip s="act">{t("v3.evalCost.overLimit", { max: usd(estimate.max_usd) })}</Chip>}
              {!estimate.over_limit && estimate.confirm_required && <Chip s="wait">{t("v3.evalCost.needsConfirm")}</Chip>}
              {unpriced.length > 0 && <Chip s="wait" title={unpriced.join(", ")}>{t("v3.evalCost.unpricedCount", { count: unpriced.length })}</Chip>}
            </div>
          </div>
          <PartBar parts={parts} />
          <PartList
            parts={parts}
            t={t}
            why={(key) => {
              const part = parts.find((p) => p.key === key);
              return part ? basisLabel(t, part) : null;
            }}
          />
          {unpriced.length > 0 && (
            <Notice s="wait">{t("v3.evalCost.unpricedModels", { models: unpriced.join(", ") })}</Notice>
          )}
          {estimate.over_limit && <Notice s="act">{t("v3.evalCost.overLimitHint", { max: usd(estimate.max_usd) })}</Notice>}
          {estimate.judges.length > 0 && (
            <details>
              <summary>{t("v3.evalCost.judgeDetail", { count: estimate.judges.length })}</summary>
              <table className="v3-cost-judges">
                <thead>
                  <tr>
                    <th>{t("v3.evalCost.colEvaluator")}</th>
                    <th>{t("v3.evalCost.colBilledBy")}</th>
                    <th style={{ textAlign: "right" }}>{t("v3.evalCost.colJudgements")}</th>
                    <th style={{ textAlign: "right" }}>{t("v3.evalCost.colTokens")}</th>
                    <th style={{ textAlign: "right" }}>{t("v3.evalCost.colUsd")}</th>
                  </tr>
                </thead>
                <tbody>
                  {estimate.judges.map((j) => (
                    <tr key={j.evaluator_id}>
                      <td>{j.evaluator_id}</td>
                      <td>{t(`v3.evalCost.billedBy.${j.billed_by ?? "your_account"}`, { defaultValue: j.billed_by ?? "" })}</td>
                      <td className="n">{j.judgements ?? "—"}</td>
                      <td className="n" title={j.samples ? t("v3.evalCost.samples", { count: j.samples }) : t("v3.evalCost.roughTokens")}>
                        {j.tokens_per_item ?? "—"}
                      </td>
                      <td className="n">{j.usd === null || j.usd === undefined ? t("v3.evalCost.unpriced") : usd(j.usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
          )}
          <div className="v3-cost-basis">
            <span>{t("v3.evalCost.agentSamples")} <b>{estimate.basis?.agent_samples ?? 0}</b></span>
            <span>{t("v3.evalCost.durationSamples")} <b>{estimate.basis?.duration_samples ?? 0}</b></span>
            {prices?.source && (
              <span>
                {t("v3.evalCost.priceSource")} <b>{prices.source}</b>
                {prices.updated_at && ` · ${t("v3.evalCost.updated", { date: prices.updated_at })}`}
              </span>
            )}
            {estimate.basis?.model_prices_updated_at && (
              <span>{t("v3.evalCost.modelPrices")} <b>{ago(estimate.basis.model_prices_updated_at)}</b></span>
            )}
          </div>
          {estimate.confirm_required && !estimate.over_limit && onConfirm && (
            <label className="v3-cost-confirm">
              <input type="checkbox" checked={Boolean(confirmed)} onChange={(e) => onConfirm(e.target.checked)} />
              {t("v3.evalCost.confirm", { usd: usd(estimate.total_usd) })}
            </label>
          )}
        </div>
      )}
    </Panel>
  );
}

function deltaSignal(pct: number | null): Signal | undefined {
  if (pct === null) return undefined;
  return pct > 25 ? "wait" : pct <= 0 ? "ok" : undefined;
}

/** "This run's cost" — measured at the run's end; the live spend while it replays. */
export function RunCostCard({ runId, running }: { runId: string; running: boolean }) {
  const { t } = useTranslation();
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setTick((n) => n + 1), LIVE_POLL_MS);
    return () => window.clearInterval(timer);
  }, [running]);
  // one more read when the run ends: the terminal record is written then
  const card = useLoad(() => evalCostApi.runCost(runId), `v3-run-cost:${runId}:${tick}:${running}`);
  const data = card.data;
  if (card.error && !data) return null; // an older server: the card simply is not there
  const actual = data?.actual ?? null;
  const live = data?.live ?? null;
  const estimateTotal = data?.estimate_total_usd ?? null;
  const total = actual ? actual.total_usd : live ? live.total_usd : null;
  const pct = actual ? deltaPct(actual.total_usd, estimateTotal) : null;
  const parts = actual
    ? [
        { key: "agent", usd: actual.agent_usd },
        { key: "runtime", usd: actual.runtime_usd },
        { key: "judge", usd: actual.judge_usd },
        { key: "managed", usd: actual.managed_evaluations_usd },
      ]
    : live
      ? [
          { key: "agent", usd: live.agent_usd },
          { key: "runtime", usd: live.runtime_usd },
        ]
      : [];
  return (
    <Panel
      title={t("v3.evalCost.runTitle")}
      end={actual ? <span>{t(`v3.evalCost.source.${actual.source}`, { defaultValue: actual.source })}</span> : undefined}
    >
      {!data ? (
        <Skeleton rows={2} />
      ) : !actual && !live ? (
        <div className="v3-cost">
          <div className="v3-cost-head">
            <div className="v3-cost-fig">
              <span className="k">{t("v3.evalCost.estimated")}</span>
              <span className="v">{usd(estimateTotal)}</span>
              <span className="sub">{data.running ? t("v3.evalCost.notStarted") : t("v3.evalCost.notMeasured")}</span>
            </div>
            {data.estimate_range && <RangeScale range={data.estimate_range} t={t} />}
          </div>
        </div>
      ) : (
        <div className="v3-cost">
          <div className="v3-cost-head">
            <div className="v3-cost-fig">
              <span className="k">{actual ? t("v3.evalCost.actual") : t("v3.evalCost.spentSoFar")}</span>
              <span className="v">{usd(total)}</span>
              <span className="sub">
                {estimateTotal !== null
                  ? t("v3.evalCost.vsEstimate", { estimate: usd(estimateTotal), actual: usd(total) })
                  : t("v3.evalCost.noEstimate")}
              </span>
            </div>
            <div className="v3-cost-side">
              {pct !== null && (
                <span className="v3-cost-delta" data-s={deltaSignal(pct)}>
                  {pct > 0 ? "+" : ""}
                  {pct}%
                </span>
              )}
              {live && !actual && (
                <span className="v3-cost-live">
                  <Lamp s="info" live />
                  {t("v3.evalCost.live", { count: live.sessions })}
                </span>
              )}
              {actual?.partial && <Chip s="wait">{t("v3.evalCost.partial", { status: actual.status })}</Chip>}
              {(actual?.unpriced_models.length ?? 0) > 0 && (
                <Chip s="wait" title={actual?.unpriced_models.join(", ")}>
                  {t("v3.evalCost.unpricedCount", { count: actual?.unpriced_models.length ?? 0 })}
                </Chip>
              )}
            </div>
          </div>
          <PartBar parts={parts} />
          <PartList
            parts={parts}
            t={t}
            why={(key) => {
              if (key === "runtime") {
                const seconds = actual?.runtime_seconds ?? live?.seconds ?? 0;
                return t("v3.evalCost.seconds", { seconds: Math.round(seconds) });
              }
              if (key === "agent" && actual?.agent_tokens) {
                const tok = actual.agent_tokens;
                const n = tok.total ?? (tok.input ?? 0) + (tok.output ?? 0);
                return n ? t("v3.evalCost.tokens", { count: n }) : null;
              }
              if ((key === "judge" || key === "managed") && actual) {
                const n = Object.values(actual.judgements).reduce((a, b) => a + b, 0);
                return key === "judge" && n ? t("v3.evalCost.judgements", { count: n }) : null;
              }
              return null;
            }}
          />
          <div className="v3-cost-basis">
            <span>{t("v3.evalCost.sessionsMeasured")} <b>{actual?.sessions ?? live?.sessions ?? 0}</b></span>
            {actual && <span>{t("v3.evalCost.computed")} <b>{ago(actual.computed_at)}</b></span>}
            {live && !actual && <span>{t("v3.evalCost.updatedLive")} <b>{ago(live.updated_at)}</b></span>}
            {data.prices.source && <span>{t("v3.evalCost.priceSource")} <b>{data.prices.source}</b></span>}
          </div>
        </div>
      )}
    </Panel>
  );
}

/** The task list's cost column: measured, live (running) or estimate — always marked. */
export function CostCell({ summary }: { summary: CostSummary | undefined }) {
  const { t } = useTranslation();
  if (!summary || summary.usd === null) return <span style={{ color: "var(--v3-text-3)" }}>—</span>;
  return (
    <span className="v3-cost-cell" data-kind={summary.kind}>
      {usd(summary.usd)}
      {summary.kind !== "actual" && <small>{t(`v3.evalCost.kind.${summary.kind}`)}</small>}
    </span>
  );
}

/** An online evaluation's cost per day: judging plus its console reports. */
export function OnlineCostCard({ configId }: { configId: string }) {
  const { t } = useTranslation();
  const daily = useLoad(() => evalCostApi.onlineDaily(configId, 7), `v3-online-cost:${configId}`);
  if (daily.error && !daily.data) return null;
  const days = daily.data?.days ?? [];
  const max = Math.max(0, ...days.map((d) => d.usd));
  return (
    <Panel title={t("v3.evalCost.dailyTitle")} end={<span>{t("v3.evalCost.last7")}</span>}>
      {!daily.data ? (
        <Skeleton rows={2} />
      ) : days.length === 0 ? (
        <span style={{ color: "var(--v3-text-3)", fontSize: 13 }}>{t("v3.evalCost.noDaily")}</span>
      ) : (
        <div className="v3-cost">
          <div className="v3-cost-head">
            <div className="v3-cost-fig">
              <span className="k">{t("v3.evalCost.dailyTotal")}</span>
              <span className="v">{usd(daily.data.total_usd)}</span>
              <span className="sub">{t("v3.evalCost.dailyAvg", { usd: usd(daily.data.total_usd / Math.max(1, days.length)) })}</span>
            </div>
            {daily.data.unpriced_models.length > 0 && (
              <div className="v3-cost-side">
                <Chip s="wait" title={daily.data.unpriced_models.join(", ")}>
                  {t("v3.evalCost.unpricedCount", { count: daily.data.unpriced_models.length })}
                </Chip>
              </div>
            )}
          </div>
          <div className="v3-cost-days" role="list">
            {days.map((d, i) => (
              <div className="d" role="listitem" key={d.day} title={`${d.day} · ${usd(d.usd)} · ${t("v3.evalCost.judgements", { count: d.judgements })}`}>
                <span className="col" style={{ height: `${max > 0 ? Math.max(2, (d.usd / max) * 100) : 2}%`, animationDelay: `${i * 40}ms` }} />
                <span className="lb">{d.day.slice(5)}</span>
              </div>
            ))}
          </div>
          <div className="v3-cost-basis">
            <span>{t("v3.evalCost.judgeModel")} <b>{daily.data.judge_model}</b></span>
            {daily.data.basis.prices.source && <span>{t("v3.evalCost.priceSource")} <b>{daily.data.basis.prices.source}</b></span>}
            <span>{t("v3.evalCost.estimatedFromJudgements")}</span>
          </div>
        </div>
      )}
    </Panel>
  );
}

