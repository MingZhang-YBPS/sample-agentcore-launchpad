/**
 * Configure → Cost tags → back-fill (cost-tags design §1.3): the workspace-wide run
 * that gives resources created before the tag set (and the workspace infrastructure)
 * the standard tags. A run is a manifest: the tag set it writes, its two stages,
 * the coverage it reached as one segmented bar, the kinds it found, and the
 * resources it could not tag with why — `denied` names the IAM permission to add.
 * Polled while it is queued or running; resumable, so a restart continues it.
 */
import { Play, RotateCw } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  backfillActive,
  type BackfillRun,
  type BackfillStage,
  type BackfillView,
  costTagsApi,
  type ResourceTagStatus,
} from "../../../lib/api/costTags";
import { ErrorCallout } from "../../../components/ErrorCallout";
import { errorMessage } from "../../../lib/api";
import { useFailure } from "../../failure";
import { ago } from "../../format";
import { ErrorCallout } from "../../../components/ErrorCallout";
import { useFailure } from "../../failure";
import { useToast } from "../../hooks";
import { Btn, Chip, Filters, Lamp, Notice, Panel, type Signal, Track } from "../../ui";
import { coverageSegments, kindRows, RESOURCE_SIGNAL, resourceLabel, sortOpen } from "./model";

export const BACKFILL_POLL_MS = 3000;
const OPEN_PAGE = 50;

const RUN_SIGNAL: Record<BackfillRun["status"], Signal> = { queued: "info", running: "info", succeeded: "ok", failed: "act" };
const STAGE_SIGNAL: Record<BackfillStage["status"], Signal> = { pending: "off", running: "info", succeeded: "ok", failed: "act" };

/** The coverage of one run: the figure, the segmented bar and its legend. */
export function BackfillCoverageBar({ run }: { run: BackfillRun }) {
  const { t } = useTranslation();
  const cov = run.coverage;
  const segments = coverageSegments(cov);
  const live = backfillActive(run);
  return (
    <div className="v3-bf-cov" data-testid="cost-tags-coverage">
      <div className="v3-bf-fig">
        <span className="v">
          {cov.percent === null ? "—" : cov.percent}
          {cov.percent !== null && <small>%</small>}
        </span>
        <span className="sub">
          {cov.total === 0 && !live
            ? t("v3.costTags.bf.nothingFound")
            : t("v3.costTags.bf.coverageSub", { tagged: cov.tagged, taggable: cov.taggable, total: cov.total })}
        </span>
      </div>
      <div className="v3-bf-bar" data-live={live || undefined} role="img"
        aria-label={t("v3.costTags.bf.coverageAria", { percent: cov.percent ?? 0, tagged: cov.tagged, taggable: cov.taggable })}>
        {segments.map((s) => (
          <i key={s.status} data-r={s.status} style={{ width: `${s.pct.toFixed(2)}%` }} title={`${t(`v3.costTags.bf.result.${s.status}`)} ${s.count}`} />
        ))}
      </div>
      <div className="v3-ctag-legend">
        {segments.map((s) => (
          <span key={s.status}><Lamp s={RESOURCE_SIGNAL[s.status]} />{t(`v3.costTags.bf.result.${s.status}`)} <b className="mono">{s.count}</b></span>
        ))}
      </div>
    </div>
  );
}

function KindTable({ run }: { run: BackfillRun }) {
  const { t } = useTranslation();
  const rows = kindRows(run.coverage);
  if (rows.length === 0) return null;
  return (
    <ul className="v3-bf-kinds" aria-label={t("v3.costTags.bf.byKind")}>
      {rows.map((row) => {
        const taggable = row.total - row.unsupported;
        return (
          <li key={row.kind} data-open={row.open > 0 || undefined}>
            <span className="nm">{t(`v3.costTags.bf.kind.${row.kind}`, { defaultValue: row.kind })}</span>
            <span className="meter" aria-hidden="true">
              <i style={{ width: `${taggable ? (row.tagged / taggable) * 100 : 0}%` }} />
            </span>
            <span className="n mono">
              {taggable ? `${row.tagged}/${taggable}` : t("v3.costTags.bf.noTags")}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

function OpenResources({ run }: { run: BackfillRun }) {
  const { t } = useTranslation();
  const [filter, setFilter] = useState<"all" | ResourceTagStatus>("all");
  const [shown, setShown] = useState(OPEN_PAGE);
  const all = sortOpen(run.resources);
  if (all.length === 0) return null;
  const present = [...new Set(all.map((r) => r.status))];
  const rows = all.filter((r) => filter === "all" || r.status === filter);
  return (
    <div className="v3-bf-open" data-testid="cost-tags-open">
      <div className="v3-bf-open-head">
        <b>{t("v3.costTags.bf.openTitle", { count: all.length })}</b>
        {present.length > 1 && (
          <Filters value={filter} onChange={(v) => { setFilter(v); setShown(OPEN_PAGE); }}
            options={[{ value: "all" as const, label: t("v3.costTags.bf.all") },
              ...present.map((s) => ({ value: s, s: RESOURCE_SIGNAL[s], label: t(`v3.costTags.bf.result.${s}`), count: all.filter((r) => r.status === s).length }))]} />
        )}
      </div>
      {all.some((r) => r.status === "denied") && <Notice s="act">{t("v3.costTags.bf.deniedHint")}</Notice>}
      <div className="v3-table-scroll">
        <table className="v3-table">
          <thead>
            <tr>
              <th>{t("v3.costTags.bf.col.result")}</th>
              <th>{t("v3.costTags.bf.col.kind")}</th>
              <th>{t("v3.costTags.bf.col.resource")}</th>
              <th>{t("v3.costTags.bf.col.detail")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, shown).map((r) => (
              <tr key={r.key} data-r={r.status}>
                <td><span style={{ display: "inline-flex", gap: 8, alignItems: "center", whiteSpace: "nowrap" }}><Lamp s={RESOURCE_SIGNAL[r.status]} />{t(`v3.costTags.bf.result.${r.status}`)}</span></td>
                <td className="mono" style={{ color: "var(--v3-text-3)", whiteSpace: "nowrap" }}>{t(`v3.costTags.bf.kind.${r.kind}`, { defaultValue: r.kind })}</td>
                <td><span className="v3-bf-res mono" title={r.arn || r.name}>{resourceLabel(r)}</span></td>
                <td className="v3-bf-detail">{r.detail || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length > shown && (
        <div><Btn size="sm" kind="ghost" onClick={() => setShown((n) => n + OPEN_PAGE)}>{t("v3.costTags.bf.more", { count: rows.length - shown })}</Btn></div>
      )}
    </div>
  );
}

/** One run's manifest: stages, coverage, kinds, what it could not tag. */
export function BackfillRunView({ run }: { run: BackfillRun }) {
  const { t } = useTranslation();
  const stage = (name: "plan" | "tag") => run.stages.find((s) => s.name === name);
  const nodes = (["plan", "tag"] as const).map((name) => {
    const s = stage(name);
    const status = s?.status ?? "pending";
    return {
      key: name,
      s: STAGE_SIGNAL[status],
      here: status === "running" || status === "failed",
      label: t(`v3.costTags.bf.stage.${name}`),
      detail: s?.detail || t(`v3.costTags.bf.stageStatus.${status}`),
    };
  });
  return (
    <div className="v3-bf" data-status={run.status}>
      <div className="v3-bf-top">
        <BackfillCoverageBar run={run} />
        <div className="v3-bf-stages">
          <Track nodes={nodes} />
          <small className="v3-hint">
            {t("v3.costTags.bf.by", {
              actor: run.actor ?? "—",
              when: run.created_at ? ago(run.created_at) : "—",
              reason: t(`v3.costTags.bf.reason.${run.reason ?? "admin"}`, { defaultValue: run.reason ?? "" }),
            })}
          </small>
        </div>
      </div>
      {run.status === "failed" && run.error && <Notice s="act">{t("v3.costTags.bf.failed", { error: run.error })}</Notice>}
      <KindTable run={run} />
      <OpenResources run={run} />
    </div>
  );
}

/** The back-fill panel: start a run, follow it while it runs, read the latest. */
export function Backfill() {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const [view, setView] = useState<BackfillView | null>(null);
  const [failure, setFailure] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const failed = useFailure();
  const [tick, setTick] = useState(0);
  const run = view?.run ?? null;
  const active = backfillActive(run);

  useEffect(() => {
    let live = true;
    costTagsApi.backfill()
      .then((next) => { if (live) { setView(next); setFailure(null); } })
      .catch((err: unknown) => { if (live) setFailure(err); });
    return () => { live = false; };
  }, [tick]);
  useEffect(() => {
    if (!active) return;
    const timer = window.setTimeout(() => setTick((n) => n + 1), BACKFILL_POLL_MS);
    return () => window.clearTimeout(timer);
  }, [active, view]);

  const start = async () => {
    setBusy(true);
    failed.clear();
    try {
      const started = await costTagsApi.startBackfill();
      setView((prev) => (prev ? { ...prev, run: started } : prev));
      toast("ok", t("v3.costTags.bf.started"));
      setTick((n) => n + 1);
    } catch (err) {
      failed.fail(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel title={t("v3.costTags.backfill")} signal={run ? RUN_SIGNAL[run.status] : undefined}
      end={
        <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
          {run && <Chip s={RUN_SIGNAL[run.status]}>{t(`v3.costTags.bf.status.${run.status}`)}</Chip>}
          <Btn size="sm" kind={run ? undefined : "primary"} busy={busy} disabled={active || !view}
            disabledReason={active ? t("v3.costTags.bf.runningReason") : t("story.reason.notLoaded")} onClick={() => void start()}>
            {run ? <RotateCw size={13} /> : <Play size={13} />} {run ? t("v3.costTags.bf.rerun") : t("v3.costTags.bf.start")}
          </Btn>
        </span>
      }>
      <div data-testid="cost-tags-backfill" style={{ display: "grid", gap: 14 }}>
        <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="cost-tags-backfill-error" />
        {failure != null && !view ? (
          <Notice s="act">{errorMessage(failure)}</Notice>
        ) : !view ? (
          <div className="v3-bf-slot" aria-busy="true" />
        ) : (
          <>
            <div className="v3-bf-set">
              <span className="v3-hint">{t("v3.costTags.bf.writes")}</span>
              {view.tag_keys.map((key) => <span key={key} className="v3-ctag">{key}</span>)}
              <span className="v3-hint">{t("v3.costTags.bf.setHint", { shared: view.shared_project, max: view.max_cost_tags })}</span>
            </div>
            {run ? (
              <BackfillRunView run={run} />
            ) : (
              <div className="v3-ctag-slot">
                <b>{t("v3.costTags.bf.none")}</b>
                <span>{t("v3.costTags.backfillBody")}</span>
              </div>
            )}
          </>
        )}
      </div>
    </Panel>
  );
}
