import { Cpu, RefreshCw } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, errorMessage, type SystemPresetInfo, type SystemPresetInstallInput } from "../../../lib/api";
import { architectReplayBudget, budgetCapped, type ReplayBudget } from "../../../lib/api/assistant-budget";
import { useModelCatalog } from "../../../lib/model-catalog";
import {
  CUSTOM_MODEL_OPTION,
  isCustomModelId,
  type ModelSource,
  modelOptionsFor,
  supportsReasoningEffort,
} from "../../../lib/models";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Dialog, Notice, Panel, Stat } from "../../ui";
import { Select } from "../../select";

/** The system preset behind the architect assistant (`backend/app/system_agents/presets.py`). */
export const ARCHITECT_KEY = "aws-agent-solution-architect";
const SOURCES: ModelSource[] = ["bedrock", "mantle"];

function SwitchDialog({ preset, workspaceId, onClose, onSwitched }: {
  preset: SystemPresetInfo;
  workspaceId: string;
  onClose: () => void;
  onSwitched: () => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  useModelCatalog(); // the account's catalog joins the Bedrock options when it lands
  const [source, setSource] = useState<ModelSource>((preset.model_source as ModelSource) || "bedrock");
  const [modelId, setModelId] = useState(preset.model_id ?? "");
  const [custom, setCustom] = useState(() => isCustomModelId(preset.model_id ?? "", source));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const options = modelOptionsFor(source);
  const next = modelId.trim();
  const unchanged = next === (preset.model_id ?? "") && source === (preset.model_source || "bedrock");

  const pickSource = (value: ModelSource) => {
    setSource(value);
    setModelId(modelOptionsFor(value)[0]?.model_id ?? "");
    setCustom(false);
  };

  const submit = async () => {
    if (!next || unchanged) return;
    setBusy(true);
    setError(null);
    const body: SystemPresetInstallInput = { model_id: next, model_source: source };
    // a reasoning effort is refused for any model that cannot take one (422): a
    // switch away from such a model drops the stored effort with it
    if (!supportsReasoningEffort(next, source)) body.clear = ["reasoning_effort"];
    try {
      await api.installSystemPreset(ARCHITECT_KEY, body, workspaceId);
      toast("ok", t("v3.assistant.model.switched", { model: next }));
      onSwitched();
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={t("v3.assistant.model.dialogTitle")}
      onClose={() => !busy && onClose()}
      foot={
        <>
          <Btn kind="ghost" disabled={busy} onClick={onClose}>{t("v3.common.cancel")}</Btn>
          <Btn kind="primary" busy={busy} disabled={!next || unchanged}
            disabledReason={!next ? t("story.reason.typeModelId") : t("story.reason.noChanges")} onClick={() => void submit()}>
            {busy ? t("v3.assistant.model.switching") : t("v3.assistant.model.submit")}
          </Btn>
        </>
      }
    >
      <div style={{ display: "grid", gap: 12 }} data-testid="v3-architect-model-dialog">
        <p style={{ margin: 0 }}>{t("v3.assistant.model.dialogBody")}</p>
        <label className="v3-field">
          <span>{t("v3.assistant.model.source")}</span>
          <Select className="v3-select" value={source} onChange={(e) => pickSource(e.target.value as ModelSource)}>
            {SOURCES.map((value) => (
              <option key={value} value={value}>{t(`v2.agents.wizard.source.${value}`)}</option>
            ))}
          </Select>
        </label>
        <label className="v3-field">
          <span>{t("v3.assistant.model.model")}</span>
          <Select
            className="v3-select"
            value={custom ? CUSTOM_MODEL_OPTION : modelId}
            onChange={(e) => {
              if (e.target.value === CUSTOM_MODEL_OPTION) {
                setCustom(true);
                return;
              }
              setCustom(false);
              setModelId(e.target.value);
            }}
            data-testid="v3-architect-model-select"
          >
            {options.map((option) => (
              <option key={option.model_id} value={option.model_id}>{option.label}</option>
            ))}
            <option value={CUSTOM_MODEL_OPTION}>{t("v3.assistant.model.custom")}</option>
          </Select>
        </label>
        {custom && (
          <label className="v3-field">
            <span>{t("v3.assistant.model.customId")}</span>
            <input className="v3-input mono" value={modelId} onChange={(e) => setModelId(e.target.value)} placeholder={t("v3.assistant.model.customId")} />
          </label>
        )}
        <small className="v3-hint">{t("v3.assistant.model.redeployNote")}</small>
        {error && <Notice s="act">{error}</Notice>}
      </div>
    </Dialog>
  );
}

/** The replay budget of the preset's model (assistant-context C3), beside the model. */
function BudgetStat({ budget }: { budget: ReplayBudget | null | undefined }) {
  const { t, i18n } = useTranslation();
  if (!budget) return <Stat label={t("v3.assistant.budget.label")} value="—" />;
  const fmt = (n: number) => n.toLocaleString(i18n.language);
  const value = t(budget.unit === "tokens" ? "v3.assistant.budget.tokens" : "v3.assistant.budget.chars", {
    value: fmt(budget.limit),
  });
  const foot = [
    t(`v3.assistant.budget.${budget.source}`, {
      window: budget.context_window_tokens != null ? fmt(budget.context_window_tokens) : "—",
      turns: budget.max_turns,
    }),
    budgetCapped(budget) ? t("v3.assistant.budget.capped", { limit: fmt(budget.limit) }) : "",
  ].filter(Boolean).join(" ");
  return (
    <Stat
      label={t("v3.assistant.budget.label")}
      value={<span style={{ fontSize: 15 }} data-testid="v3-architect-budget">{value}</span>}
      foot={foot}
    />
  );
}

/**
 * Administrators switch the architect assistant's model here instead of on the
 * classic system-presets page: a partial install edit (`model_id` + `model_source`),
 * which re-publishes the preset's Harness as a new version. Members see nothing.
 */
export function ArchitectModelPanel({ workspaceId }: { workspaceId: string }) {
  const { t } = useTranslation();
  const presets = useLoad(() => api.listSystemPresets(workspaceId), `v3-architect-presets:${workspaceId}`);
  const [open, setOpen] = useState(false);
  const preset = presets.data?.presets.find((p) => p.key === ARCHITECT_KEY) ?? null;
  // keyed on the stored model: a switch re-reads the budget of the new one
  const budget = useLoad(architectReplayBudget, `v3-architect-budget:${workspaceId}:${preset?.model_id ?? ""}`);
  if (!preset) return presets.error ? <Notice s="wait">{presets.error}</Notice> : null;
  const deploying = preset.status === "deploying" || preset.deployment_status === "running";
  return (
    <Panel
      title={t("v3.assistant.model.title")}
      signal={deploying ? "wait" : preset.status === "failed" ? "act" : undefined}
      end={
        <span style={{ display: "inline-flex", gap: 8 }}>
          <button type="button" className="v3-btn ghost sm" onClick={() => { presets.reload(); budget.reload(); }} disabled={presets.loading}>
            <RefreshCw size={13} /> {t("v3.ux.refresh")}
          </button>
          <Btn size="sm" disabled={!preset.can_configure} disabledReason={t("v3.assistant.model.cannot")} onClick={() => setOpen(true)}>
            <Cpu size={13} /> {t("v3.assistant.model.switch")}
          </Btn>
        </span>
      }
    >
      <div className="v3-grid c4" data-testid="v3-architect-model">
        <Stat label={t("v3.assistant.model.current")} value={<span className="mono" style={{ fontSize: 14, wordBreak: "break-all" }}>{preset.model_id ?? "—"}</span>} />
        <Stat label={t("v3.assistant.model.source")} value={<span style={{ fontSize: 15 }}>{preset.model_source ? t(`v2.agents.wizard.source.${preset.model_source}`) : "—"}</span>} />
        <Stat
          label={t("v3.assistant.model.status")}
          value={<Chip s={deploying ? "wait" : preset.status === "active" ? "ok" : preset.status === "failed" ? "act" : undefined}>{t(`create.system.status.${preset.status}`)}</Chip>}
          foot={preset.status === "failed" ? preset.error ?? undefined : undefined}
        />
        <BudgetStat budget={budget.data} />
      </div>
      {open && (
        <SwitchDialog preset={preset} workspaceId={workspaceId} onClose={() => setOpen(false)} onSwitched={presets.reload} />
      )}
    </Panel>
  );
}
