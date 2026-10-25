/**
 * Golden sets. Three splits, three purposes: dev to iterate, regression for the gate
 * to replay, and the holdout — written exactly once, by `seed`, then sealed (the
 * server refuses any other write to it). Coverage is criteria × case tier, because a
 * criterion with two items reports a rate that is noise.
 */
import { Lock, Plus, Sprout } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { ErrorCallout } from "../../../components/ErrorCallout";
import { useAuth } from "../../../auth/auth-context";
import { dlcApi } from "../../../lib/api";
import type { GoldenSet, GoldenSplit, WritableSplit } from "../../../lib/dlc";
import { useLoad, useToast } from "../../hooks";
import type { NextStepNote } from "../../story/flow";
import { useNextStep } from "../../story/useNextStep";
import { StoryPrereqs } from "../../story/Prereqs";
import { SharedFromTag } from "../../shares/SharedFromTag";
import { Btn, Chip, Dialog, Empty, Lamp, Notice, Panel, Skeleton, Stat } from "../../ui";
import { CoverageMatrix } from "./charts";
import { biasText } from "./common";
import { ImportDialog } from "./golden/ImportDialog";
import { Select } from "../../select";

const SPLITS: GoldenSplit[] = ["dev", "regression", "holdout"];

export function GoldenIndex({ lineages, onOpen }: { lineages: { value: string; label: string }[]; onOpen: (id: string) => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const list = useLoad(() => dlcApi.listGolden(), "v3-std-golden");
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [lineage, setLineage] = useState("");
  const [busy, setBusy] = useState(false);
  const rows = list.data?.golden_sets ?? [];
  const [failure, setFailure] = useState<unknown>(null);
  const agentId = useSearchParams()[0].get("agent");

  const create = () => {
    setBusy(true);
    setFailure(null);
    dlcApi
      .createGolden({ name, criteria_lineage_id: lineage || null })
      .then((created) => {
        toast("ok", t("v2.dlc.golden.created"));
        setCreating(false);
        onOpen(created.id);
      })
      .catch((err: unknown) => setFailure(err))
      .finally(() => setBusy(false));
  };

  return (
    <>
      <Panel title={t("v2.dlc.golden.indexTitle")} flush
        end={<Btn size="sm" kind="primary" onClick={() => { setName(""); setLineage(""); setCreating(true); }}><Plus size={13} /> {t("v2.dlc.golden.create")}</Btn>}>
        {list.loading && !list.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={3} /></div>
        ) : list.error ? (
          <div style={{ padding: 20 }}><Notice s="act"><ApiErrorDetail error={list.failure ?? list.error} /></Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={t("v2.dlc.golden.noSets")}>
            {t("v2.dlc.golden.indexSub")}
            <StoryPrereqs agentId={agentId} stepKey="evaluate.golden.seal" />
          </Empty>
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v3.standards.name")}</th>
                <th className="num">{t("v2.dlc.split.dev")}</th>
                <th className="num">{t("v2.dlc.split.regression")}</th>
                <th>{t("v2.dlc.split.holdout")}</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const sealed = (row.splits.holdout?.items ?? 0) > 0;
                return (
                  <tr key={row.id} className="click" onClick={() => onOpen(row.id)}>
                    <td style={{ width: 30 }}><Lamp s={sealed ? "ok" : "wait"} /></td>
                    <td><span className="v3-lent-row"><b>{row.name}</b><SharedFromTag row={row} /></span></td>
                    <td className="num">{row.splits.dev?.active_items ?? 0}</td>
                    <td className="num">{row.splits.regression?.active_items ?? 0}</td>
                    <td>
                      {sealed ? (
                        <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
                          <span className="mono">{row.splits.holdout?.active_items ?? 0}</span>
                          <Chip s="ok"><Lock size={11} /> {t("v2.dlc.golden.sealed")}</Chip>
                        </span>
                      ) : (
                        <Chip s="wait">{t("v2.dlc.golden.unsealed")}</Chip>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>
      {creating && (
        <Dialog
          title={t("v2.dlc.golden.createTitle")}
          onClose={() => { setCreating(false); setFailure(null); }}
          foot={
            <>
              <Btn kind="ghost" onClick={() => setCreating(false)}>{t("v3.common.cancel")}</Btn>
              <Btn kind="primary" busy={busy} disabled={name.trim() === ""} disabledReason={t("story.reason.fillName")} onClick={create}>{t("v3.standards.save")}</Btn>
            </>
          }
        >
          <div style={{ display: "grid", gap: 12 }}>
            <label className="v3-field">
              <span>{t("v3.standards.name")}</span>
              <input className="v3-input" value={name} maxLength={64} onChange={(e) => setName(e.target.value)} />
            </label>
            <label className="v3-field">
              <span>{t("v2.dlc.golden.forCriteria")}</span>
              <Select className="v3-select" value={lineage} onChange={(e) => setLineage(e.target.value)}>
                <option value="">{t("v2.dlc.golden.pickCriteria")}</option>
                {lineages.map((l) => <option key={l.value} value={l.value}>{l.label}</option>)}
              </Select>
              <small className="v3-hint">{t("v2.dlc.golden.forCriteriaHint")}</small>
            </label>
            <ErrorCallout error={failure} onDismiss={() => setFailure(null)} data-testid="v3-golden-create-error" />
          </div>
        </Dialog>
      )}
    </>
  );
}

export function GoldenCurator({ goldenSet, onReload, onBack }: { goldenSet: GoldenSet; onReload: () => void; onBack: () => void }) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState<WritableSplit | null>(null);
  const [seeding, setSeeding] = useState(false);
  const [retiring, setRetiring] = useState<{ split: GoldenSplit; scenario: string } | null>(null);
  const [reason, setReason] = useState("");
  const coverage = goldenSet.coverage;
  const sealed = (goldenSet.splits.holdout?.items ?? 0) > 0;
  const mayEdit = can("criteria.manage");
  const maySeed = can("golden.admit");

  // add / seed / retire fail in place, with the way out (storyline-ux §4); sealing
  // the holdout says what comes next instead (§3.1)
  const [failure, setFailure] = useState<unknown>(null);
  const nextStep = useNextStep();
  const agentId = useSearchParams()[0].get("agent");
  const run = async (fn: () => Promise<unknown>, okKey: string, note?: NextStepNote) => {
    setBusy(true);
    setFailure(null);
    nextStep.clear();
    try {
      await fn();
      if (note) nextStep.show(note);
      else toast("ok", t(okKey));
      onReload();
    } catch (err) {
      setFailure(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <div><button type="button" className="v3-btn ghost sm" onClick={onBack}>← {t("v2.dlc.golden.backToList")}</button></div>
      <ErrorCallout error={failure} params={{ dataset_id: goldenSet.id }} onDismiss={() => setFailure(null)}
        data-testid="v3-golden-error" />
      {nextStep.card}
      <Panel
        title={t("v2.dlc.golden.title", { name: goldenSet.name })}
        signal={sealed ? "ok" : "wait"}
        end={sealed ? <Chip s="ok"><Lock size={11} /> {t("v2.dlc.golden.sealed")}</Chip> : <Chip s="wait">{t("v2.dlc.golden.unsealed")}</Chip>}
      >
        <p className="v3-std-muted" style={{ margin: "0 0 12px" }}>
          {goldenSet.criteria_lineage_id ? t("v2.dlc.golden.servesCriteria") : t("v2.dlc.golden.noCriteria")}
        </p>
        {!sealed && (
          <div style={{ marginBottom: 12 }}>
            <Notice s="wait">
              {t("v2.dlc.golden.seedNeeded")}{" "}
              {maySeed && <Btn size="sm" kind="primary" onClick={() => setSeeding(true)}><Sprout size={13} /> {t("v2.dlc.golden.seed")}</Btn>}
            </Notice>
          </div>
        )}
        <div className="v3-grid c3">
          {SPLITS.map((name) => {
            const split = goldenSet.splits[name];
            return (
              <div key={name} className="v3-std-split">
                <Stat label={t(`v2.dlc.split.${name}`)} value={split?.active_items ?? 0}
                  foot={split && split.retired_items > 0 ? t("v2.dlc.golden.retiredCount", { n: split.retired_items }) : t(`v2.dlc.golden.splitPurpose.${name}`)} />
                <div className="meta">
                  <span className="mono">{split?.latest_version ?? "—"}</span>
                  {name === "holdout" ? (
                    <Chip><Lock size={11} /> {t("v2.dlc.golden.readOnly")}</Chip>
                  ) : (
                    mayEdit && (
                      <span style={{ display: "inline-flex", gap: 6 }}>
                        <Btn size="sm" disabled={busy} onClick={() => setAdding(name as WritableSplit)}><Plus size={12} /> {t("v3.standards.add")}</Btn>
                        <Btn size="sm" kind="ghost" busy={busy} disabled={!split} disabledReason={t("story.reason.nothingToRetire")} onClick={() => { setReason(""); setRetiring({ split: name, scenario: "" }); }}>
                          {t("v2.dlc.golden.retireItem")}
                        </Btn>
                      </span>
                    )
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </Panel>

      <Panel title={t("v2.dlc.golden.coverage")} flush={Boolean(coverage)}
        signal={coverage && (coverage.agent_observed_items > 0 || coverage.unmapped_items > 0) ? "wait" : undefined}>
        {coverage ? (
          <>
            <CoverageMatrix rows={coverage.criteria} />
            <div style={{ padding: "16px 20px", display: "grid", gap: 12 }}>
              <div className="v3-grid c3">
                <Stat label={t("v2.dlc.golden.items")} value={coverage.items} />
                <Stat label={t("v2.dlc.golden.unmapped")} value={coverage.unmapped_items} signal={coverage.unmapped_items > 0 ? "wait" : undefined} />
                <Stat label={t("v2.dlc.golden.agentObserved")} value={coverage.agent_observed_items} signal={coverage.agent_observed_items > 0 ? "act" : undefined} />
              </div>
              {coverage.agent_observed_items > 0 && <Notice s="wait">{t("v2.dlc.golden.agentObservedWarn")}</Notice>}
              {coverage.sources.length > 0 && (
                <table className="v3-table">
                  <thead><tr><th>{t("v2.dlc.golden.source")}</th><th className="num">{t("v2.dlc.golden.items")}</th><th>{t("v2.dlc.golden.bias")}</th></tr></thead>
                  <tbody>
                    {coverage.sources.map((s) => (
                      <tr key={s.origin}>
                        <td>{t(`v2.dlc.origin.${s.origin}`, { defaultValue: s.origin })}</td>
                        <td className="num">{s.count}</td>
                        <td className="v3-std-muted">{biasText(t, s.origin, s.bias)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </>
        ) : (
          <p className="v3-std-empty">{t("v2.dlc.golden.noCoverage")}</p>
        )}
      </Panel>

      {adding && (
        <ImportDialog
          mode="add"
          split={adding}
          goldenSet={goldenSet}
          title={t("v2.dlc.golden.addTitle", { split: t(`v2.dlc.split.${adding}`) })}
          hint={t("v2.dlc.golden.addHint")}
          busy={busy}
          onClose={() => setAdding(null)}
          onSubmit={(items) => {
            const split = adding;
            setAdding(null);
            void run(() => dlcApi.addGoldenItems(goldenSet.id, split, items), "v2.dlc.golden.added");
          }}
        />
      )}
      {seeding && (
        <ImportDialog
          mode="seed"
          goldenSet={goldenSet}
          title={t("v2.dlc.golden.seedTitle")}
          hint={t("v2.dlc.golden.seedHint")}
          busy={busy}
          extra={<Notice s="wait">{t("v2.dlc.golden.seedOnce")}</Notice>}
          onClose={() => setSeeding(false)}
          onSubmit={(items) => {
            setSeeding(false);
            void run(() => dlcApi.seedGolden(goldenSet.id, items), "v2.dlc.golden.seeded",
              { done: t("story.done.goldenSealed"), after: "evaluate.golden.seal", agentId, params: { dataset_id: goldenSet.id } });
          }}
        />
      )}
      {retiring && (
        <Dialog
          title={t("v2.dlc.golden.retireTitle")}
          onClose={() => setRetiring(null)}
          foot={
            <>
              <Btn kind="ghost" onClick={() => setRetiring(null)}>{t("v3.common.cancel")}</Btn>
              <Btn kind="danger" busy={busy} disabled={!retiring.scenario || reason.trim() === ""}
                disabledReason={!retiring.scenario ? t("story.reason.pickScenario") : t("story.reason.fillReason")}
                onClick={() => {
                  const target = retiring;
                  setRetiring(null);
                  void run(() => dlcApi.retireGoldenItem(goldenSet.id, { split: target.split, scenario_id: target.scenario, reason }), "v2.dlc.golden.retired");
                }}>
                {t("v2.dlc.golden.retireItem")}
              </Btn>
            </>
          }
        >
          <div style={{ display: "grid", gap: 12 }}>
            <p style={{ margin: 0 }}>{t("v2.dlc.golden.retireHint")}</p>
            <label className="v3-field">
              <span>{t("v2.dlc.golden.scenarioId")}</span>
              <input className="v3-input mono" value={retiring.scenario} onChange={(e) => setRetiring({ ...retiring, scenario: e.target.value })} />
            </label>
            <label className="v3-field">
              <span>{t("v2.dlc.golden.retireReason")}</span>
              <input className="v3-input" value={reason} onChange={(e) => setReason(e.target.value)} />
            </label>
          </div>
        </Dialog>
      )}
    </div>
  );
}
