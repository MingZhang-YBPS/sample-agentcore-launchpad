import { BookOpenCheck, Database, ExternalLink, MessageSquareQuote, RefreshCw } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { errorMessage, type IssueDetail, issueApi, type IssueStatus, ruleApi } from "../../../lib/api";
import { V2ToastProvider } from "../../../v2/ui";
import { AddToDatasetModal } from "../../../v2/pages/data/AddToDataset";
import { useWorkspace } from "../../../workspace/workspace-context";
import { ago } from "../../format";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Empty, Filters, Lamp, Notice, Panel, Skeleton, Stat } from "../../ui";
import { hours, issueSignal, useAgents } from "./common";

type StatusFilter = IssueStatus | "all";

/**
 * The issue box. A thumbs-down, a reviewer's correction or an unanswered question
 * lands here; the owner opens it, reads the turn, picks a fix and closes it. The
 * fixes call the existing endpoints (curated answer, a `from-sessions` dataset,
 * the knowledge-base page) — this page never builds a dataset or uploads a file.
 */
export function IssueBox() {
  const { t } = useTranslation();
  const { current } = useWorkspace();
  const ws = current?.id ?? "";
  const { agents, label } = useAgents(ws);
  const [params, setParams] = useSearchParams();
  const [status, setStatus] = useState<StatusFilter>("open");
  const [agentId, setAgentId] = useState("");
  const [tick, setTick] = useState(0);
  const openId = params.get("issue");

  // backfill the thumbs-down votes cast before the box existed (idempotent, as V2)
  useEffect(() => {
    void issueApi.sync().then(() => setTick((n) => n + 1), () => undefined);
  }, [ws]);

  const list = useLoad(
    () => issueApi.list({ status: status === "all" ? undefined : status, agent_id: agentId || undefined }),
    `v3-issues:${ws}:${status}:${agentId}:${tick}`,
  );
  const summary = list.data?.summary;
  const items = list.data?.items ?? [];
  const reload = () => setTick((n) => n + 1);
  const select = (id: string | null) => {
    const next = new URLSearchParams(params);
    if (id) next.set("issue", id);
    else next.delete("issue");
    setParams(next, { replace: true });
  };
  const oldest = summary?.oldest_open_hours ?? null;

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div className="v3-grid c4">
        <Panel signal={summary?.open ? "act" : summary ? "ok" : undefined}>
          <Stat label={t("v3.issues.open")} value={summary?.open ?? "—"} signal={summary?.open ? "act" : undefined}
            foot={summary ? t("v3.issues.openFoot", { total: summary.total }) : undefined} />
        </Panel>
        <Panel signal={oldest != null && oldest > 48 ? "wait" : undefined}>
          <Stat label={t("v3.issues.oldest")} value={hours(oldest, t)} foot={t("v3.issues.oldestFoot")} />
        </Panel>
        <Panel><Stat label={t("v3.issues.toClose")} value={hours(summary?.median_hours_to_close, t)} foot={t("selfService.issues.kpiCloseHint")} /></Panel>
        <Panel>
          <Stat label={t("v3.issues.closed")} value={summary ? summary.fixed + summary.wont_fix : "—"}
            foot={summary ? t("v3.issues.closedFoot", { fixed: summary.fixed, wont: summary.wont_fix }) : undefined} />
        </Panel>
      </div>

      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <Filters
          value={status}
          onChange={(v) => {
            setStatus(v);
            select(null);
          }}
          options={[
            { value: "open", label: t("selfService.issues.state.open"), s: "act", count: summary?.open },
            { value: "fixed", label: t("selfService.issues.state.fixed"), s: "ok", count: summary?.fixed },
            { value: "wont_fix", label: t("selfService.issues.state.wont_fix"), s: "off", count: summary?.wont_fix },
            { value: "all", label: t("v3.issues.all"), count: summary?.total },
          ]}
        />
        <select className="v3-select v3-iss-select" value={agentId} onChange={(e) => setAgentId(e.target.value)}
          aria-label={t("selfService.common.agent")}>
          <option value="">{t("selfService.common.allAgents")}</option>
          {agents.map((a) => <option key={a.id} value={a.id}>{label(a.id)}</option>)}
        </select>
        <Btn kind="ghost" size="sm" onClick={reload} title={t("v3.issues.refresh")}><RefreshCw size={14} /></Btn>
      </div>

      <div className="v3-iss-desk">
        <Panel flush title={t("v3.issues.queue")} end={<span className="mono">{items.length}</span>}>
          {list.loading && !list.data ? (
            <div style={{ padding: 20 }}><Skeleton rows={5} /></div>
          ) : list.error ? (
            <div style={{ padding: 20 }}><Notice s="act">{list.error}</Notice></div>
          ) : items.length === 0 ? (
            <Empty title={status === "open" ? t("v3.issues.clear") : t("selfService.issues.empty")}>
              {status === "open" && t("v3.issues.clearSub")}
            </Empty>
          ) : (
            <div className="v3-iss-queue">
              {items.map((r) => (
                <button key={r.id} type="button" className={openId === r.id ? "v3-iss-item on" : "v3-iss-item"} onClick={() => select(r.id)}>
                  <span className="lamp"><Lamp s={issueSignal(r.status)} live={r.status === "open"} /></span>
                  <span className="q" title={r.question}>{r.question || "—"}</span>
                  <span className="when">{ago(r.created_at)}</span>
                  <span className="a" title={r.answer}>{r.answer || "—"}</span>
                  <span className="meta">
                    <Chip>{t(`selfService.issues.kindName.${r.kind}`)}</Chip>
                    <span>{r.agent_name}</span>
                    {r.status !== "open" && <span>· {t(`selfService.issues.state.${r.status}`)}</span>}
                  </span>
                </button>
              ))}
            </div>
          )}
        </Panel>
        {openId ? (
          <IssueBench key={openId} id={openId} onChanged={reload} onClose={() => select(null)} />
        ) : (
          <Panel><Empty title={t("v3.issues.pick")}>{t("v3.issues.pickSub")}</Empty></Panel>
        )}
      </div>
    </div>
  );
}

function IssueBench({ id, onChanged, onClose }: { id: string; onChanged: () => void; onClose: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [nonce, setNonce] = useState(0);
  const detail = useLoad<IssueDetail>(() => issueApi.get(id), `v3-issue:${id}:${nonce}`);
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
      toast("ok", okText);
      refresh();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  if (detail.loading && !issue) return <Panel><Skeleton rows={6} /></Panel>;
  if (!issue) return <Panel><Notice s="act">{detail.error ?? t("v3.issues.notFound")}</Notice></Panel>;

  const open = issue.status === "open";
  const ruleFix = issue.fixes.find((f) => f.action === "rule");
  const s = issueSignal(issue.status);

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <Panel
        signal={s === "off" ? undefined : s}
        title={t("selfService.issues.drawerTitle", { agent: issue.agent_name })}
        end={
          <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
            <Chip s={s === "off" ? undefined : s}>{t(`selfService.issues.state.${issue.status}`)}</Chip>
            <Chip>{t(`selfService.issues.kindName.${issue.kind}`)}</Chip>
            <Link className="v3-btn ghost sm" to={`/v3/chat?agent=${encodeURIComponent(issue.agent_id)}&session=${encodeURIComponent(issue.session_id)}`}>
              {t("selfService.common.openSession")} <ExternalLink size={12} />
            </Link>
            <button type="button" className="v3-btn ghost sm" onClick={onClose} aria-label={t("v3.issues.close")}>×</button>
          </span>
        }
      >
        <div className="v3-iss-transcript">
          {issue.transcript.map((m) => (
            <div key={m.id} className={["v3-iss-turn", m.role === "user" ? "user" : "", m.flagged ? "flagged" : ""].join(" ").trim()}>
              <div className="who">
                {m.role === "user" ? t("selfService.common.question") : t("selfService.common.answer")}
                {m.answered_by && <Chip s="wait">{t("selfService.common.curated")}</Chip>}
                {m.flagged && <Chip s="act">{t("v3.issues.flagged")}</Chip>}
              </div>
              <p>{m.text}</p>
            </div>
          ))}
        </div>
        {(issue.comment || issue.correction) && (
          <div style={{ marginTop: 12 }}>
            <Notice>
              {issue.comment && <div>{t("selfService.issues.commentIs", { text: issue.comment })}</div>}
              {issue.correction && <div>{t("selfService.issues.correctionIs", { text: issue.correction })}</div>}
            </Notice>
          </div>
        )}
      </Panel>

      {open && (
        <Panel title={t("selfService.issues.chooseFix")}>
          <div className="v3-iss-fixes" role="radiogroup" aria-label={t("selfService.issues.chooseFix")}>
            {(
              [
                ["rule", <MessageSquareQuote key="i" size={16} />, "selfService.issues.fixRule", "v3.issues.fixRuleSub"],
                ["kb", <BookOpenCheck key="i" size={16} />, "selfService.issues.fixKb", "v3.issues.fixKbSub"],
                ["dataset", <Database key="i" size={16} />, "selfService.issues.fixDataset", "v3.issues.fixDatasetSub"],
              ] as const
            ).map(([key, icon, title, sub]) => (
              <button key={key} type="button" role="radio" aria-checked={mode === key}
                className={mode === key ? "v3-iss-fix on" : "v3-iss-fix"} onClick={() => setMode(key)}>
                <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>{icon}<b>{t(title)}</b></span>
                <small>{t(sub)}</small>
              </button>
            ))}
          </div>

          {mode === "rule" && (
            <div style={{ display: "grid", gap: 12, marginTop: 16 }}>
              <label className="v3-field">
                <span>{t("selfService.answers.pattern")}</span>
                <input className="v3-input" value={pattern} onChange={(e) => setPattern(e.target.value)} />
                <small className="v3-hint">{t("selfService.answers.patternHint")}</small>
              </label>
              <label className="v3-field">
                <span>{t("selfService.answers.match")}</span>
                <select className="v3-select" value={match} onChange={(e) => setMatch(e.target.value as "exact" | "contains")}>
                  <option value="exact">{t("selfService.answers.matchExact")}</option>
                  <option value="contains">{t("selfService.answers.matchContains")}</option>
                </select>
              </label>
              <label className="v3-field">
                <span>{t("selfService.answers.answer")}</span>
                <textarea className="v3-input" rows={4} value={answer} onChange={(e) => setAnswer(e.target.value)} />
              </label>
              <div>
                <Btn kind="primary" disabled={busy || !pattern.trim() || !answer.trim()}
                  onClick={() => void run(() => ruleApi.create(issue.agent_id, { match, pattern, answer, issue_id: issue.id }), t("selfService.issues.ruleAdded"))}>
                  {t("selfService.issues.saveRule")}
                </Btn>
              </div>
            </div>
          )}
          {mode === "kb" && (
            <div style={{ display: "grid", gap: 12, marginTop: 16 }}>
              <p style={{ margin: 0, color: "var(--v3-text-2)" }}>
                {t("selfService.issues.kbBody")}{" "}
                <Link to="/v3/knowledge" style={{ textDecoration: "underline" }}>{t("selfService.issues.kbOpen")}</Link>
              </p>
              <label className="v3-field">
                <span>{t("selfService.issues.kbId")}</span>
                <input className="v3-input mono" maxLength={64} value={kbId} onChange={(e) => setKbId(e.target.value)} />
                <small className="v3-hint">{t("selfService.issues.kbIdHint")}</small>
              </label>
              <div>
                <Btn disabled={busy} onClick={() => void run(() => issueApi.recordFix(issue.id, { action: "kb", ref: kbId.trim() || null }), t("selfService.issues.fixRecorded"))}>
                  {t("selfService.issues.kbRecord")}
                </Btn>
              </div>
            </div>
          )}
          {mode === "dataset" && (
            // the dataset picker is V2's modal, hosted on the V3 theme
            <V2ToastProvider>
              <div className="v2 v3-host">
                <AddToDatasetModal
                  open
                  sessionIds={[issue.session_id]}
                  range="7d"
                  onClose={() => setMode(null)}
                  onDone={(res) => {
                    void issueApi.recordFix(issue.id, { action: "dataset", ref: res.dataset.id }).then(refresh, (err) => setError(errorMessage(err)));
                  }}
                />
              </div>
            </V2ToastProvider>
          )}
        </Panel>
      )}

      {issue.fixes.length > 0 && (
        <Panel
          title={t("selfService.issues.fixesTitle")}
          end={
            ruleFix && open ? (
              <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
                {testResult && <span>{testResult}</span>}
                <Btn size="sm" onClick={async () => {
                  try {
                    const res = await ruleApi.test(issue.agent_id, issue.question);
                    setTestResult(res.matched ? t("selfService.issues.verifyOk") : t("selfService.issues.verifyNo"));
                  } catch (err) {
                    setTestResult(errorMessage(err));
                  }
                }}>{t("selfService.issues.verify")}</Btn>
              </span>
            ) : undefined
          }
        >
          <ul className="v3-iss-log">
            {issue.fixes.map((f, i) => (
              <li key={i}>
                <Lamp s="ok" />
                <span>{t(`selfService.issues.fixName.${f.action}`)}{f.ref ? ` · ${f.ref}` : ""}</span>
                <span className="at">{f.by} · {ago(f.at)}</span>
              </li>
            ))}
          </ul>
        </Panel>
      )}

      {error && <Notice s="act">{error}</Notice>}

      {open ? (
        <Panel title={t("selfService.issues.closeTitle")}>
          <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <input className="v3-input" style={{ flex: "1 1 260px" }} maxLength={1000} value={note}
              placeholder={t("selfService.issues.note")} aria-label={t("selfService.issues.note")} onChange={(e) => setNote(e.target.value)} />
            <Btn kind="primary" disabled={busy} onClick={() => void run(() => issueApi.resolve(issue.id, "fixed", note), t("selfService.issues.markedFixed"))}>
              {t("selfService.issues.markFixed")}
            </Btn>
            <Btn disabled={busy} onClick={() => void run(() => issueApi.resolve(issue.id, "wont_fix", note), t("selfService.issues.markedWontFix"))}>
              {t("selfService.issues.markWontFix")}
            </Btn>
          </div>
        </Panel>
      ) : (
        <Panel>
          <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
            <span style={{ color: "var(--v3-text-2)" }}>
              {t("selfService.issues.closedBy", { who: issue.resolved_by ?? "—", when: ago(issue.resolved_at) })}
              {issue.hours_to_close != null && ` · ${hours(issue.hours_to_close, t)}`}
            </span>
            <span style={{ marginLeft: "auto" }}>
              <Btn disabled={busy} onClick={() => void run(() => issueApi.reopen(issue.id), t("selfService.issues.reopened"))}>
                {t("selfService.issues.reopen")}
              </Btn>
            </span>
          </div>
        </Panel>
      )}

      <Panel title={t("selfService.issues.historyTitle")}>
        <ul className="v3-iss-log">
          {issue.history.map((h, i) => (
            <li key={i}>
              <Lamp s={issueSignal(h.status)} />
              <span>{t(`selfService.issues.state.${h.status}`)}{h.note ? ` — ${h.note}` : ""}</span>
              <span className="at">{h.by} · {ago(h.at)}</span>
            </li>
          ))}
        </ul>
      </Panel>
    </div>
  );
}
