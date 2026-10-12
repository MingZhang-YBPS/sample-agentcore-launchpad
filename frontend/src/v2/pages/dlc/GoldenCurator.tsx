/**
 * The golden-set curator — §7.3.
 *
 * Three splits serve three different purposes, and the page is built so a person
 * cannot quietly destroy that: dev is for iterating, regression is what the gate
 * replays, and the holdout is written exactly once at curation time and then sealed.
 * Coverage is shown as criteria × case tier because a criterion with two items
 * reports a rate that is noise, and the source mix is shown with its bias spelled
 * out — a set built only from past traffic has no long tail in it.
 */
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { dlcApi, errorMessage } from "../../../lib/api";
import {
  CASE_TIERS,
  type CaseTier,
  type GoldenItem,
  type GoldenSet,
  type GoldenSplit,
  type WritableSplit,
} from "../../../lib/dlc";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Card, Confirm, Field, Kpi, LinkButton, Modal, Select, Tag } from "../../ui";
import { CoverageMatrix } from "./charts";

const SPLIT_ORDER: GoldenSplit[] = ["dev", "regression", "holdout"];

function parseItems(text: string): GoldenItem[] {
  /* One item per line: `scenario_id | question | expected answer`. A JSON array is
   * accepted too, for a set exported from somewhere else. */
  const trimmed = text.trim();
  if (trimmed.startsWith("[")) return JSON.parse(trimmed) as GoldenItem[];
  return trimmed
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line, index) => {
      const [id, question, expected] = line.split("|").map((p) => p.trim());
      return {
        scenario_id: id || `item-${index + 1}`,
        turns: [{ input: question ?? id, expected_response: expected ?? "" }],
      };
    });
}

function ItemsModal({
  open,
  title,
  hint,
  busy,
  onClose,
  onSubmit,
  extra,
}: {
  open: boolean;
  title: string;
  hint: string;
  busy: boolean;
  onClose: () => void;
  onSubmit: (items: GoldenItem[], caseTier: CaseTier) => void;
  extra?: React.ReactNode;
}) {
  const { t } = useTranslation();
  const [text, setText] = useState("");
  const [caseTier, setCaseTier] = useState<CaseTier>("known_bad");
  const [parseError, setParseError] = useState<string | null>(null);
  return (
    <Modal
      open={open}
      title={title}
      wide
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
          <Button
            kind="primary"
            disabled={busy || text.trim() === ""}
            onClick={() => {
              try {
                const items = parseItems(text);
                setParseError(null);
                onSubmit(items, caseTier);
              } catch (error) {
                setParseError(errorMessage(error));
              }
            }}
          >
            {t("v2.common.save")}
          </Button>
        </>
      }
    >
      <p className="v2-muted">{hint}</p>
      {extra}
      <Field label={t("v2.dlc.golden.caseTier")} hint={t("v2.dlc.golden.caseTierHint")}>
        <Select
          value={caseTier}
          onChange={(v) => setCaseTier(v as CaseTier)}
          options={CASE_TIERS.map((c) => ({ value: c, label: t(`v2.dlc.caseTier.${c}`) }))}
        />
      </Field>
      <Field label={t("v2.dlc.golden.itemsField")} hint={t("v2.dlc.golden.itemsHint")} error={parseError}>
        <textarea className="v2-input mono" rows={10} value={text} onChange={(e) => setText(e.target.value)} />
      </Field>
    </Modal>
  );
}

export function GoldenCurator({
  goldenSet,
  onReload,
}: {
  goldenSet: GoldenSet;
  onReload: () => void;
}) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useV2Toast();
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState<WritableSplit | null>(null);
  const [seeding, setSeeding] = useState(false);
  const [retiring, setRetiring] = useState<{ split: GoldenSplit; scenario: string } | null>(null);
  const [retireReason, setRetireReason] = useState("");
  const coverage = goldenSet.coverage;
  const holdout = goldenSet.splits.holdout;
  const sealed = (holdout?.items ?? 0) > 0;
  const mayEdit = can("criteria.manage");
  const maySeed = can("golden.admit");

  const run = async (fn: () => Promise<unknown>, okKey: string) => {
    setBusy(true);
    try {
      await fn();
      toast("success", t(okKey));
      onReload();
    } catch (error) {
      toast("error", errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Card
        title={t("v2.dlc.golden.title", { name: goldenSet.name })}
        sub={goldenSet.criteria_lineage_id ? t("v2.dlc.golden.servesCriteria") : t("v2.dlc.golden.noCriteria")}
        end={
          sealed ? (
            <Tag tone="green">{t("v2.dlc.golden.sealed")}</Tag>
          ) : (
            <Tag tone="orange">{t("v2.dlc.golden.unsealed")}</Tag>
          )
        }
      >
        {!sealed && (
          <Alert
            action={
              maySeed ? (
                <LinkButton onClick={() => setSeeding(true)}>{t("v2.dlc.golden.seed")}</LinkButton>
              ) : undefined
            }
          >
            {t("v2.dlc.golden.seedNeeded")}
          </Alert>
        )}
        <div className="v2-dlc-kpis">
          {SPLIT_ORDER.map((name) => {
            const split = goldenSet.splits[name];
            return (
              <Kpi
                key={name}
                label={t(`v2.dlc.split.${name}`)}
                value={String(split?.active_items ?? 0)}
                sub={
                  split && split.retired_items > 0
                    ? t("v2.dlc.golden.retiredCount", { n: split.retired_items })
                    : t(`v2.dlc.golden.splitPurpose.${name}`)
                }
              />
            );
          })}
        </div>
        <div className="v2-dlc-actions">
          {mayEdit &&
            (["dev", "regression"] as WritableSplit[]).map((name) => (
              <Button key={name} size="sm" disabled={busy} onClick={() => setAdding(name)}>
                {t("v2.dlc.golden.addTo", { split: t(`v2.dlc.split.${name}`) })}
              </Button>
            ))}
        </div>
      </Card>

      <Card title={t("v2.dlc.golden.coverage")} sub={t("v2.dlc.golden.coverageSub")}>
        {coverage ? (
          <>
            <CoverageMatrix rows={coverage.criteria} />
            <div className="v2-dlc-kpis">
              <Kpi label={t("v2.dlc.golden.items")} value={String(coverage.items)} />
              <Kpi
                label={t("v2.dlc.golden.unmapped")}
                value={String(coverage.unmapped_items)}
                tone={coverage.unmapped_items > 0 ? "bad" : undefined}
              />
              <Kpi
                label={t("v2.dlc.golden.agentObserved")}
                value={String(coverage.agent_observed_items)}
                tone={coverage.agent_observed_items > 0 ? "bad" : undefined}
              />
            </div>
            {coverage.agent_observed_items > 0 && (
              <Alert tone="warn">{t("v2.dlc.golden.agentObservedWarn")}</Alert>
            )}
            {coverage.sources.length > 0 && (
              <table className="v2-table dense">
                <thead>
                  <tr>
                    <th>{t("v2.dlc.golden.source")}</th>
                    <th>{t("v2.dlc.golden.items")}</th>
                    <th>{t("v2.dlc.golden.bias")}</th>
                  </tr>
                </thead>
                <tbody>
                  {coverage.sources.map((s) => (
                    <tr key={s.origin}>
                      <td>{t(`v2.dlc.origin.${s.origin}`, { defaultValue: s.origin })}</td>
                      <td>{s.count}</td>
                      <td className="v2-muted">{s.bias}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        ) : (
          <p className="v2-dlc-empty">{t("v2.dlc.golden.noCoverage")}</p>
        )}
      </Card>

      <Card title={t("v2.dlc.golden.splits")}>
        <table className="v2-table">
          <thead>
            <tr>
              <th>{t("v2.dlc.golden.split")}</th>
              <th>{t("v2.dlc.golden.active")}</th>
              <th>{t("v2.dlc.golden.retired")}</th>
              <th>{t("v2.dlc.golden.awsVersion")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {SPLIT_ORDER.map((name) => {
              const split = goldenSet.splits[name];
              if (!split) return null;
              return (
                <tr key={name}>
                  <td>
                    {t(`v2.dlc.split.${name}`)}
                    {name === "holdout" && <Tag tone="gray">{t("v2.dlc.golden.readOnly")}</Tag>}
                  </td>
                  <td>{split.active_items}</td>
                  <td>{split.retired_items}</td>
                  <td className="mono">{split.latest_version ?? "—"}</td>
                  <td>
                    {mayEdit && name !== "holdout" && (
                      <LinkButton
                        onClick={() => {
                          setRetireReason("");
                          setRetiring({ split: name, scenario: "" });
                        }}
                      >
                        {t("v2.dlc.golden.retireItem")}
                      </LinkButton>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Card>

      <ItemsModal
        open={adding !== null}
        title={t("v2.dlc.golden.addTitle", { split: adding ? t(`v2.dlc.split.${adding}`) : "" })}
        hint={t("v2.dlc.golden.addHint")}
        busy={busy}
        onClose={() => setAdding(null)}
        onSubmit={(items, caseTier) => {
          const split = adding;
          setAdding(null);
          if (!split) return;
          void run(
            () =>
              dlcApi.addGoldenItems(
                goldenSet.id,
                split,
                items.map((i) => ({ ...i, metadata: { dlc: { case_tier: caseTier } } })),
              ),
            "v2.dlc.golden.added",
          );
        }}
      />

      <ItemsModal
        open={seeding}
        title={t("v2.dlc.golden.seedTitle")}
        hint={t("v2.dlc.golden.seedHint")}
        busy={busy}
        onClose={() => setSeeding(false)}
        extra={<Alert tone="warn">{t("v2.dlc.golden.seedOnce")}</Alert>}
        onSubmit={(items, caseTier) => {
          setSeeding(false);
          void run(
            () =>
              dlcApi.seedGolden(
                goldenSet.id,
                items.map((i) => ({ ...i, metadata: { dlc: { case_tier: caseTier } } })),
              ),
            "v2.dlc.golden.seeded",
          );
        }}
      />

      <Modal
        open={retiring !== null}
        title={t("v2.dlc.golden.retireTitle")}
        onClose={() => setRetiring(null)}
        footer={
          <>
            <Button onClick={() => setRetiring(null)}>{t("v2.common.cancel")}</Button>
            <Button
              kind="primary"
              disabled={busy || !retiring?.scenario || retireReason.trim() === ""}
              onClick={() => {
                const target = retiring;
                setRetiring(null);
                if (!target) return;
                void run(
                  () =>
                    dlcApi.retireGoldenItem(goldenSet.id, {
                      split: target.split,
                      scenario_id: target.scenario,
                      reason: retireReason,
                    }),
                  "v2.dlc.golden.retired",
                );
              }}
            >
              {t("v2.dlc.golden.retireItem")}
            </Button>
          </>
        }
      >
        <p className="v2-muted">{t("v2.dlc.golden.retireHint")}</p>
        <Field label={t("v2.dlc.golden.scenarioId")}>
          <input
            className="v2-input mono"
            value={retiring?.scenario ?? ""}
            onChange={(e) => setRetiring((prev) => (prev ? { ...prev, scenario: e.target.value } : prev))}
          />
        </Field>
        <Field label={t("v2.dlc.golden.retireReason")} required>
          <input className="v2-input" value={retireReason} onChange={(e) => setRetireReason(e.target.value)} />
        </Field>
      </Modal>
    </>
  );
}

/** Create a golden set for one criteria lineage. */
export function GoldenCreate({
  lineages,
  onCreated,
}: {
  lineages: { value: string; label: string }[];
  onCreated: (id: string) => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [lineage, setLineage] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <>
      <Button kind="primary" size="sm" onClick={() => setOpen(true)}>
        {t("v2.dlc.golden.create")}
      </Button>
      <Confirm
        open={open}
        title={t("v2.dlc.golden.createTitle")}
        confirmLabel={t("v2.common.save")}
        busy={busy}
        onClose={() => setOpen(false)}
        onConfirm={() => {
          setBusy(true);
          dlcApi
            .createGolden({ name, criteria_lineage_id: lineage || null })
            .then((created) => {
              toast("success", t("v2.dlc.golden.created"));
              setOpen(false);
              onCreated(created.id);
            })
            .catch((error: unknown) => toast("error", errorMessage(error)))
            .finally(() => setBusy(false));
        }}
        body={
          <>
            <Field label={t("v2.common.name")} required>
              <input className="v2-input" value={name} maxLength={64} onChange={(e) => setName(e.target.value)} />
            </Field>
            <Field label={t("v2.dlc.golden.forCriteria")} hint={t("v2.dlc.golden.forCriteriaHint")}>
              <Select
                value={lineage}
                onChange={setLineage}
                placeholder={t("v2.dlc.golden.pickCriteria")}
                options={lineages}
              />
            </Field>
          </>
        }
      />
    </>
  );
}
