/**
 * Calibration: "the judge agrees with us" as a measurement. Three numbers — human–
 * human κ, judge–human κ with its interval, and the confusion cells. Labelling is
 * blind (the judge's verdict is withheld from an annotator until the task closes),
 * a task needs at least two annotators, and `aligned` is refused by the server when
 * the numbers do not support it or when the decider labelled the evidence.
 */
import { Copy, Plus } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { dlcApi, errorMessage } from "../../../lib/api";
import type { AnnotationTask } from "../../../lib/dlc";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Dialog, Empty, Lamp, Notice, Panel, Skeleton, Stat } from "../../ui";
import { ConfusionMatrix } from "./charts";
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
                          .catch((err: unknown) => toast("act", errorMessage(err)))
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
                .catch((err: unknown) => toast("act", errorMessage(err)))
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

  const act = (fn: () => Promise<unknown>, okKey?: string) => {
    setBusy(true);
    fn()
      .then(() => { if (okKey) toast("ok", t(okKey)); setNonce((n) => n + 1); })
      .catch((err: unknown) => toast("act", errorMessage(err)))
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

function NewTask({ agentId, lineageId, criteriaKeys, people, onClose, onCreated }: {
  agentId: string | null;
  lineageId: string | null;
  criteriaKeys: string[];
  people: string[];
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const [key, setKey] = useState(criteriaKeys[0] ?? "");
  const [runId, setRunId] = useState("");
  const [annotators, setAnnotators] = useState<string[]>([]);
  const [adjudicator, setAdjudicator] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <Dialog
      wide
      title={t("v2.dlc.calibration.newTaskTitle")}
      onClose={onClose}
      foot={
        <>
          <Btn kind="ghost" onClick={onClose}>{t("v3.common.cancel")}</Btn>
          {/* two raters at least: one person's labels are not a consensus */}
          <Btn kind="primary" disabled={busy || !key || annotators.length < 2 || !runId}
            onClick={() => {
              setBusy(true);
              dlcApi
                .createTask({ agent_id: agentId, criteria_lineage_id: lineageId, criterion_key: key, annotators, adjudicator: adjudicator || null, run_id: runId })
                .then((created) => { toast("ok", t("v2.dlc.calibration.taskCreated")); onCreated(created.id); })
                .catch((err: unknown) => toast("act", errorMessage(err)))
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
            <select className="v3-select" value={key} onChange={(e) => setKey(e.target.value)}>
              <option value="">{t("v2.dlc.calibration.pickCriterion")}</option>
              {criteriaKeys.map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
          </label>
          <label className="v3-field">
            <span>{t("v2.dlc.calibration.fromRun")}</span>
            <input className="v3-input mono" value={runId} onChange={(e) => setRunId(e.target.value)} />
            <small className="v3-hint">{t("v2.dlc.calibration.fromRunHint")}</small>
          </label>
        </div>
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
  );
}

export function Calibration({ agentId, lineageId, criteriaKeys, people, taskId, onOpenTask }: {
  agentId: string | null;
  lineageId: string | null;
  criteriaKeys: string[];
  people: string[];
  taskId: string | null;
  onOpenTask: (id: string | null) => void;
}) {
  const { t } = useTranslation();
  const [nonce, setNonce] = useState(0);
  const [creating, setCreating] = useState(false);
  const tasks = useLoad<{ tasks: AnnotationTask[] }>(() => dlcApi.listTasks({ agentId: agentId ?? undefined }), `v3-std-tasks:${agentId ?? ""}:${nonce}`);
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
          <div style={{ padding: 20 }}><Notice s="act">{tasks.error}</Notice></div>
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
        <NewTask agentId={agentId} lineageId={lineageId} criteriaKeys={criteriaKeys} people={people}
          onClose={() => setCreating(false)}
          onCreated={(id) => { setCreating(false); setNonce((n) => n + 1); onOpenTask(id); }} />
      )}
    </div>
  );
}
