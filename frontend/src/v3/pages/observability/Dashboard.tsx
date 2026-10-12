import { ArrowRight } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, errorMessage, type ObsDashboard } from "../../../lib/api";
import { fmtBucket, fmtCompact } from "../../../pages/observability/format";
import { fmtNumber } from "../../../v2/format";
import { approxCost } from "../../../v2/pages/observability/common";
import { ms } from "../../format";
import { useToast } from "../../hooks";
import { Lamp, Panel, Stat } from "../../ui";
import { ERROR_RATE_ACT, type Finding, findings, P95_SLOW_MS, P95_TIMEOUT_MS } from "./attention";

export type DashTarget = { tab: "traces"; status?: "error" };

function Legend({ items }: { items: { color: string; label: string }[] }) {
  return (
    <div className="v3-obs-legend">
      {items.map((item) => (
        <span key={item.label}>
          <i style={{ background: item.color }} />
          {item.label}
        </span>
      ))}
    </div>
  );
}

function Traffic({ data }: { data: ObsDashboard }) {
  const { t } = useTranslation();
  const series = data.series;
  const longRange = data.range === "7d";
  const W = 600;
  const H = 150;
  const max = Math.max(1, ...series.map((b) => b.traces));
  const slot = series.length ? W / series.length : W;
  const bw = Math.max(1, slot * 0.72);
  return (
    <Panel title={t("v3.obs.traffic")} end={<span>{t("v3.obs.trafficSub")}</span>}>
      {series.length === 0 ? (
        <div className="v3-obs-empty">{t("v3.obs.noData")}</div>
      ) : (
        <>
          <svg className="v3-obs-chart" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label={t("v3.obs.traffic")}>
            <line x1="0" y1={H - 0.5} x2={W} y2={H - 0.5} className="axis" />
            <line x1="0" y1={H / 2} x2={W} y2={H / 2} className="grid" />
            {series.map((b, i) => {
              const h = Math.max(1.5, (b.traces / max) * (H - 6));
              const eh = b.traces ? (b.errors / b.traces) * h : 0;
              const x = i * slot + (slot - bw) / 2;
              return (
                <g key={b.bucket}>
                  <title>{t("v3.obs.bucketTip", { bucket: fmtBucket(b.bucket, longRange), traces: b.traces, errors: b.errors })}</title>
                  <rect className="t" x={x} y={H - h} width={bw} height={h} rx="1" />
                  {eh > 0 && <rect className="e" x={x} y={H - eh} width={bw} height={eh} rx="1" />}
                </g>
              );
            })}
          </svg>
          <div className="v3-obs-axis">
            <span>{fmtBucket(series[0].bucket, longRange)}</span>
            {series.length > 2 && <span>{fmtBucket(series[Math.floor(series.length / 2)].bucket, longRange)}</span>}
            <span>{t("v3.obs.now")}</span>
          </div>
          <Legend items={[{ color: "var(--v3-info)", label: t("v3.obs.traces") }, { color: "var(--v3-act)", label: t("v3.obs.errors") }]} />
        </>
      )}
    </Panel>
  );
}

function Latency({ data }: { data: ObsDashboard }) {
  const { t } = useTranslation();
  const series = data.series;
  const W = 600;
  const TOP = 10;
  const BASE = 146;
  const max = Math.max(1, ...series.map((b) => b.p95_ms));
  const x = (i: number) => (series.length > 1 ? (i / (series.length - 1)) * W : W / 2);
  const y = (v: number) => BASE - (v / max) * (BASE - TOP);
  const line = (pick: (b: ObsDashboard["series"][number]) => number) =>
    series.map((b, i) => `${x(i).toFixed(1)},${y(pick(b)).toFixed(1)}`).join(" ");
  // the slow threshold, drawn when it falls inside the chart
  const slowY = P95_SLOW_MS <= max ? y(P95_SLOW_MS) : null;
  return (
    <Panel title={t("v3.obs.latency")} end={<span>{t("v3.obs.latencySub")}</span>}>
      {series.length === 0 ? (
        <div className="v3-obs-empty">{t("v3.obs.noData")}</div>
      ) : (
        <>
          <svg className="v3-obs-chart" viewBox={`0 0 ${W} 150`} preserveAspectRatio="none" role="img" aria-label={t("v3.obs.latency")}>
            <line x1="0" y1={BASE} x2={W} y2={BASE} className="axis" />
            <line x1="0" y1={TOP} x2={W} y2={TOP} className="grid" />
            {slowY != null && <line x1="0" y1={slowY} x2={W} y2={slowY} stroke="var(--v3-wait)" strokeDasharray="4 4" opacity="0.5" vectorEffect="non-scaling-stroke" />}
            <polyline fill="none" className="p95" strokeWidth="2" vectorEffect="non-scaling-stroke" points={line((b) => b.p95_ms)} />
            <polyline fill="none" className="p50" strokeWidth="2" vectorEffect="non-scaling-stroke" points={line((b) => b.p50_ms)} />
          </svg>
          <div className="v3-obs-axis">
            <span>{t("v3.obs.peak", { v: ms(max) })}</span>
            <span>{t("v3.obs.now")}</span>
          </div>
          <Legend
            items={[
              { color: "var(--v3-info)", label: `P50 · ${ms(data.tiles.latency.p50_ms)}` },
              { color: "var(--v3-wait)", label: `P95 · ${ms(data.tiles.latency.p95_ms)}` },
            ]}
          />
        </>
      )}
    </Panel>
  );
}

function Tokens({ data, onPricesRefreshed }: { data: ObsDashboard; onPricesRefreshed: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const rows = data.tokens_by_model;
  const grand = Math.max(1, ...rows.map((r) => r.total));
  const refresh = () => {
    setBusy(true);
    api
      .obsRefreshPrices()
      .then((res) => {
        toast("ok", t("obs.prices.refreshed", { updated: res.meta.updated.length, added: res.meta.added.length }));
        onPricesRefreshed();
      })
      .catch((err: unknown) => toast("act", t("obs.loadFailed", { msg: errorMessage(err) })))
      .finally(() => setBusy(false));
  };
  return (
    <Panel
      title={t("v3.obs.tokens")}
      end={
        <button type="button" className="v3-obs-link" disabled={busy} onClick={refresh}>
          {busy ? t("v3.obs.pricesUpdating") : t("v3.obs.pricesUpdate")}
        </button>
      }
    >
      {rows.length === 0 ? (
        <div className="v3-obs-empty">{t("v3.obs.noTokens")}</div>
      ) : (
        <>
          {rows.map((r) => (
            <div className="v3-obs-hbar" key={r.model}>
              <div className="l">
                <span title={r.model}>{r.model}</span>
                <b>{fmtCompact(r.total)} · {approxCost(r.est_cost_usd)}</b>
              </div>
              <div className="track" title={t("v3.obs.inOut", { input: fmtNumber(r.input), output: fmtNumber(r.output) })}>
                <div className="seg" style={{ width: `${(r.input / grand) * 100}%`, background: "var(--v3-info)" }} />
                <div className="seg" style={{ width: `${(r.output / grand) * 100}%`, background: "var(--v3-obs-out)" }} />
              </div>
            </div>
          ))}
          <Legend items={[{ color: "var(--v3-info)", label: t("v3.obs.input") }, { color: "var(--v3-obs-out)", label: t("v3.obs.output") }]} />
          <div className="v3-obs-note">
            {t("obs.charts.priceNote")}
            {data.prices_meta?.updated_at != null && ` · ${t("obs.prices.updatedAt", { date: data.prices_meta.updated_at.slice(0, 10) })}`}
          </div>
        </>
      )}
    </Panel>
  );
}

function Tools({ data }: { data: ObsDashboard }) {
  const { t } = useTranslation();
  // failing tools first: that is what a reader of this panel is looking for
  const rows = [...data.top_tools].sort((a, b) => (b.errors > 0 ? 1 : 0) - (a.errors > 0 ? 1 : 0) || b.calls - a.calls).slice(0, 8);
  const max = Math.max(1, ...rows.map((r) => r.calls));
  return (
    <Panel title={t("v3.obs.tools")} end={<span>{t("v3.obs.toolsSub")}</span>}>
      {rows.length === 0 ? (
        <div className="v3-obs-empty">{t("v3.obs.noTools")}</div>
      ) : (
        <>
          {rows.map((r) => (
            <div className="v3-obs-hbar" key={r.tool}>
              <div className="l">
                <span title={r.tool} style={r.errors > 0 ? { color: "var(--v3-text)" } : undefined}>{r.tool}</span>
                <b style={r.errors > 0 ? { color: "var(--v3-act)" } : undefined}>
                  {t("v3.obs.calls", { count: r.calls })}
                  {r.success_rate != null && ` · ${r.success_rate}%`}
                </b>
              </div>
              <div className="track">
                <div className="seg" style={{ width: `${((r.calls - r.errors) / max) * 100}%`, background: "var(--v3-ok)" }} />
                {r.errors > 0 && <div className="seg" style={{ width: `${(r.errors / max) * 100}%`, background: "var(--v3-act)" }} />}
              </div>
            </div>
          ))}
          <Legend items={[{ color: "var(--v3-ok)", label: t("v3.obs.succeeded") }, { color: "var(--v3-act)", label: t("v3.obs.failed") }]} />
        </>
      )}
    </Panel>
  );
}

function FindingRow({ f, longRange, onOpen }: { f: Finding; longRange: boolean; onOpen: (target: DashTarget) => void }) {
  const { t } = useTranslation();
  const v = f.values;
  const text =
    f.kind === "errorRate"
      ? t("v3.obs.find.errorRate", { rate: v.rate, errors: v.errors, total: v.total })
      : f.kind === "errorSpike"
        ? t("v3.obs.find.errorSpike", { bucket: fmtBucket(String(v.bucket), longRange), errors: v.errors, traces: v.traces })
        : f.kind === "slowP95"
          ? t("v3.obs.find.slowP95", { p95: ms(Number(v.p95)) })
          : t("v3.obs.find.toolFailing", { tool: v.tool, errors: v.errors, calls: v.calls });
  const hint =
    f.kind === "errorRate"
      ? t("v3.obs.find.errorRateHint", { limit: `${ERROR_RATE_ACT * 100}%` })
      : f.kind === "slowP95"
        ? t("v3.obs.find.slowP95Hint", { slow: ms(P95_SLOW_MS), timeout: ms(P95_TIMEOUT_MS) })
        : f.kind === "toolFailing"
          ? t("v3.obs.find.toolFailingHint", { rate: v.rate })
          : t("v3.obs.find.errorSpikeHint");
  return (
    <button type="button" className="v3-obs-finding" data-s={f.s} onClick={() => f.target && onOpen(f.target)}>
      <Lamp s={f.s} live />
      <span className="what">
        {text}
        <small>{hint}</small>
      </span>
      <span className="go">{t(f.target?.status ? "v3.obs.find.openFailed" : "v3.obs.find.openTraces")} <ArrowRight size={12} style={{ verticalAlign: -1 }} /></span>
    </button>
  );
}

/** The dashboard leads with what needs a look, then the numbers behind it. */
export function DashboardView({
  data,
  onOpen,
  onPricesRefreshed,
}: {
  data: ObsDashboard;
  onOpen: (target: DashTarget) => void;
  onPricesRefreshed: () => void;
}) {
  const { t } = useTranslation();
  const { tiles } = data;
  const list = findings(data);
  // the tiles can count sessions while the root-span tally is still empty
  const traffic = tiles.traces.total > 0 || tiles.sessions.total > 0;
  const errSignal = tiles.error_rate > ERROR_RATE_ACT ? "act" : tiles.traces.error > 0 ? "wait" : tiles.traces.total ? "ok" : undefined;
  const p95Signal = tiles.latency.p95_ms > P95_TIMEOUT_MS ? "act" : tiles.latency.p95_ms > P95_SLOW_MS ? "wait" : undefined;
  return (
    <>
      <Panel title={t("v3.obs.needsLook")} signal={list.length ? list[0].s : traffic ? "ok" : undefined} end={<span className="mono">{list.length}</span>}>
        {list.length === 0 ? (
          <p style={{ margin: 0, color: "var(--v3-text-2)", display: "flex", gap: 10, alignItems: "center" }}>
            <Lamp s={traffic ? "ok" : "off"} />
            {tiles.traces.total
              ? t("v3.obs.quiet", { count: tiles.traces.total })
              : tiles.sessions.total
                ? t("v3.obs.quietSessions", { count: tiles.sessions.total })
                : t("v3.obs.noTraffic")}
          </p>
        ) : (
          <div className="v3-obs-findings">
            {list.map((f, i) => <FindingRow key={`${f.kind}:${i}`} f={f} longRange={data.range === "7d"} onOpen={onOpen} />)}
          </div>
        )}
      </Panel>

      <div className="v3-obs-stats">
        <Panel><Stat label={t("v3.obs.traces")} value={fmtNumber(tiles.traces.total)} foot={t("v3.obs.tracesFoot", { ok: tiles.traces.ok, error: tiles.traces.error })} /></Panel>
        <Panel><Stat label={t("v3.obs.sessions")} value={fmtNumber(tiles.sessions.total)} foot={t("v3.obs.agentsActive", { count: tiles.sessions.agents })} /></Panel>
        <Panel signal={errSignal}>
          <Stat label={t("v3.obs.errorRate")} value={`${(tiles.error_rate * 100).toFixed(1)}%`} signal={errSignal === "act" ? "act" : undefined}
            foot={t("v3.obs.errorOf", { errors: tiles.traces.error, total: tiles.traces.total })} />
        </Panel>
        <Panel signal={p95Signal}>
          <Stat label={t("v3.obs.latencyStat")} value={<>{ms(tiles.latency.p50_ms)}<small>/ {ms(tiles.latency.p95_ms)}</small></>}
            signal={p95Signal} foot={t("v3.obs.latencyFoot")} />
        </Panel>
        <Panel>
          <Stat label={t("v3.obs.tokens")} value={fmtCompact(tiles.tokens.total)}
            foot={t("v3.obs.tokensFoot", { input: fmtCompact(tiles.tokens.input), output: fmtCompact(tiles.tokens.output), cost: approxCost(tiles.tokens.est_cost_usd) })} />
        </Panel>
      </div>

      <div className="v3-grid c2">
        <Traffic data={data} />
        <Latency data={data} />
      </div>
      <div className="v3-grid c2">
        <Tools data={data} />
        <Tokens data={data} onPricesRefreshed={onPricesRefreshed} />
      </div>
    </>
  );
}
