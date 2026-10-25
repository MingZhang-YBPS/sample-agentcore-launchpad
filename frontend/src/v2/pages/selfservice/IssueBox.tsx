import { BookOpenCheck, Database, FilePlus2, MessageSquareQuote, RefreshCw } from "lucide-react";
import type { TFunction } from "i18next";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import {
  errorMessage,
  type IssueDetail,
  type IssueInfo,
  issueApi,
  type IssueStatus,
  ruleApi,
} from "../../../lib/api";
import { fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Drawer, Field, FilterSelect, Select, Kpi, Table, Tag, type TagTone } from "../../ui";
import { AddToDatasetModal } from "../data/AddToDataset";
import "./selfservice.css";
import { useAgents } from "./useAgents";

const STATUS_TONE: Record<IssueStatus, TagTone> = { open: "orange", fixed: "green", wont_fix: "gray" };

function hours(value: number | null | undefined, t: TFunction): string {
  if (value == null) return "—";
  return value < 48 ? t("selfService.issues.hours", { count: Math.round(value * 10) / 10 }) : t("selfService.issues.days", { count: Math.round(value / 24) });
}

/**
 * T36 — the issue box. A thumbs-down, a reviewer's correction or an unanswered question
 * lands here; the owner opens it, sees the session, picks a fix and marks it resolved.
 * The fixes call the existing endpoints (curated answer, `from-sessions` dataset, the
 * knowledge-base page); this component never builds a dataset or uploads a document.
 */
export function IssueBox() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const { agents, label } = useAgents();
  const [status, setStatus] = useState<IssueStatus | "">("open");
  const [agentId, setAgentId] = useState("");
  const [tick, setTick] = useState(0);
  const [openId, setOpenId] = useState<string | null>(null);

  // backfill the thumbs-down votes cast before the box existed (idempotent)
  useEffect(() => {
    void issueApi.sync().then(() => setTick((n) => n + 1), () => undefined);
  }, []);

  const list = useLoad(
    () => issueApi.list({ status: status || undefined, agent_id: agentId || undefined }),
    `issues:${status}:${agentId}:${tick}`,
  );
  const summary = list.data?.summary;
  const reload = () => setTick((n) => n + 1);

  return (
    <>
      <div className="v2-kpis">
        <Kpi label={t("selfService.issues.kpiOpen")} value={summary?.open ?? "—"} tone={summary?.open ? "bad" : undefined} testId="v2-issues-open" />
        <Kpi label={t("selfService.issues.kpiFixed")} value={summary?.fixed ?? "—"} tone="good" />
        <Kpi label={t("selfService.issues.kpiWontFix")} value={summary?.wont_fix ?? "—"} />
        <Kpi label={t("selfService.issues.kpiClose")} value={hours(summary?.median_hours_to_close, t)} sub={t("selfService.issues.kpiCloseHint")} testId="v2-issues-close-time" />
        <Kpi label={t("selfService.issues.kpiOldest")} value={hours(summary?.oldest_open_hours, t)} />
      </div>
      <Card
        title={t("selfService.issues.listTitle")}
        end={
          <>
            <FilterSelect
              label={t("selfService.issues.status")}
              value={status}
              allLabel={t("selfService.common.all")}
              options={(["open", "fixed", "wont_fix"] as const).map((s) => ({ value: s, label: t(`selfService.issues.state.${s}`) }))}
              onChange={(v) => setStatus(v as IssueStatus | "")}
            />
            <FilterSelect
              label={t("selfService.common.agent")}
              value={agentId}
              allLabel={t("selfService.common.allAgents")}
              options={agents.map((a) => ({ value: a.id, label: label(a.id) }))}
              onChange={setAgentId}
            />
            <Button onClick={reload} title={t("v2.common.refresh")}>
              <RefreshCw size={14} aria-hidden="true" />
            </Button>
          </>
        }
      >
        <Table<IssueInfo>
          rows={list.data?.items ?? []}
          rowKey={(r) => r.id}
          loading={list.loading}
          error={list.error}
          onRetry={reload}
          empty={t("selfService.issues.empty")}
          testId="v2-issues-table"
          columns={[
            { key: "st", title: t("selfService.issues.status"), render: (r) => <Tag tone={STATUS_TONE[r.status]}>{t(`selfService.issues.state.${r.status}`)}</Tag> },
            { key: "kind", title: t("selfService.issues.kind"), render: (r) => <Tag tone="outline">{t(`selfService.issues.kindName.${r.kind}`)}</Tag> },
            { key: "agent", title: t("selfService.common.agent"), render: (r) => r.agent_name },
            { key: "q", title: t("selfService.common.question"), render: (r) => <span className="clip" title={r.question}>{r.question || "—"}</span> },
            { key: "a", title: t("selfService.common.answer"), render: (r) => <span className="clip" title={r.answer}>{r.answer || "—"}</span> },
            { key: "at", title: t("selfService.issues.opened"), className: "nowrap", render: (r) => fmtTime(r.created_at) },
            {
              key: "act",
              title: t("v2.common.actions"),
              className: "right",
              render: (r) => (
                <Button size="sm" onClick={() => setOpenId(r.id)} testId="v2-issue-open">
                  {r.status === "open" ? t("selfService.issues.work") : t("selfService.issues.view")}
                </Button>
              ),
            },
          ]}
        />
      </Card>
      {openId && (
        <IssueDrawer
          id={openId}
          onClose={() => setOpenId(null)}
          onChanged={reload}
          onToast={(tone, text) => toast(tone, text)}
        />
      )}
    </>
  );
}

function IssueDrawer({
  id,
  onClose,
  onChanged,
  onToast,
}: {
  id: string;
  onClose: () => void;
  onChanged: () => void;
  onToast: (tone: "success" | "error", text: string) => void;
}) {
  const { t } = useTranslation();
  const [nonce, setNonce] = useState(0);
  const detail = useLoad<IssueDetail>(() => issueApi.get(id), `issue:${id}:${nonce}`);
  const issue = detail.data;
  const [mode, setMode] = useState<"rule" | "kb" | "dataset" | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pattern, setPattern] = useState("");
  const [answer, setAnswer] = useState("");
  const [match, setMatch] = useState<"exact" | "contains">("exact");
  const [kbId, setKbId] = useState("");
  const [note, setNote] = useState("");
  const [testResult, setTestResult] = useState<string | null>(null);

  useEffect(() => {
    if (issue) {
      setPattern(issue.question);
      setAnswer(issue.correction ?? "");
    }
    // seed the form once per issue, not on every reload after a fix
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [issue?.id]);

  const refresh = () => {
    setNonce((n) => n + 1);
    onChanged();
  };
  const run = async (fn: () => Promise<unknown>, okText: string) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onToast("success", okText);
      refresh();
      return true;
    } catch (err) {
      setError(errorMessage(err));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const open = issue?.status === "open";
  const ruleFix = issue?.fixes.find((f) => f.action === "rule");

  return (
    <Drawer open title={issue ? t("selfService.issues.drawerTitle", { agent: issue.agent_name }) : ""} onClose={onClose}>
      {detail.loading && !issue && <p>{t("v2.common.loading")}</p>}
      {detail.error && <Alert tone="error">{detail.error}</Alert>}
      {issue && (
        <div className="v2-stack">
          <div>
            <Tag tone={STATUS_TONE[issue.status]}>{t(`selfService.issues.state.${issue.status}`)}</Tag>{" "}
            <Tag tone="outline">{t(`selfService.issues.kindName.${issue.kind}`)}</Tag>{" "}
            <Link to={`/v2/chat?agent=${encodeURIComponent(issue.agent_id)}&session=${encodeURIComponent(issue.session_id)}`}>
              {t("selfService.common.openSession")}
            </Link>
          </div>
          <Card title={t("selfService.issues.session")} flush>
            <div className="issue-transcript" data-testid="v2-issue-transcript">
              {issue.transcript.map((m) => (
                <div key={m.id} className={m.flagged ? "issue-turn flagged" : "issue-turn"}>
                  <b>{m.role === "user" ? t("selfService.common.question") : t("selfService.common.answer")}</b>
                  {m.answered_by && <Tag tone="orange">{t("selfService.common.curated")}</Tag>}
                  <p>{m.text}</p>
                </div>
              ))}
            </div>
          </Card>
          {(issue.comment || issue.correction) && (
            <Alert tone="info">
              {issue.comment && <div>{t("selfService.issues.commentIs", { text: issue.comment })}</div>}
              {issue.correction && <div>{t("selfService.issues.correctionIs", { text: issue.correction })}</div>}
            </Alert>
          )}

          {open && (
            <Card title={t("selfService.issues.chooseFix")}>
              <div className="v2-row-actions">
                <Button kind={mode === "rule" ? "primary" : undefined} onClick={() => setMode("rule")} testId="v2-fix-rule">
                  <MessageSquareQuote size={14} aria-hidden="true" /> {t("selfService.issues.fixRule")}
                </Button>
                <Button kind={mode === "kb" ? "primary" : undefined} onClick={() => setMode("kb")} testId="v2-fix-kb">
                  <BookOpenCheck size={14} aria-hidden="true" /> {t("selfService.issues.fixKb")}
                </Button>
                <Button kind={mode === "dataset" ? "primary" : undefined} onClick={() => setMode("dataset")} testId="v2-fix-dataset">
                  <Database size={14} aria-hidden="true" /> {t("selfService.issues.fixDataset")}
                </Button>
              </div>
              {mode === "rule" && (
                <div className="v2-stack">
                  <Field label={t("selfService.answers.pattern")} hint={t("selfService.answers.patternHint")}>
                    <input className="v2-input" value={pattern} onChange={(e) => setPattern(e.target.value)} />
                  </Field>
                  <Field label={t("selfService.answers.match")}>
                    <Select
                      value={match}
                      options={[
                        { value: "exact", label: t("selfService.answers.matchExact") },
                        { value: "contains", label: t("selfService.answers.matchContains") },
                      ]}
                      onChange={(v) => setMatch(v as "exact" | "contains")}
                    />
                  </Field>
                  <Field label={t("selfService.answers.answer")}>
                    <textarea className="v2-textarea" rows={4} value={answer} onChange={(e) => setAnswer(e.target.value)} />
                  </Field>
                  <Button
                    kind="primary"
                    disabled={busy || !pattern.trim() || !answer.trim()}
                    onClick={() =>
                      void run(
                        () => ruleApi.create(issue.agent_id, { match, pattern, answer, issue_id: issue.id }),
                        t("selfService.issues.ruleAdded"),
                      )
                    }
                    testId="v2-fix-rule-save"
                  >
                    <FilePlus2 size={14} aria-hidden="true" /> {t("selfService.issues.saveRule")}
                  </Button>
                </div>
              )}
              {mode === "kb" && (
                <div className="v2-stack">
                  <p>{t("selfService.issues.kbBody")}</p>
                  <Link to="/v2/knowledge-bases">{t("selfService.issues.kbOpen")}</Link>
                  <Field label={t("selfService.issues.kbId")} hint={t("selfService.issues.kbIdHint")}>
                    <input className="v2-input" maxLength={64} value={kbId} onChange={(e) => setKbId(e.target.value)} />
                  </Field>
                  <Button disabled={busy} onClick={() => void run(() => issueApi.recordFix(issue.id, { action: "kb", ref: kbId.trim() || null }), t("selfService.issues.fixRecorded"))}>
                    {t("selfService.issues.kbRecord")}
                  </Button>
                </div>
              )}
              {mode === "dataset" && (
                <AddToDatasetModal
                  open
                  sessionIds={[issue.session_id]}
                  range="7d"
                  onClose={() => setMode(null)}
                  onDone={(res) => {
                    void issueApi
                      .recordFix(issue.id, { action: "dataset", ref: res.dataset.id })
                      .then(refresh, (err) => setError(errorMessage(err)));
                  }}
                />
              )}
            </Card>
          )}

          {issue.fixes.length > 0 && (
            <Card title={t("selfService.issues.fixesTitle")}>
              <ul>
                {issue.fixes.map((f, i) => (
                  <li key={i}>
                    {t(`selfService.issues.fixName.${f.action}`)}
                    {f.ref ? ` · ${f.ref}` : ""} — {f.by}, {fmtTime(f.at)}
                  </li>
                ))}
              </ul>
              {ruleFix && open && (
                <div className="v2-row-actions">
                  <Button
                    onClick={async () => {
                      try {
                        const res = await ruleApi.test(issue.agent_id, issue.question);
                        setTestResult(res.matched ? t("selfService.issues.verifyOk") : t("selfService.issues.verifyNo"));
                      } catch (err) {
                        setTestResult(errorMessage(err));
                      }
                    }}
                    testId="v2-issue-verify"
                  >
                    {t("selfService.issues.verify")}
                  </Button>
                  {testResult && <span>{testResult}</span>}
                </div>
              )}
            </Card>
          )}

          {error && <Alert tone="error">{error}</Alert>}

          {open ? (
            <Card title={t("selfService.issues.closeTitle")}>
              <Field label={t("selfService.issues.note")}>
                <input className="v2-input" maxLength={1000} value={note} onChange={(e) => setNote(e.target.value)} />
              </Field>
              <div className="v2-row-actions">
                <Button kind="primary" disabled={busy} onClick={() => void run(() => issueApi.resolve(issue.id, "fixed", note), t("selfService.issues.markedFixed"))} testId="v2-issue-fixed">
                  {t("selfService.issues.markFixed")}
                </Button>
                <Button disabled={busy} onClick={() => void run(() => issueApi.resolve(issue.id, "wont_fix", note), t("selfService.issues.markedWontFix"))} testId="v2-issue-wontfix">
                  {t("selfService.issues.markWontFix")}
                </Button>
              </div>
            </Card>
          ) : (
            <div className="v2-row-actions">
              <span>
                {t("selfService.issues.closedBy", { who: issue.resolved_by ?? "—", when: fmtTime(issue.resolved_at) })}
                {issue.hours_to_close != null && ` · ${hours(issue.hours_to_close, t)}`}
              </span>
              <Button disabled={busy} onClick={() => void run(() => issueApi.reopen(issue.id), t("selfService.issues.reopened"))} testId="v2-issue-reopen">
                {t("selfService.issues.reopen")}
              </Button>
            </div>
          )}

          <Card title={t("selfService.issues.historyTitle")}>
            <ul>
              {issue.history.map((h, i) => (
                <li key={i}>
                  {t(`selfService.issues.state.${h.status}`)} — {h.by}, {fmtTime(h.at)}
                  {h.note ? ` (${h.note})` : ""}
                </li>
              ))}
            </ul>
          </Card>
        </div>
      )}
    </Drawer>
  );
}
