import { ArrowLeft, Pencil } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { api, type EvaluatorDetail } from "../../../lib/api";
import { evaluatorLabel } from "../../../lib/evaluators";
import { useLoad } from "../../hooks";
import { Chip, Lamp, Notice, PageHead, Panel, Skeleton, Stat } from "../../ui";
import { evaluatorSignal, isManaged } from "./common";

/** One evaluator: what it judges, at which level, with which model or Lambda, on which scale. */
export function EvaluatorView({ id }: { id: string }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const managed = isManaged(id);
  const list = useLoad(() => api.v2Evaluators(), "v3-evaluators");
  const detail = useLoad<EvaluatorDetail | null>(() => (managed ? Promise.resolve(null) : api.v2Evaluator(id)), `v3-evaluator:${id}`);
  const row = list.data?.evaluators.find((e) => e.id === id) ?? null;
  const d = detail.data;
  const name = managed ? evaluatorLabel(t, id) : (d?.name ?? row?.name ?? id);
  const level = d?.level ?? row?.level ?? null;

  if ((detail.loading && !d && !managed) || (list.loading && !list.data)) return <Skeleton rows={6} />;
  if (detail.error) return <Notice s="act">{detail.error}</Notice>;
  const s = row ? evaluatorSignal(row) : "info";

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <div>
        <button type="button" className="v3-btn ghost sm" onClick={() => setParams({})}>
          <ArrowLeft size={14} /> {t("v2.evaluators.title")}
        </button>
      </div>
      <PageHead
        eyebrow={`${row ? t(`v2.evaluators.source.${row.source}`) : "—"} · ${id}`}
        title={
          <span style={{ display: "inline-flex", alignItems: "center", gap: 14 }}>
            <Lamp s={s} />
            {name}
          </span>
        }
        sub={d?.description || undefined}
        end={
          row?.source === "custom" ? (
            <Link to={`/v3/evaluators?view=edit&id=${encodeURIComponent(id)}`} className="v3-btn primary"><Pencil size={14} /> {t("v3.evaluators.edit")}</Link>
          ) : undefined
        }
      />
      {managed && <Notice>{t("v2.evaluators.managedHint")}</Notice>}
      <div className="v3-grid c4">
        <Panel><Stat label={t("v2.evaluators.colLevel")} value={<span style={{ fontSize: 22 }}>{level ? t(`v2.level.${level}`, { defaultValue: level }) : "—"}</span>} /></Panel>
        <Panel>
          <Stat label={t("v2.evaluators.colDefinition")}
            value={<span style={{ fontSize: 22 }}>{d?.definition ? t(`v2.evaluators.definition.${d.definition}`) : t("v2.evaluators.definition.managed")}</span>} />
        </Panel>
        <Panel signal={row?.requires_ground_truth ? "wait" : undefined}>
          <Stat label={t("v2.evaluators.colGroundTruth")} value={<span style={{ fontSize: 22 }}>{row?.requires_ground_truth ? t("v2.evaluators.needsGt") : t("v2.common.no")}</span>} />
        </Panel>
        <Panel><Stat label={t("v3.evaluators.status")} value={<span style={{ fontSize: 22 }}>{d?.status ?? row?.status ?? "—"}</span>} /></Panel>
      </div>
      <Panel title={t("v2.evaluators.basic")}>
        <dl className="v3-kv">
          <dt>ID</dt><dd className="mono">{id}</dd>
          <dt>{t("v2.evaluators.model")}</dt><dd className="mono">{d?.model_id ?? "—"}</dd>
          {d?.base_evaluator_id && <><dt>{t("v2.evaluators.base")}</dt><dd className="mono">{d.base_evaluator_id}</dd></>}
          {d?.lambda_arn && (
            <>
              <dt>{t("v2.evaluators.lambda")}</dt><dd className="mono">{d.lambda_arn}</dd>
              <dt>{t("v2.evaluators.timeout")}</dt><dd className="mono">{`${d.lambda_timeout_s ?? 60}s`}</dd>
            </>
          )}
          {(row?.provider || d?.provider) && <><dt>{t("v3.evaluators.provider")}</dt><dd>{row?.provider ?? d?.provider}</dd></>}
        </dl>
      </Panel>
      {d?.instructions && (
        <Panel title={t("v2.evaluators.rubric")}>
          <pre className="v3-pre">{d.instructions}</pre>
        </Panel>
      )}
      {d && d.rating_scale.length > 0 && (
        <Panel title={t("v2.evaluators.scale")} flush>
          <table className="v3-table">
            <thead><tr><th className="num">{t("v2.evaluators.scaleValue")}</th><th>{t("v2.evaluators.scaleLabel")}</th><th>{t("v2.evaluators.scaleDefinition")}</th></tr></thead>
            <tbody>
              {d.rating_scale.map((p) => (
                <tr key={`${p.value}:${p.label}`}>
                  <td className="num mono">{p.value}</td>
                  <td><Chip>{p.label}</Chip></td>
                  <td style={{ color: "var(--v3-text-2)" }}>{p.definition}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Panel>
      )}
    </div>
  );
}
