/**
 * Calibration: "the judge agrees with us" as a measurement. Three numbers — human–
 * human κ, judge–human κ with its interval, and the confusion cells. Labelling is
 * blind (the judge's verdict is withheld from an annotator until the task closes),
 * a task needs at least two annotators, and `aligned` is refused by the server when
 * the numbers do not support it or when the decider labelled the evidence.
 */
import { Copy, Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { ErrorCallout } from "../../../components/ErrorCallout";
import { useAuth } from "../../../auth/auth-context";
import { dlcApi, errorMessage } from "../../../lib/api";
import { type CandidateRun, calibrationApi, candidateLabel, DROPDOWN_RUNS } from "../../../lib/api/calibration";
import type { AnnotationTask } from "../../../lib/dlc";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Dialog, Empty, Filters, Lamp, Notice, Panel, Skeleton, Stat } from "../../ui";
import { ConfusionMatrix } from "./charts";
import { runNamed, runRefusal } from "./calibrationRunError";
import { kappaSignal, stamp } from "./common";

const LABELS = ["pass", "fail", "inconclusive"] as const;

/** Annotation links: a link *is* an annotator; a prod workspace refuses to issue one. */
function LinkPanel({ taskId }: { taskId: string }) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useToast();
  const [nonce, setNonce] = useState(0);
  const [label, setLabel] = useState("");
  const [days, setDays] = useState(14);
  const [minted, setMinted] = useState<{ url: string; label: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const links = useLoad(() => dlcApi.annotationLinks(taskId), `v3-std-links:${taskId}:${nonce}`);
  const mayManage = can("judge.calibrate");
  const allowed = links.data?.allowed ?? false;
  const rows = links.data?.links ?? [];

  return (
    <Panel title={t("v2.dlc.calibration.links")} flush={rows.length > 0}>
      <div style={rows.length > 0 ? { padding: "0 20px 14px", display: "grid", gap: 10 } : { display: "grid", gap: 10 }}>
        <p className="v3-std-muted" style={{ margin: 0 }}>{t("v2.dlc.calibration.linksSub")}</p>
        {links.data && !allowed && <Notice s="wait">{links.data.reason}</Notice>}
        {minted && (
          <Notice s="ok">
            {t("v2.dlc.calibration.linkMinted", { who: minted.label })}
            <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 6 }}>
              <code className="mono" style={{ wordBreak: "break-all" }}>{minted.url}</code>
              <Btn size="sm" kind="ghost" onClick={() => { void navigator.clipboard?.writeText(minted.url); toast("ok", t("v2.dlc.calibration.linkCopied")); }}>
                <Copy size={13} />
              </Btn>
            </div>
          </Notice>
        )}
        {links.loading && !links.data && <Skeleton rows={2} />}
        {links.data && rows.length === 0 && <p className="v3-std-muted" style={{ margin: 0 }}>{t("v2.dlc.calibration.noLinks")}</p>}
      </div>
      {rows.length > 0 && (
        <table className="v3-table">
          <thead>
            <tr>
              <th>{t("v2.dlc.calibration.linkFor")}</th>
              <th>{t("v3.standards.status")}</th>
              <th className="num">{t("v2.dlc.calibration.linkUses")}</th>
              <th>{t("v2.dlc.waiver.expires")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id}>
                <td>{row.label}</td>
                <td><Chip s={row.state === "active" ? "ok" : undefined}>{t(`v2.dlc.calibration.linkState.${row.state}`)}</Chip></td>
                <td className="num">{row.use_count}</td>
                <td>{row.expires_at?.slice(0, 10) ?? "—"}</td>
                <td style={{ width: 1 }}>
                  {row.state === "active" && mayManage && (
                    <Btn size="sm" kind="ghost" disabled={busy}
                      onClick={() => {
                        setBusy(true);
                        dlcApi
                          .revokeAnnotationLink(taskId, row.id)
                          .then(() => { toast("ok", t("v2.dlc.calibration.linkRevoked")); setNonce((n) => n + 1); })
                          .catch((err: unknown) => toast("act", errorMessage(err), err))
                          .finally(() => setBusy(false));
                      }}>
                      {t("v2.dlc.calibration.revokeLink")}
                    </Btn>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {mayManage && allowed && (
        <div style={{ padding: rows.length > 0 ? "14px 20px 18px" : "12px 0 0", display: "flex", gap: 10, alignItems: "end", flexWrap: "wrap" }}>
          <label className="v3-field" style={{ flex: "1 1 220px" }}>
            <span>{t("v2.dlc.calibration.linkFor")}</span>
            <input className="v3-input" value={label} maxLength={64} onChange={(e) => setLabel(e.target.value)} placeholder={t("v2.dlc.calibration.linkForHint")} />
          </label>
          <label className="v3-field" style={{ width: 140 }}>
            <span>{t("v2.dlc.calibration.linkDays")}</span>
            <input className="v3-input" type="number" min={1} max={90} value={days} onChange={(e) => setDays(Number(e.target.value))} />
          </label>
          <Btn disabled={busy || label.trim() === ""}
            onClick={() => {
              setBusy(true);
              dlcApi
                .createAnnotationLink(taskId, { label, expires_in_days: days })
                .then((created) => {
                  setMinted({ url: `${window.location.origin}${created.path}`, label: created.label });
                  setLabel("");
                  setNonce((n) => n + 1);
                })
                .catch((err: unknown) => toast("act", errorMessage(err), err))
                .finally(() => setBusy(false));
            }}>
            {t("v2.dlc.calibration.createLink")}
          </Btn>
        </div>
      )}
    </Panel>
  );
}

function TaskBench({ taskId, onBack }: { taskId: string; onBack: () => void }) {
  const { t } = useTranslation();
  const { can, username } = useAuth();
  const toast = useToast();
  const [nonce, setNonce] = useState(0);
  const task = useLoad(() => dlcApi.getTask(taskId), `v3-std-task:${taskId}:${nonce}`);
  const agreement = useLoad(() => dlcApi.agreement(taskId), `v3-std-agree:${taskId}:${nonce}`);
  const [deciding, setDeciding] = useState<"aligned" | "not_aligned" | null>(null);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const data = task.data;
  const measured = agreement.data;
  const mayDecide = can("judge.calibrate");
  // labelling / adjudicating / deciding fail in place, with the way out (storyline-ux §4)
  const [failure, setFailure] = useState<unknown>(null);

  const act = (fn: () => Promise<unknown>, okKey?: string) => {
    setBusy(true);
    setFailure(null);
    fn()
      .then(() => { if (okKey) toast("ok", t(okKey)); setNonce((n) => n + 1); })
      .catch((err: unknown) => setFailure(err))
      .finally(() => setBusy(false));
  };

  if (task.loading && !data) return <Skeleton rows={6} />;
  if (!data) return <Notice s="act">{task.error ?? t("v2.dlc.calibration.notFound")}</Notice>;
  const blind = data.status !== "closed" && !mayDecide;
  const mine = data.items.filter((i) => i.my_label).length;
  const iAnnotate = data.annotators.includes(username ?? "") && data.status !== "closed";

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div><button type="button" className="v3-btn ghost sm" onClick={onBack}>← {t("v2.dlc.view.calibration")}</button></div>
      <ErrorCallout error={failure} params={{ agent_id: data.agent_id ?? undefined, task_id: data.id }}
        onDismiss={() => setFailure(null)} data-testid="v3-calibration-error" />
      <Panel
        title={t("v2.dlc.calibration.taskTitle", { key: data.criterion_key })}
        signal={data.status === "closed" ? "ok" : "wait"}
        end={<Chip s={data.status === "closed" ? "ok" : "wait"}>{t(`v2.dlc.calibration.taskStatus.${data.status}`)}</Chip>}
      >
        <p className="v3-std-muted" style={{ margin: "0 0 12px" }}>{t(`v2.dlc.calibration.purpose.${data.purpose}`)}</p>
        <div className="v3-grid c3">
          <Stat label={t("v2.dlc.calibration.items")} value={data.total} />
          <Stat label={t("v2.dlc.calibration.mine")} value={`${mine} / ${data.total}`} />
          <Stat label={t("v2.dlc.calibration.annotators")} value={data.annotators.length}
            foot={data.annotators.map((a) => `${a} (${data.progress[a] ?? 0})`).join(" · ")} />
        </div>
        {blind && <div style={{ marginTop: 12 }}><Notice>{t("v2.dlc.calibration.blindHint")}</Notice></div>}
      </Panel>

      {measured && (
        <Panel title={t("v2.dlc.calibration.agreement")} signal={measured.suggested_verdict === "aligned" ? "ok" : measured.suggested_verdict === "insufficient_n" || measured.suggested_verdict === "one_class" ? "wait" : "act"}>
          <div className="v3-grid v3-split" style={{ alignItems: "start" }}>
            <div style={{ display: "grid", gap: 12 }}>
              <div className="v3-grid c2">
                <Stat label={t("v2.dlc.calibration.humanKappa")} value={measured.human_human_kappa === null ? "—" : measured.human_human_kappa.toFixed(2)}
                  signal={kappaSignal(measured.human_human_kappa)} foot={measured.human_band ? t(`v2.dlc.kappaBand.${measured.human_band}`) : undefined} />
                <Stat label={t("v2.dlc.calibration.judgeKappa")} value={measured.judge_human_kappa === null ? "—" : measured.judge_human_kappa.toFixed(2)}
                  signal={kappaSignal(measured.judge_human_kappa)} foot={measured.band ? t(`v2.dlc.kappaBand.${measured.band}`) : undefined} />
                <Stat label={t("v2.dlc.calibration.n")} value={measured.n} />
                <Stat label={t("v2.dlc.calibration.floor")} value={measured.policy.kappa_floor.toFixed(2)}
                  foot={t("v2.dlc.calibration.period", { days: measured.policy.period_days })} />
              </div>
              {measured.human_human_kappa !== null && measured.human_human_kappa < 0.6 && <Notice s="wait">{t("v2.dlc.calibration.humansDisagree")}</Notice>}
              {measured.suggested_verdict === "insufficient_n" && <Notice s="wait">{t("v2.dlc.calibration.needMore")}</Notice>}
              {measured.suggested_verdict === "one_class" && <Notice s="wait">{t("v2.dlc.calibration.oneClass")}</Notice>}
            </div>
            <ConfusionMatrix confusion={measured.confusion} kappa={measured.judge_human_kappa} ci={measured.kappa_ci} />
          </div>
          {measured.disagreements.length > 0 && (
            <>
              <div className="v3-std-subtitle">{t("v2.dlc.calibration.disagreements")}</div>
              <table className="v3-table">
                <thead>
                  <tr>
                    <th>{t("v2.dlc.calibration.item")}</th>
                    <th>{t("v2.dlc.calibration.humanCol")}</th>
                    <th>{t("v2.dlc.calibration.judgeCol")}</th>
                    <th>{t("v2.dlc.calibration.judgeSaid")}</th>
                  </tr>
                </thead>
                <tbody>
                  {measured.disagreements.slice(0, 20).map((d) => (
                    <tr key={d.ref}>
                      <td className="mono">{d.ref}</td>
                      <td>{d.needs_adjudication ? <Chip s="wait">{t("v2.dlc.calibration.needsAdjudication")}</Chip> : d.human ?? "—"}</td>
                      <td>{d.judge ?? "—"}</td>
                      <td className="v3-std-muted">{d.judge_explanation ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          {mayDecide && data.status !== "closed" && (
            <div style={{ display: "flex", gap: 8, marginTop: 14, flexWrap: "wrap" }}>
              <Btn kind="primary" disabled={busy || measured.suggested_verdict !== "aligned"}
                title={measured.suggested_verdict !== "aligned" ? t("v2.dlc.calibration.cannotAlign") : undefined}
                onClick={() => { setNote(""); setDeciding("aligned"); }}>
                {t("v2.dlc.calibration.markAligned")}
              </Btn>
              <Btn disabled={busy} onClick={() => { setNote(""); setDeciding("not_aligned"); }}>{t("v2.dlc.calibration.markNotAligned")}</Btn>
              <Btn kind="ghost" disabled={busy} onClick={() => act(() => dlcApi.adjudicate(taskId))}>{t("v2.dlc.calibration.adjudicate")}</Btn>
            </div>
          )}
        </Panel>
      )}

      <Panel title={t("v2.dlc.calibration.label")} flush>
        <p className="v3-std-muted" style={{ margin: "0 20px 10px" }}>{t("v2.dlc.calibration.labelSub")}</p>
        <table className="v3-table">
          <thead>
            <tr>
              <th>{t("v2.dlc.calibration.item")}</th>
              <th>{t("v2.dlc.admission.question")}</th>
              <th>{t("v2.dlc.admission.expected")}</th>
              <th>{t("v2.dlc.calibration.answer")}</th>
              <th>{t("v2.dlc.calibration.myLabel")}</th>
              {!blind && <th>{t("v2.dlc.calibration.judgeCol")}</th>}
            </tr>
          </thead>
          <tbody>
            {data.items.map((item) => (
              <tr key={item.ref}>
                <td className="mono">{item.ref}</td>
                <td>{item.input ?? "—"}</td>
                <td className="v3-std-muted">{item.expected || "—"}</td>
                <td style={{ whiteSpace: "pre-wrap" }}>{item.answer || "—"}</td>
                <td style={{ whiteSpace: "nowrap" }}>
                  {iAnnotate ? (
                    <span className="v3-std-seg" role="radiogroup">
                      {LABELS.map((l) => (
                        <button key={l} type="button" aria-pressed={item.my_label === l} className={item.my_label === l ? `on ${l}` : undefined}
                          disabled={busy} onClick={() => act(() => dlcApi.label(taskId, { item_ref: item.ref, label: l }))}>
                          {t(`v2.dlc.label.${l}`)}
                        </button>
                      ))}
                    </span>
                  ) : (
                    item.my_label ?? "—"
                  )}
                </td>
                {!blind && <td>{item.judge_label ?? "—"}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      </Panel>

      <LinkPanel taskId={taskId} />

      {deciding && (
        <Dialog
          title={t("v2.dlc.calibration.decideTitle")}
          onClose={() => setDeciding(null)}
          foot={
            <>
              <Btn kind="ghost" onClick={() => setDeciding(null)}>{t("v3.common.cancel")}</Btn>
              <Btn kind={deciding === "aligned" ? "primary" : "danger"} disabled={busy}
                onClick={() => { const v = deciding; setDeciding(null); act(() => dlcApi.decideCalibration(taskId, v, note), "v2.dlc.calibration.decided"); }}>
                {t("v3.standards.confirm")}
              </Btn>
            </>
          }
        >
          <div style={{ display: "grid", gap: 12 }}>
            <p style={{ margin: 0 }}>{deciding === "aligned" ? t("v2.dlc.calibration.alignedHint") : t("v2.dlc.calibration.notAlignedHint")}</p>
            <label className="v3-field">
              <span>{t("v2.dlc.calibration.note")}</span>
              <textarea className="v3-input" rows={3} value={note} onChange={(e) => setNote(e.target.value)} />
            </label>
          </div>
        </Dialog>
      )}
    </div>
  );
}

type SinceFilter = "any" | "7" | "30";
type SourceFilter = "all" | "gate" | "manual";
const LIST_PAGE = 20;

const sinceIso = (since: SinceFilter): string | undefined =>
  since === "any" ? undefined : new Date(Date.now() - Number(since) * 86_400_000).toISOString();

/** One run as the picker shows it: name, release tag, finished time, id. */
function RunName({ run }: { run: CandidateRun }) {
  const { t } = useTranslation();
  return (
    <>
      {candidateLabel(run)}
      {run.release_id && <> <Chip s="info">{t("v2.dlc.calibration.releaseTag")}</Chip></>}
    </>
  );
}

/** §4.2: every run of the agent, searchable and filterable, one picked. */
function RunListDialog({ agentId, agentName, criterionKey, selected, onClose, onPick }: {
  agentId: string;
  agentName: string;
  criterionKey: string;
  selected: string;
  onClose: () => void;
  onPick: (run: CandidateRun) => void;
}) {
  const { t } = useTranslation();
  const [q, setQ] = useState("");
  const [since, setSince] = useState<SinceFilter>("any");
  const [source, setSource] = useState<SourceFilter>("all");
  const [onlyEligible, setOnlyEligible] = useState(true);
  const [page, setPage] = useState(0);
  const [choice, setChoice] = useState<CandidateRun | null>(null);
  const needle = q.trim();
  const list = useLoad(
    () => calibrationApi.candidateRuns({
      agentId, criterionKey, q: needle, since: sinceIso(since), source: source === "all" ? undefined : source,
      onlyEligible, limit: LIST_PAGE, offset: page * LIST_PAGE,
    }),
    `v3-cal-runlist:${agentId}:${criterionKey}:${needle}:${since}:${source}:${onlyEligible}:${page}`,
  );
  const rows = list.data?.runs ?? [];
  const pages = Math.max(1, Math.ceil((list.data?.total ?? 0) / LIST_PAGE));
  const current = choice?.id ?? selected;
  const reset = () => setPage(0);
  return (
    <Dialog
      wide
      title={t("v2.dlc.calibration.listTitle", { key: criterionKey, agent: agentName })}
      onClose={onClose}
      foot={
        <>
          <Btn kind="ghost" onClick={onClose}>{t("v3.common.cancel")}</Btn>
          <Btn kind="primary" disabled={!choice} onClick={() => choice && onPick(choice)}>{t("v2.dlc.calibration.choose")}</Btn>
        </>
      }
    >
      <div style={{ display: "grid", gap: 12 }}>
        <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
          <input className="v3-input" style={{ flex: "1 1 220px" }} value={q} placeholder={t("v2.dlc.calibration.searchRun")}
            aria-label={t("v2.dlc.calibration.searchRun")} onChange={(e) => { setQ(e.target.value); reset(); }} />
          <select className="v3-select" style={{ width: 140 }} value={since} aria-label={t("v2.dlc.calibration.colFinished")}
            onChange={(e) => { setSince(e.target.value as SinceFilter); reset(); }}>
            <option value="any">{t("v2.dlc.calibration.sinceAny")}</option>
            <option value="7">{t("v2.dlc.calibration.since7")}</option>
            <option value="30">{t("v2.dlc.calibration.since30")}</option>
          </select>
          <Filters value={source} onChange={(v) => { setSource(v); reset(); }}
            options={[
              { value: "all", label: t("v2.dlc.calibration.sourceAll") },
              { value: "gate", label: t("v2.dlc.calibration.sourceGate") },
              { value: "manual", label: t("v2.dlc.calibration.sourceManual") },
            ]} />
          <label style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
            <input type="checkbox" checked={onlyEligible} onChange={(e) => { setOnlyEligible(e.target.checked); reset(); }} />
            <span>{t("v2.dlc.calibration.onlyEligible", { key: criterionKey })}</span>
          </label>
        </div>
        {list.loading && !list.data ? (
          <Skeleton rows={4} />
        ) : list.error ? (
          <Notice s="act"><ApiErrorDetail error={list.failure ?? list.error} /></Notice>
        ) : rows.length === 0 ? (
          <p className="v3-std-muted" style={{ margin: 0 }}>{t("v2.dlc.calibration.noRunsMatch")}</p>
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v2.dlc.calibration.colName")}</th>
                <th>{t("v2.dlc.calibration.colFinished")}</th>
                <th>{t("v2.dlc.calibration.colId")}</th>
                <th className="num">{t("v2.dlc.calibration.colResults", { key: criterionKey })}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((run) => (
                <tr key={run.id} className={run.eligible ? "click" : undefined}
                  style={{ opacity: run.eligible ? 1 : 0.55, background: run.id === current ? "var(--v3-info-dim)" : undefined }}
                  title={run.reason ? t(`v2.dlc.calibration.ineligibleReason.${run.reason}`) : undefined}
                  onClick={() => run.eligible && setChoice(run)}>
                  <td style={{ width: 30 }}>
                    <input type="radio" name="cal-run" checked={run.id === current} disabled={!run.eligible}
                      aria-label={candidateLabel(run)} onChange={() => setChoice(run)} />
                  </td>
                  <td>
                    <RunName run={run} />
                    {run.reason && <small className="v3-hint" style={{ display: "block" }}>{t(`v2.dlc.calibration.ineligibleReason.${run.reason}`)}</small>}
                  </td>
                  <td>{stamp(run.finished_at)}</td>
                  <td className="mono">{run.id}</td>
                  <td className="num">{t("v2.dlc.calibration.runItems", { count: run.items })}</td>
                  <td style={{ width: 1 }}>
                    <a href={`/v3/tasks?view=detail&kind=run&id=${encodeURIComponent(run.id)}`} target="_blank" rel="noreferrer"
                      onClick={(e) => e.stopPropagation()}>{t("v2.dlc.calibration.viewRun")}</a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {pages > 1 && (
          <div style={{ display: "flex", gap: 8, alignItems: "center", justifyContent: "flex-end" }}>
            <Btn size="sm" kind="ghost" disabled={page === 0} onClick={() => setPage((n) => n - 1)}>‹</Btn>
            <span className="v3-std-muted">{t("v2.dlc.calibration.page", { page: page + 1, pages })}</span>
            <Btn size="sm" kind="ghost" disabled={page + 1 >= pages} onClick={() => setPage((n) => n + 1)}>›</Btn>
          </div>
        )}
      </div>
    </Dialog>
  );
}

const PASTE = "__paste__";

/**
 * §4.1: the run a task is taken from. The agent's latest runs (D-1: 20) with the
 * criterion's sample count, pickable ones first; older runs by pasting an id (checked
 * as it is typed) or through the full list.
 */
function RunPicker({ agentId, criterionKey, value, picked, pasting, onPasting, onChange, onList }: {
  agentId: string | null;
  criterionKey: string;
  value: string;
  /** a run chosen in the full list, which may be older than the dropdown's 20 */
  picked: CandidateRun | null;
  pasting: boolean;
  onPasting: (on: boolean) => void;
  onChange: (id: string) => void;
  onList: () => void;
}) {
  const { t } = useTranslation();
  const ready = Boolean(agentId && criterionKey);
  const recent = useLoad(
    () => (agentId && criterionKey
      ? calibrationApi.candidateRuns({ agentId, criterionKey, onlyEligible: false, limit: DROPDOWN_RUNS })
      : Promise.resolve(null)),
    `v3-cal-recent:${agentId ?? ""}:${criterionKey}`,
  );
  const runs = recent.data?.runs ?? [];
  const eligible = runs.filter((r) => r.eligible);
  const ineligible = runs.filter((r) => !r.eligible);
  const extra = picked && !runs.some((r) => r.id === picked.id) ? picked : null;
  const known = [...runs, ...(extra ? [extra] : [])].find((r) => r.id === value) ?? null;
  // a prefilled id the dropdown does not hold is shown as pasted, and checked
  useEffect(() => {
    if (recent.data && value && !known) onPasting(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only once the list arrives
  }, [recent.data]);

  const typed = value.trim();
  // a pasted id is checked once typing pauses, against the same list the dropdown reads
  const [check, setCheck] = useState<{ id: string; key: string; run: CandidateRun | null } | null>(null);
  useEffect(() => {
    if (!pasting || !typed || !agentId || !criterionKey) return;
    let live = true;
    const timer = window.setTimeout(() => {
      calibrationApi.candidateRuns({ agentId, criterionKey, q: typed, onlyEligible: false, limit: 5 })
        .then((res) => live && setCheck({ id: typed, key: criterionKey, run: res.runs.find((r) => r.id === typed) ?? null }))
        .catch(() => live && setCheck({ id: typed, key: criterionKey, run: null }));
    }, 300);
    return () => { live = false; window.clearTimeout(timer); };
  }, [pasting, typed, agentId, criterionKey]);
  const checked = check && check.id === typed && check.key === criterionKey ? check : null;
  const pastedRun = checked?.run ?? null;

  const option = (run: CandidateRun) => (
    <option key={run.id} value={run.id} disabled={!run.eligible}>
      {`${candidateLabel(run)}${run.release_id ? ` · ${t("v2.dlc.calibration.releaseTag")}` : ""}   ${stamp(run.finished_at ?? run.created_at, 16).slice(5)}   ${run.id}   ${t("v2.dlc.calibration.runItems", { count: run.items })}`}
    </option>
  );

  return (
    <div className="v3-field">
      <span>{t("v2.dlc.calibration.fromRun")}</span>
      <div style={{ display: "flex", gap: 8 }}>
        <select className="v3-select mono" style={{ flex: 1, minWidth: 0 }} disabled={!ready}
          value={pasting ? PASTE : known ? value : ""}
          onChange={(e) => {
            if (e.target.value === PASTE) { onPasting(true); onChange(""); return; }
            onPasting(false);
            onChange(e.target.value);
          }}>
          <option value="">{t("v2.dlc.calibration.pickRun")}</option>
          {extra && option(extra)}
          {eligible.map(option)}
          {ineligible.length > 0 && (
            <optgroup label={t("v2.dlc.calibration.ineligibleGroup", { key: criterionKey })}>
              {ineligible.map(option)}
            </optgroup>
          )}
          <option value={PASTE}>{t("v2.dlc.calibration.pasteRunId")}</option>
        </select>
        <Btn size="sm" disabled={!ready} onClick={onList}>{t("v2.dlc.calibration.pickFromList")}</Btn>
      </div>
      {pasting && (
        <input className="v3-input mono" autoFocus value={value} aria-label={t("v2.dlc.calibration.pasteRunId")}
          onChange={(e) => onChange(e.target.value)} />
      )}
      {pasting && typed ? (
        <small className="v3-hint">
          {!checked ? t("v2.dlc.calibration.pasteChecking")
            : pastedRun ? `${candidateLabel(pastedRun)} · ${pastedRun.reason ? t(`v2.dlc.calibration.ineligibleReason.${pastedRun.reason}`) : t("v2.dlc.calibration.runItems", { count: pastedRun.items })}`
              : t("v2.dlc.calibration.pasteNotListed", { key: criterionKey })}
        </small>
      ) : recent.data && eligible.length === 0 && !extra ? (
        <small className="v3-hint">{t("v2.dlc.calibration.noCandidateRuns", { key: criterionKey })}</small>
      ) : (
        <small className="v3-hint">
          {known ? t("v2.dlc.calibration.runItems", { count: known.items }) : t("v2.dlc.calibration.pickRunHint")}
        </small>
      )}
      {recent.error && <small className="v3-hint" style={{ color: "var(--v3-act)" }}>{recent.error}</small>}
    </div>
  );
}

function NewTask({ agentId, agentName, lineageId, criteriaKeys, people, initialRunId, initialKey, onClose, onCreated }: {
  agentId: string | null;
  agentName: string;
  lineageId: string | null;
  criteriaKeys: string[];
  people: string[];
  initialRunId?: string;
  initialKey?: string | null;
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const [key, setKey] = useState(initialKey ?? (initialRunId ? "" : criteriaKeys[0] ?? ""));
  const [runId, setRunId] = useState(initialRunId ?? "");
  const [pasting, setPasting] = useState(false);
  const [picked, setPicked] = useState<CandidateRun | null>(null);
  // the full list opens beside this dialog, not inside it, and Esc closes only the list
  const [listing, setListing] = useState(false);
  const [annotators, setAnnotators] = useState<string[]>([]);
  const [adjudicator, setAdjudicator] = useState("");
  const [busy, setBusy] = useState(false);
  // a prefilled criterion may arrive before the agent's table has loaded
  const keys = key && !criteriaKeys.includes(key) ? [key, ...criteriaKeys] : criteriaKeys;
  // a run refusal stays in the dialog (not a toast): it may offer the id of a run
  // whose name was typed, which the user confirms before saving again
  const [refusal, setRefusal] = useState<{ message: string; suggestion: string | null } | null>(null);
  const refused = (err: unknown, typed: string) => {
    const why = runRefusal(err, typed, key);
    if (!why) { toast("act", errorMessage(err), err); return; }
    setRefusal({ message: t(why.key, why.vars), suggestion: null });
    const name = why.lookupName;
    if (!name || !agentId) return;
    // the picker's own search matches names, so a typed name finds its run here
    calibrationApi.candidateRuns({ agentId, criterionKey: key, q: name, onlyEligible: false, limit: 100 })
      .then((res) => {
        const id = runNamed(res.runs, name);
        if (id && id !== typed) setRefusal((prev) => prev && { ...prev, suggestion: id });
      })
      .catch(() => undefined); // the explanation stands without the suggestion
  };
  return (
    <>
      <Dialog
        wide
        title={t("v2.dlc.calibration.newTaskTitle")}
        onClose={listing ? () => undefined : onClose}
        foot={
          <>
            <Btn kind="ghost" onClick={onClose}>{t("v3.common.cancel")}</Btn>
            {/* two raters at least: one person's labels are not a consensus */}
            <Btn kind="primary" disabled={busy || !key || annotators.length < 2 || !runId.trim()}
              onClick={() => {
                const typed = runId.trim();
                setBusy(true);
                setRefusal(null);
                dlcApi
                  .createTask({ agent_id: agentId, criteria_lineage_id: lineageId, criterion_key: key, annotators, adjudicator: adjudicator || null, run_id: typed })
                  .then((created) => { toast("ok", t("v2.dlc.calibration.taskCreated")); onCreated(created.id); })
                  .catch((err: unknown) => refused(err, typed))
                  .finally(() => setBusy(false));
              }}>
              {t("v3.standards.save")}
            </Btn>
          </>
        }
      >
        <div style={{ display: "grid", gap: 12 }}>
          <p style={{ margin: 0 }}>{t("v2.dlc.calibration.newTaskHint")}</p>
          <div className="v3-grid c2">
            <label className="v3-field">
              <span>{t("v2.dlc.criteria.key")}</span>
              <select className="v3-select" value={key} onChange={(e) => { setKey(e.target.value); setRefusal(null); }}>
                <option value="">{t("v2.dlc.calibration.pickCriterion")}</option>
                {keys.map((k) => <option key={k} value={k}>{k}</option>)}
              </select>
            </label>
            <RunPicker agentId={agentId} criterionKey={key} value={runId} picked={picked}
              pasting={pasting} onPasting={setPasting} onList={() => setListing(true)}
              onChange={(id) => { setRunId(id); setRefusal(null); }} />
          </div>
          {refusal && (
            <Notice s="act">
              {refusal.message}
              {refusal.suggestion && (
                <div style={{ display: "flex", gap: 8, alignItems: "center", marginTop: 6, flexWrap: "wrap" }}>
                  <span>{t("v2.dlc.calibration.runNamedLike", { id: refusal.suggestion })}</span>
                  <Btn size="sm" onClick={() => { setRunId(refusal.suggestion ?? ""); setPasting(true); setRefusal(null); }}>
                    {t("v2.dlc.calibration.useThisRun")}
                  </Btn>
                </div>
              )}
            </Notice>
          )}
          <div className="v3-field">
            <span>{t("v2.dlc.calibration.annotators")}</span>
            <div className="v3-std-keys">
              {people.map((person) => (
                <label key={person}>
                  <input type="checkbox" checked={annotators.includes(person)}
                    onChange={(e) => setAnnotators((prev) => (e.target.checked ? [...prev, person] : prev.filter((p) => p !== person)))} />
                  <span>{person}</span>
                </label>
              ))}
              {people.length === 0 && <span className="v3-std-muted">{t("v3.standards.noPeople")}</span>}
            </div>
            <small className="v3-hint">{t("v2.dlc.calibration.annotatorsHint")}</small>
          </div>
          <label className="v3-field">
            <span>{t("v2.dlc.calibration.adjudicator")}</span>
            <select className="v3-select" value={adjudicator} onChange={(e) => setAdjudicator(e.target.value)}>
              <option value="">{t("v2.dlc.calibration.noAdjudicator")}</option>
              {people.map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
            <small className="v3-hint">{t("v2.dlc.calibration.adjudicatorHint")}</small>
          </label>
        </div>
      </Dialog>
      {listing && agentId && (
        <RunListDialog agentId={agentId} agentName={agentName} criterionKey={key} selected={runId}
          onClose={() => setListing(false)}
          onPick={(run) => { setPicked(run); setPasting(false); setRunId(run.id); setRefusal(null); setListing(false); }} />
      )}
    </>
  );
}

export function Calibration({ agentId, agentName, lineageId, criteriaKeys, people, taskId, prefill, onPrefillDone, onOpenTask }: {
  agentId: string | null;
  agentName?: string;
  lineageId: string | null;
  criteriaKeys: string[];
  people: string[];
  taskId: string | null;
  /** `new=1&run=&criterion=` from a "build an annotation task from this run" link */
  prefill?: { runId: string; criterion: string | null } | null;
  /** drop the prefill from the address once the dialog is closed or the task created */
  onPrefillDone?: () => void;
  onOpenTask: (id: string | null) => void;
}) {
  const { t } = useTranslation();
  const [nonce, setNonce] = useState(0);
  const [creating, setCreating] = useState(Boolean(prefill));
  const tasks = useLoad<{ tasks: AnnotationTask[] }>(() => dlcApi.listTasks({ agentId: agentId ?? undefined }), `v3-std-tasks:${agentId ?? ""}:${nonce}`);
  const prefillKey = prefill ? `${prefill.runId}:${prefill.criterion ?? ""}` : "";
  // a link followed while the page is already open
  useEffect(() => {
    if (prefillKey) setCreating(true);
  }, [prefillKey]);
  if (taskId) return <TaskBench taskId={taskId} onBack={() => onOpenTask(null)} />;
  const rows = tasks.data?.tasks ?? [];
  const open = rows.filter((r) => r.status !== "closed").length;

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <Panel title={t("v2.dlc.calibration.title")} flush signal={open ? "wait" : undefined}
        end={<Btn size="sm" kind="primary" onClick={() => setCreating(true)}><Plus size={13} /> {t("v2.dlc.calibration.newTask")}</Btn>}>
        <p className="v3-std-muted" style={{ margin: "0 20px 10px" }}>{t("v2.dlc.calibration.sub")}</p>
        {tasks.loading && !tasks.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={3} /></div>
        ) : tasks.error ? (
          <div style={{ padding: 20 }}><Notice s="act"><ApiErrorDetail error={tasks.failure ?? tasks.error} /></Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={t("v2.dlc.calibration.noTasks")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v2.dlc.criteria.key")}</th>
                <th>{t("v2.dlc.calibration.purposeCol")}</th>
                <th>{t("v3.standards.status")}</th>
                <th>{t("v2.dlc.calibration.progress")}</th>
                <th>{t("v3.standards.created")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} className="click" onClick={() => onOpenTask(row.id)}>
                  <td style={{ width: 30 }}><Lamp s={row.status === "closed" ? "ok" : "wait"} /></td>
                  <td className="mono">{row.criterion_key}</td>
                  <td>{t(`v2.dlc.calibration.purpose.${row.purpose}`)}</td>
                  <td><Chip s={row.status === "closed" ? "ok" : "wait"}>{t(`v2.dlc.calibration.taskStatus.${row.status}`)}</Chip></td>
                  <td className="mono" style={{ color: "var(--v3-text-2)" }}>{row.annotators.map((a) => `${a} ${row.progress[a] ?? 0}/${row.total}`).join(" · ")}</td>
                  <td>{stamp(row.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
      {creating && (
        <NewTask key={prefillKey} agentId={agentId} agentName={agentName || agentId || ""} lineageId={lineageId}
          criteriaKeys={criteriaKeys} people={people}
          initialRunId={prefill?.runId} initialKey={prefill?.criterion}
          onClose={() => { setCreating(false); onPrefillDone?.(); }}
          onCreated={(id) => { setCreating(false); setNonce((n) => n + 1); onOpenTask(id); }} />
      )}
    </div>
  );
}
