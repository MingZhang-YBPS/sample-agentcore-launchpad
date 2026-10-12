import { CircleHelp, ExternalLink, Inbox, RefreshCw } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import {
  errorMessage,
  type IntentCluster,
  intentApi,
  type IntentSession,
  type IntentView,
  issueApi,
} from "../../lib/api";
import { useLoad, useV2Toast } from "../hooks";
import { Alert, Button, Card, FilterSelect, Kpi, Modal, PageHeader, Table, Tag, type TagTone } from "../ui";
import "./selfservice/selfservice.css";
import { useAgents } from "./selfservice/useAgents";

const DAYS = ["1", "7", "14", "30"] as const;

const STATUS_TONE: Record<IntentSession["status"], TagTone> = {
  down: "red",
  unanswered: "orange",
  up: "green",
  ok: "gray",
};

function pct(rate: number | null): string {
  return rate == null ? "—" : `${Math.round(rate * 100)}%`;
}

const chatLink = (s: { agent_id: string; session_id: string }) =>
  `/v2/chat?agent=${encodeURIComponent(s.agent_id)}&session=${encodeURIComponent(s.session_id)}`;

/**
 * T33 — the intent view: what were people trying to do, in business language, and where
 * did the agent fail them. Read-only over the ledger's recent sessions; the one action is
 * sending an unanswered question to the issue box.
 */
export function V2Intents() {
  const { t, i18n } = useTranslation();
  const toast = useV2Toast();
  const { agents, label } = useAgents();
  const [agentId, setAgentId] = useState("");
  const [days, setDays] = useState<(typeof DAYS)[number]>("7");
  const [tick, setTick] = useState(0);
  const lang = i18n.resolvedLanguage?.startsWith("zh") ? "zh-CN" : "en";
  const view = useLoad(
    () => intentApi.view({ agent_id: agentId || undefined, days: Number(days), lang, refresh: tick > 0 }),
    `intents:${agentId}:${days}:${lang}:${tick}`,
  );
  const data = view.data;
  const [open, setOpen] = useState<IntentCluster | null>(null);
  const [sent, setSent] = useState<Set<string>>(new Set());

  const toIssueBox = async (s: IntentSession) => {
    if (s.message_id == null) return;
    try {
      await issueApi.open({ agent_id: s.agent_id, session_id: s.session_id, message_id: s.message_id, kind: "unanswered" });
      setSent((prev) => new Set(prev).add(s.session_id));
      toast("success", t("selfService.intents.sent"));
    } catch (err) {
      toast("error", errorMessage(err));
    }
  };

  const worst = [...(data?.clusters ?? [])].sort((a, b) => (b.down_rate ?? -1) - (a.down_rate ?? -1))[0];

  return (
    <>
      <PageHeader
        title={t("selfService.intents.title")}
        desc={t("selfService.intents.desc")}
        end={
          <>
            <FilterSelect
              label={t("selfService.common.agent")}
              value={agentId}
              allLabel={t("selfService.common.allAgents")}
              options={agents.map((a) => ({ value: a.id, label: label(a.id) }))}
              onChange={setAgentId}
            />
            <FilterSelect
              label={t("selfService.intents.window")}
              value={days}
              options={DAYS.map((d) => ({ value: d, label: t("selfService.intents.days", { count: Number(d) }) }))}
              onChange={(v) => setDays(v as (typeof DAYS)[number])}
            />
            <Button onClick={() => setTick((n) => n + 1)} title={t("selfService.intents.regroup")}>
              <RefreshCw size={14} aria-hidden="true" /> {t("selfService.intents.regroup")}
            </Button>
          </>
        }
      />
      {view.error && <Alert tone="error">{view.error}</Alert>}
      {data?.source === "fallback" && data.clusters.length > 0 && (
        <Alert tone="warn">{t("selfService.intents.fallback")}</Alert>
      )}
      <div className="v2-kpis">
        <Kpi label={t("selfService.intents.kpiSessions")} value={data?.sessions_considered ?? "—"} />
        <Kpi label={t("selfService.intents.kpiIntents")} value={data?.clusters.length ?? "—"} />
        <Kpi
          label={t("selfService.intents.kpiUnanswered")}
          value={data?.unanswered.length ?? "—"}
          tone={data?.unanswered.length ? "bad" : undefined}
        />
        <Kpi label={t("selfService.intents.kpiWorst")} value={worst?.down_rate != null ? worst.label : "—"} sub={worst?.down_rate != null ? pct(worst.down_rate) : undefined} />
      </div>

      <Card
        title={
          <>
            <CircleHelp size={15} aria-hidden="true" /> {t("selfService.intents.unansweredTitle")}
          </>
        }
        sub={t("selfService.intents.unansweredDesc")}
      >
        <Table<IntentView["unanswered"][number]>
          rows={data?.unanswered ?? []}
          rowKey={(r) => r.session_id}
          loading={view.loading}
          empty={t("selfService.intents.unansweredEmpty")}
          testId="v2-intents-unanswered"
          columns={[
            { key: "q", title: t("selfService.common.question"), render: (r) => <span className="clip" title={r.question}>{r.question}</span> },
            { key: "a", title: t("selfService.common.answer"), render: (r) => <span className="clip" title={r.answer}>{r.answer || "—"}</span> },
            { key: "c", title: t("selfService.intents.colIntent"), render: (r) => r.cluster ?? "—" },
            {
              key: "act",
              title: t("v2.common.actions"),
              className: "right",
              render: (r) => (
                <>
                  <Button
                    size="sm"
                    disabled={r.message_id == null || sent.has(r.session_id)}
                    onClick={() => void toIssueBox(r)}
                    testId="v2-intents-to-issue"
                  >
                    <Inbox size={13} aria-hidden="true" />{" "}
                    {sent.has(r.session_id) ? t("selfService.intents.inBox") : t("selfService.intents.toIssueBox")}
                  </Button>{" "}
                  <Link to={chatLink(r)}>{t("selfService.common.openSession")}</Link>
                </>
              ),
            },
          ]}
        />
      </Card>

      <Card title={t("selfService.intents.tableTitle")} sub={data?.source === "model" ? t("selfService.intents.byModel") : undefined}>
        <Table<IntentCluster>
          rows={data?.clusters ?? []}
          rowKey={(r) => r.id}
          loading={view.loading}
          empty={t("selfService.intents.empty")}
          testId="v2-intents-table"
          columns={[
            { key: "label", title: t("selfService.intents.colIntent"), render: (r) => <b>{r.label}</b> },
            { key: "vol", title: t("selfService.intents.colVolume"), className: "nowrap", render: (r) => r.volume },
            {
              key: "down",
              title: t("selfService.intents.colDownRate"),
              className: "nowrap",
              render: (r) => (
                <span title={t("selfService.intents.downRateHint", { down: r.thumbs_down, rated: r.rated })}>
                  {pct(r.down_rate)}
                  {r.rated > 0 && <span className="muted"> ({r.thumbs_down}/{r.rated})</span>}
                </span>
              ),
            },
            {
              key: "un",
              title: t("selfService.intents.colUnanswered"),
              className: "nowrap",
              render: (r) => (r.unanswered ? <Tag tone="orange">{r.unanswered}</Tag> : "0"),
            },
            {
              key: "s",
              title: t("selfService.intents.colSessions"),
              className: "right",
              render: (r) => (
                <Button size="sm" onClick={() => setOpen(r)}>
                  {t("selfService.intents.viewSessions", { count: r.volume })}
                </Button>
              ),
            },
          ]}
        />
      </Card>

      <Modal open={open !== null} title={open?.label ?? ""} wide onClose={() => setOpen(null)}>
        <Table<IntentSession>
          rows={open?.sessions ?? []}
          rowKey={(r) => r.session_id}
          columns={[
            { key: "st", title: "", render: (r) => <Tag tone={STATUS_TONE[r.status]}>{t(`selfService.intents.status.${r.status}`)}</Tag> },
            { key: "q", title: t("selfService.common.question"), render: (r) => <span className="clip" title={r.question}>{r.question}</span> },
            { key: "a", title: t("selfService.common.answer"), render: (r) => <span className="clip" title={r.answer}>{r.answer || "—"}</span> },
            {
              key: "o",
              title: "",
              render: (r) => (
                <Link to={chatLink(r)} title={t("selfService.common.openSession")}>
                  <ExternalLink size={13} aria-hidden="true" />
                </Link>
              ),
            },
          ]}
        />
      </Modal>
    </>
  );
}
