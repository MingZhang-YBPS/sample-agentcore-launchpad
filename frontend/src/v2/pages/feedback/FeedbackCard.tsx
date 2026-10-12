import { DatabaseZap, ThumbsDown, ThumbsUp } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { feedbackApi, type FeedbackItem, type V2Range } from "../../../lib/api";
import { fmtTime } from "../../format";
import { useLoad } from "../../hooks";
import { Button, Card, Kpi, Table, Tag } from "../../ui";
import { AddToDatasetModal } from "../data/AddToDataset";

/**
 * User feedback (T15): thumbs from the console chat and from share pages. A
 * thumbs-down is a bad case — its session goes straight into the existing
 * "add to dataset" flow (`POST /api/eval/datasets/from-sessions`), so no
 * dataset-building logic is duplicated here.
 */
export function FeedbackCard({ range }: { range: V2Range }) {
  const { t } = useTranslation();
  const feedback = useLoad(() => feedbackApi.list({ verdict: "down", limit: 50 }), "feedback:down");
  const [adding, setAdding] = useState(false);
  const data = feedback.data;
  const sessions = data?.down_session_ids ?? [];

  return (
    <Card
      title={t("feedbackCard.title")}
      sub={t("feedbackCard.desc")}
      end={
        <Button
          disabled={sessions.length === 0}
          onClick={() => setAdding(true)}
          testId="v2-feedback-to-dataset"
        >
          <DatabaseZap size={14} aria-hidden="true" />
          {t("feedbackCard.toDataset", { count: sessions.length })}
        </Button>
      }
    >
      <div className="v2-kpis">
        <Kpi label={t("feedbackCard.up")} value={data?.counts.up ?? "—"} tone="good" />
        <Kpi label={t("feedbackCard.down")} value={data?.counts.down ?? "—"} tone={data?.counts.down ? "bad" : undefined} />
      </div>
      <Table<FeedbackItem>
        rows={data?.items ?? []}
        rowKey={(r) => r.id}
        loading={feedback.loading}
        error={feedback.error}
        onRetry={feedback.reload}
        empty={t("feedbackCard.empty")}
        testId="v2-feedback-table"
        columns={[
          {
            key: "verdict",
            title: t("feedbackCard.colVerdict"),
            render: (r) => (r.verdict === "down" ? <ThumbsDown size={14} /> : <ThumbsUp size={14} />),
          },
          { key: "time", title: t("v2.insights.colTime"), className: "nowrap", render: (r) => fmtTime(r.updated_at) },
          { key: "agent", title: "Agent", render: (r) => r.agent_name },
          { key: "q", title: t("feedbackCard.colQuestion"), render: (r) => <span className="clip" title={r.question}>{r.question || "—"}</span> },
          { key: "a", title: t("feedbackCard.colAnswer"), render: (r) => <span className="clip" title={r.answer}>{r.answer || "—"}</span> },
          { key: "c", title: t("feedbackCard.colComment"), render: (r) => r.comment ?? "—" },
          {
            key: "src",
            title: t("feedbackCard.colSource"),
            render: (r) => <Tag tone="outline">{t(`feedbackCard.source.${r.source}`)}</Tag>,
          },
          {
            key: "open",
            title: t("v2.common.actions"),
            className: "right",
            render: (r) => (
              <Link to={`/v2/chat?agent=${encodeURIComponent(r.agent_id)}&session=${encodeURIComponent(r.session_id)}`}>
                {t("feedbackCard.openSession")}
              </Link>
            ),
          },
        ]}
      />
      <AddToDatasetModal open={adding} sessionIds={sessions} range={range} onClose={() => setAdding(false)} />
    </Card>
  );
}
