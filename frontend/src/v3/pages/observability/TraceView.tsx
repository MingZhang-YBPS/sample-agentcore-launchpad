import { ArrowLeft, MessagesSquare, RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, ApiError, type ObsSpanMessage, type ObsSpanNode, type ObsTraceDetail } from "../../../lib/api";
import { fmtNumber } from "../../../v2/format";
import { approxCost, shortId, TRACE_ID_RE } from "../../../v2/pages/observability/common";
import { ago, ms } from "../../format";
import { useLoad } from "../../hooks";
import { Btn, Chip, Empty, Notice, PageHead, Panel, Skeleton, Stat } from "../../ui";
import { CopyId, TraceStatus } from "./shared";

type Category = ObsSpanNode["category"];
const CATEGORIES: Category[] = ["llm", "tool", "memory", "gateway", "http", "agent", "other"];

function flatten(nodes: ObsSpanNode[], out: ObsSpanNode[] = []): ObsSpanNode[] {
  for (const node of nodes) {
    out.push(node);
    flatten(node.children, out);
  }
  return out;
}

function Messages({ label, messages }: { label: string; messages: ObsSpanMessage[] }) {
  return (
    <>
      <div className="v3-obs-subtitle">{label}</div>
      <div className="v3-obs-msgs">
        {messages.map((m, i) => (
          <div className="v3-obs-msg" key={i}>
            <div className="role">{m.role ?? "—"}{m.finish_reason != null && ` · ${m.finish_reason}`}</div>
            {m.blocks.map((b, j) =>
              b.type === "tool_use" ? (
                <div className="tool" key={j}>⇄ {b.name ?? "tool"}({b.input})</div>
              ) : b.type === "tool_result" ? (
                <div className="tool" key={j}>✓ {b.status ?? "result"} · {b.text}</div>
              ) : (
                <div className="text" key={j} style={b.type === "other" ? { color: "var(--v3-text-3)" } : undefined}>{b.text}</div>
              ),
            )}
          </div>
        ))}
      </div>
    </>
  );
}

function SpanPanel({ span, detail }: { span: ObsSpanNode; detail: ObsTraceDetail }) {
  const { t } = useTranslation();
  const [all, setAll] = useState(false);
  const flat = detail.spans.find((s) => s.span_id != null && s.span_id === span.span_id);
  const attributes = flat?.attributes ?? {};
  const messages = flat?.messages ?? null;
  const attrs = Object.entries(attributes);
  const toolAttrs = attrs.filter(([k]) => k.startsWith("gen_ai.tool.") && k !== "gen_ai.tool.name");
  const operation = (attributes["gen_ai.operation.name"] as string | undefined) ?? span.name;
  const provider = (attributes["gen_ai.system"] as string | undefined) ?? (attributes["gen_ai.provider.name"] as string | undefined) ?? null;
  const finish = Array.isArray(span.finish_reason) ? span.finish_reason.join(", ") : span.finish_reason;
  const httpStatus = (attributes["http.response.status_code"] as number | undefined) ?? (attributes["http.status_code"] as number | undefined) ?? null;
  const failed = span.status === "ERROR";
  const shown = all ? attrs : attrs.slice(0, 12);
  return (
    <Panel
      title={t("v3.obs.span")}
      signal={failed ? "act" : undefined}
      end={<span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}><i className="v3-obs-dot" data-cat={span.category} />{t(`v2.spanCategory.${span.category}`)}</span>}
    >
      <p style={{ margin: "0 0 12px", color: "var(--v3-text-2)" }}>{operation} · {span.kind ?? "—"}</p>
      <dl className="v3-kv">
        <dt>{t("v3.obs.spanName")}</dt><dd className="mono">{span.name}</dd>
        {span.model != null && <><dt>{t("v3.obs.model")}</dt><dd className="mono">{span.model}</dd></>}
        {provider != null && <><dt>{t("v3.obs.provider")}</dt><dd>{provider}</dd></>}
        {finish != null && <><dt>{t("v3.obs.finish")}</dt><dd>{finish}</dd></>}
        <dt>{t("v3.obs.startOffset")}</dt><dd className="mono">+{ms(span.start_offset_ms)}</dd>
        <dt>{t("v3.obs.duration")}</dt><dd className="mono">{ms(span.duration_ms)}</dd>
        <dt>{t("v3.obs.statusLabel")}</dt>
        <dd><Chip s={failed ? "act" : "ok"}>{span.status}{httpStatus != null ? ` · http ${httpStatus}` : ""}</Chip></dd>
        {span.tool_name != null && <><dt>{t("v3.obs.tool")}</dt><dd className="mono">{span.tool_name}</dd></>}
        {toolAttrs.slice(0, 4).map(([k, v]) => (
          <span key={k} style={{ display: "contents" }}><dt>{k.replace("gen_ai.tool.", "")}</dt><dd className="mono">{String(v).slice(0, 120)}</dd></span>
        ))}
      </dl>
      {span.tokens != null && (
        <>
          <div className="v3-obs-subtitle">{t("v3.obs.tokens")}</div>
          <dl className="v3-kv">
            <dt>{t("v3.obs.input")}</dt><dd className="mono">{fmtNumber(span.tokens.input)}</dd>
            <dt>{t("v3.obs.output")}</dt><dd className="mono">{fmtNumber(span.tokens.output)}</dd>
            <dt>{t("v3.obs.cacheRw")}</dt><dd className="mono">{fmtNumber(span.tokens.cache_read)} / {fmtNumber(span.tokens.cache_write)}</dd>
            <dt>{t("v3.obs.cost")}</dt><dd className="mono">{approxCost(span.est_cost_usd)}</dd>
          </dl>
        </>
      )}
      {messages?.input != null && messages.input.length > 0 && <Messages label={t("v3.obs.inputMessages", { count: messages.input.length })} messages={messages.input} />}
      {messages?.output != null && messages.output.length > 0 && <Messages label={t("v3.obs.outputMessages", { count: messages.output.length })} messages={messages.output} />}
      <div className="v3-obs-subtitle">
        {t("v3.obs.attributes", { count: attrs.length })}
        {attrs.length > 12 && (
          <button type="button" className="v3-obs-link" style={{ letterSpacing: 0, textTransform: "none", fontFamily: "var(--v3-body)" }} onClick={() => setAll((v) => !v)}>
            {all ? t("v3.obs.collapse") : t("v3.obs.showAll")}
          </button>
        )}
      </div>
      {attrs.length === 0 ? (
        <p style={{ margin: 0, color: "var(--v3-text-3)" }}>{t("v3.obs.noAttributes")}</p>
      ) : (
        <pre className="v3-pre">{JSON.stringify(Object.fromEntries(shown), null, 2)}</pre>
      )}
    </Panel>
  );
}

export function TraceView({
  traceId,
  range,
  onBack,
  onOpenSession,
}: {
  traceId: string;
  range: string;
  onBack: () => void;
  onOpenSession: (id: string) => void;
}) {
  const { t } = useTranslation();
  const [force, setForce] = useState(0);
  const load = useLoad<ObsTraceDetail | "invalid">(
    () =>
      TRACE_ID_RE.test(traceId)
        ? api.obsTrace(traceId, range, force > 0).catch((err: unknown) => {
            if (err instanceof ApiError && err.code === "validation.invalid_request") return "invalid" as const;
            throw err;
          })
        : Promise.resolve("invalid" as const),
    `v3-obs-trace:${traceId}:${range}:${force}`,
  );
  const [selected, setSelected] = useState<string | null>(null);
  const detail = load.data && load.data !== "invalid" ? load.data : null;
  const rows = useMemo(() => (detail ? flatten(detail.tree) : []), [detail]);
  // first failing span, else the first model call, else the root
  const span =
    rows.find((r) => r.span_id === selected) ?? rows.find((r) => r.status === "ERROR") ?? rows.find((r) => r.category === "llm") ?? rows[0] ?? null;
  const meta = detail?.meta;
  const notFound = load.data === "invalid" || (meta != null && meta.span_count === 0);
  const errorSpans = rows.filter((r) => r.status === "ERROR").length;

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <div>
        <button type="button" className="v3-btn ghost sm" onClick={onBack}><ArrowLeft size={14} /> {t("v3.obs.tab.traces")}</button>
      </div>
      <PageHead
        eyebrow={`${t("v3.obs.trace")} · ${shortId(traceId, 16)}`}
        title={
          <span style={{ display: "inline-flex", gap: 12, alignItems: "center" }}>
            {meta?.root_operation ?? t("v3.obs.trace")}
            {meta && !notFound && <TraceStatus status={meta.status} durationMs={meta.duration_ms} />}
          </span>
        }
        sub={meta && !notFound ? `${meta.agent || "—"} · ${ago(meta.start)}${meta.service ? ` · ${meta.service}` : ""}` : undefined}
        end={
          <>
            {meta?.session_id && (
              <Btn onClick={() => onOpenSession(meta.session_id as string)}><MessagesSquare size={14} /> {t("v3.obs.openSession")}</Btn>
            )}
            <Btn kind="ghost" disabled={load.loading} onClick={() => setForce((n) => n + 1)} title={t("v3.obs.refresh")}><RefreshCw size={14} /></Btn>
          </>
        }
      />
      {load.loading && !load.data ? (
        <Panel><Skeleton rows={6} /></Panel>
      ) : load.error && !load.data ? (
        <Notice s="act">{t("obs.loadFailed", { msg: load.error })}</Notice>
      ) : notFound ? (
        <Panel><Empty title={t("v3.obs.traceNotFound")} /></Panel>
      ) : detail && meta ? (
        <>
          {load.error && <Notice s="act">{t("obs.loadFailed", { msg: load.error })}</Notice>}
          <div className="v3-grid c4">
            <Panel signal={meta.status === "error" ? "act" : "ok"}><Stat label={t("v3.obs.duration")} value={ms(meta.duration_ms)} foot={<CopyId id={detail.trace_id} chars={24} />} /></Panel>
            <Panel signal={errorSpans ? "act" : undefined}>
              <Stat label={t("v3.obs.spansLlm")} value={`${meta.span_count} / ${meta.llm_count}`}
                foot={errorSpans ? t("v3.obs.errorSpans", { count: errorSpans }) : t("v3.obs.noErrorSpans")} />
            </Panel>
            <Panel><Stat label={t("v3.obs.tokens")} value={fmtNumber(meta.tokens.total)} foot={t("v3.obs.inOut", { input: fmtNumber(meta.tokens.input), output: fmtNumber(meta.tokens.output) })} /></Panel>
            <Panel><Stat label={t("v3.obs.cost")} value={approxCost(meta.est_cost_usd)} foot={t("obs.charts.priceNote")} /></Panel>
          </div>
          <div className="v3-obs-split">
            <Panel title={t("v3.obs.waterfall")} end={<span>{t("v3.obs.waterfallSub", { count: rows.length, dur: ms(meta.duration_ms) })}</span>}>
              <div className="v3-obs-legend" style={{ marginTop: 0, marginBottom: 12 }}>
                {CATEGORIES.filter((c) => rows.some((r) => r.category === c)).map((c) => (
                  <span key={c}><i className="v3-obs-dot" data-cat={c} />{t(`v2.spanCategory.${c}`)}</span>
                ))}
              </div>
              <div className="v3-obs-wf" role="list">
                <div className="h">{t("v3.obs.spanName")}</div>
                <div className="h">{t("v3.obs.timeline")}</div>
                <div className="h right">{t("v3.obs.duration")}</div>
                {rows.map((row, i) => {
                  const pick = () => setSelected(row.span_id);
                  const on = span?.span_id === row.span_id;
                  const err = row.status === "ERROR";
                  return (
                    <div key={row.span_id ?? `${row.name}:${i}`} className={on ? "r on" : "r"} role="listitem" tabIndex={0}
                      onClick={pick} onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && pick()}>
                      <span className="nm" style={{ paddingLeft: 8 + Math.min(row.depth, 8) * 14 }} title={row.name}>
                        <i className="v3-obs-dot" data-cat={row.category} />
                        <span className="t" style={err ? { color: "var(--v3-act)" } : undefined}>{row.tool_name ?? row.name}</span>
                      </span>
                      <span className="lane">
                        <span className="track">
                          <span className={err ? "bar err" : "bar"} data-cat={row.category}
                            style={{ left: `${row.offset_pct}%`, width: `${Math.max(row.width_pct, 0.4)}%` }} />
                        </span>
                      </span>
                      <span className="dur">{ms(row.duration_ms)}</span>
                    </div>
                  );
                })}
              </div>
            </Panel>
            <div className="v3-obs-side">{span && <SpanPanel key={span.span_id ?? ""} span={span} detail={detail} />}</div>
          </div>
        </>
      ) : null}
    </div>
  );
}
