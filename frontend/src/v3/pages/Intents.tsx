import "./insights.css";

import { Inbox, RefreshCw } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { errorMessage, type IntentCluster, intentApi, type IntentSession, issueApi } from "../../lib/api";
import { useAgents } from "../../v2/pages/selfservice/useAgents";
import { useWorkspace } from "../../workspace/workspace-context";
import { useLoad, useToast } from "../hooks";
import { Btn, Chip, Dialog, Empty, Filters, Lamp, Notice, PageHead, Panel, type Signal, Skeleton, Stat } from "../ui";

const DAYS = ["1", "7", "14", "30"] as const;
type Days = (typeof DAYS)[number];

const STATUS_SIGNAL: Record<IntentSession["status"], Signal> = { down: "act", unanswered: "wait", up: "ok", ok: "off" };

const pct = (rate: number | null) => (rate == null ? "—" : `${Math.round(rate * 100)}%`);

/** The thumbs-down rate of an intent as a signal: a third or more is a problem. */
function downSignal(rate: number | null): Signal {
  if (rate == null) return "off";
  return rate >= 0.3 ? "act" : rate > 0 ? "wait" : "ok";
}

const chatLink = (s: { agent_id: string; session_id: string }) =>
  `/v3/chat?agent=${encodeURIComponent(s.agent_id)}&session=${encodeURIComponent(s.session_id)}`;

/**
 * 意图视图 — what people were trying to do, in business language, and where the
 * agent failed them: unanswered questions first (each one a click from the issue
 * box), then every intent with its volume and thumbs-down rate. Read-only over the
 * ledger's recent sessions; the one action is sending a question to the issue box.
 */
export function V3Intents() {
  const { t, i18n } = useTranslation();
  const toast = useToast();
  const { current } = useWorkspace();
  const { agents, label } = useAgents();
  const [agentId, setAgentId] = useState("");
  const [days, setDays] = useState<Days>("7");
  const [tick, setTick] = useState(0);
  const lang = i18n.resolvedLanguage?.startsWith("zh") ? "zh-CN" : "en";
  const view = useLoad(
    () => intentApi.view({ agent_id: agentId || undefined, days: Number(days), lang, refresh: tick > 0 }),
    `v3-intents:${current?.id ?? ""}:${agentId}:${days}:${lang}:${tick}`,
  );
  const data = view.data;
  const [open, setOpen] = useState<IntentCluster | null>(null);
  const [sent, setSent] = useState<Set<string>>(new Set());

  const toIssueBox = async (s: IntentSession) => {
    if (s.message_id == null) return;
    try {
      await issueApi.open({ agent_id: s.agent_id, session_id: s.session_id, message_id: s.message_id, kind: "unanswered" });
      setSent((prev) => new Set(prev).add(s.session_id));
      toast("ok", t("selfService.intents.sent"));
    } catch (err) {
      toast("act", errorMessage(err));
    }
  };

  const clusters = data?.clusters ?? [];
  const unanswered = data?.unanswered ?? [];
  const worst = [...clusters].sort((a, b) => (b.down_rate ?? -1) - (a.down_rate ?? -1))[0];
  const maxVolume = Math.max(1, ...clusters.map((c) => c.volume));

  const headline = !data
    ? t("selfService.intents.title")
    : unanswered.length > 0
      ? t("v3.intents.headlineUnanswered", { count: unanswered.length })
      : clusters.length === 0
        ? t("v3.intents.headlineEmpty")
        : t("v3.intents.headline", { count: clusters.length });

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={`${t("v3.intents.eyebrow")} · ${t("v3.intents.days", { count: Number(days) })}`}
        title={headline}
        sub={t("selfService.intents.desc")}
        end={
          <>
            <select className="v3-select" style={{ width: 200 }} value={agentId} onChange={(e) => setAgentId(e.target.value)}
              aria-label={t("selfService.common.agent")}>
              <option value="">{t("selfService.common.allAgents")}</option>
              {agents.map((a) => <option key={a.id} value={a.id}>{label(a.id)}</option>)}
            </select>
            <span className="v3-ins-nowrap">
              <Filters value={days} onChange={setDays}
                options={DAYS.map((d) => ({ value: d, label: t("v3.intents.days", { count: Number(d) }) }))} />
            </span>
            <Btn onClick={() => setTick((n) => n + 1)} title={t("selfService.intents.regroup")}>
              <RefreshCw size={14} /> {t("selfService.intents.regroup")}
            </Btn>
          </>
        }
      />

      {view.error && <Notice s="act">{view.error}</Notice>}
      {data?.source === "fallback" && clusters.length > 0 && <Notice s="wait">{t("selfService.intents.fallback")}</Notice>}

      <div className="v3-grid c4">
        <Panel><Stat label={t("selfService.intents.kpiSessions")} value={data?.sessions_considered ?? "—"} /></Panel>
        <Panel><Stat label={t("selfService.intents.kpiIntents")} value={data ? clusters.length : "—"}
          foot={data?.source === "model" ? t("selfService.intents.byModel") : undefined} /></Panel>
        <Panel signal={unanswered.length ? "act" : undefined}>
          <Stat label={t("selfService.intents.kpiUnanswered")} value={data ? unanswered.length : "—"} signal={unanswered.length ? "act" : undefined} />
        </Panel>
        <Panel signal={worst?.down_rate != null ? downSignal(worst.down_rate) : undefined}>
          <Stat label={t("selfService.intents.kpiWorst")} value={worst?.down_rate != null ? <span className="v3-ins-worst">{worst.label}</span> : "—"}
            foot={worst?.down_rate != null ? t("v3.intents.downRate", { rate: pct(worst.down_rate) }) : undefined} />
        </Panel>
      </div>

      <Panel title={t("selfService.intents.unansweredTitle")} signal={unanswered.length ? "act" : undefined} flush
        end={<span className="mono">{unanswered.length}</span>}>
        <p className="v3-ins-note" style={{ padding: "0 20px" }}>{t("selfService.intents.unansweredDesc")}</p>
        {view.loading && !data ? (
          <div style={{ padding: 20 }}><Skeleton rows={3} /></div>
        ) : unanswered.length === 0 ? (
          <Empty title={t("selfService.intents.unansweredEmpty")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("selfService.common.question")}</th>
                <th>{t("selfService.common.answer")}</th>
                <th>{t("selfService.intents.colIntent")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {unanswered.map((r) => (
                <tr key={r.session_id}>
                  <td style={{ width: 30 }}><Lamp s="wait" /></td>
                  <td><span className="v3-ins-clip" title={r.question}>{r.question}</span></td>
                  <td><span className="v3-ins-clip" title={r.answer} style={{ color: "var(--v3-text-2)" }}>{r.answer || "—"}</span></td>
                  <td>{r.cluster ? <Chip>{r.cluster}</Chip> : <span style={{ color: "var(--v3-text-3)" }}>—</span>}</td>
                  <td style={{ width: 1, whiteSpace: "nowrap" }}>
                    <span style={{ display: "inline-flex", gap: 6 }}>
                      <Btn size="sm" disabled={r.message_id == null || sent.has(r.session_id)} onClick={() => void toIssueBox(r)}>
                        <Inbox size={13} /> {sent.has(r.session_id) ? t("selfService.intents.inBox") : t("selfService.intents.toIssueBox")}
                      </Btn>
                      <Link to={chatLink(r)} className="v3-btn sm ghost">{t("selfService.common.openSession")}</Link>
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>

      <Panel title={t("selfService.intents.tableTitle")} flush end={<span className="mono">{clusters.length}</span>}>
        {view.loading && !data ? (
          <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
        ) : clusters.length === 0 ? (
          <Empty title={t("selfService.intents.empty")} />
        ) : (
          <div>
            <div className="v3-ins-intent" style={{ borderTop: 0, paddingTop: 0 }}>
              <span className="h">{t("selfService.intents.colIntent")}</span>
              <span className="h">{t("selfService.intents.colVolume")}</span>
              <span className="h" style={{ textAlign: "right" }}>{t("selfService.intents.colDownRate")}</span>
              <span className="h" style={{ textAlign: "right" }}>{t("selfService.intents.colUnanswered")}</span>
              <span />
            </div>
            {clusters.map((c) => (
              <div key={c.id} className="v3-ins-intent">
                <span style={{ display: "inline-flex", gap: 10, alignItems: "center", minWidth: 0 }}>
                  <Lamp s={downSignal(c.down_rate)} />
                  <b style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={c.label}>{c.label}</b>
                </span>
                <span style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) 40px", gap: 10, alignItems: "center" }}>
                  <span className="track"><i style={{ width: `${Math.round((c.volume / maxVolume) * 100)}%` }} /></span>
                  <span className="num">{c.volume}</span>
                </span>
                <span className="num" title={t("selfService.intents.downRateHint", { down: c.thumbs_down, rated: c.rated })}>
                  {c.down_rate == null ? "—" : <Chip s={downSignal(c.down_rate)}>{pct(c.down_rate)}</Chip>}
                  {c.rated > 0 && <small style={{ marginLeft: 6, color: "var(--v3-text-3)" }}>{c.thumbs_down}/{c.rated}</small>}
                </span>
                <span className="num">{c.unanswered ? <Chip s="wait">{c.unanswered}</Chip> : "0"}</span>
                <Btn size="sm" kind="ghost" onClick={() => setOpen(c)}>{t("selfService.intents.viewSessions", { count: c.volume })}</Btn>
              </div>
            ))}
          </div>
        )}
      </Panel>

      {open && (
        <div className="v3-ins-layer">
        <Dialog wide title={open.label} onClose={() => setOpen(null)} foot={<Btn kind="ghost" onClick={() => setOpen(null)}>{t("v3.intents.close")}</Btn>}>
          <div className="v3-ins-dialog" style={{ gap: 6 }}>
            {open.sessions.map((s) => (
              <div key={s.session_id} className="v3-ins-card" style={{ gridTemplateColumns: "auto minmax(0, 1fr) auto", alignItems: "start", gap: 12 }}>
                <Chip s={STATUS_SIGNAL[s.status] === "off" ? undefined : STATUS_SIGNAL[s.status]}>{t(`selfService.intents.status.${s.status}`)}</Chip>
                <span style={{ display: "grid", gap: 4, minWidth: 0 }}>
                  <b style={{ fontWeight: 500 }}>{s.question}</b>
                  <span style={{ color: "var(--v3-text-2)" }}>{s.answer || "—"}</span>
                </span>
                <Link to={chatLink(s)} className="v3-btn sm ghost">{t("selfService.common.openSession")}</Link>
              </div>
            ))}
          </div>
        </Dialog>
        </div>
      )}
    </div>
  );
}
