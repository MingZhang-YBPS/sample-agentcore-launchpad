import { useTranslation } from "react-i18next";
import { useModelCatalog } from "../../lib/model-catalog";
import {
  BACKEND_LABELS,
  isOpenAiJudge,
  skillLabJudgeSuggestions,
  skillLabTargetSuggestions,
} from "../../lib/skillLab";
import type { SkillLabJudgeMode, SkillLabStatus, SkillLabTargetBackend } from "../../lib/api";

function defaultFor(status: SkillLabStatus | null, backend: SkillLabTargetBackend): string {
  if (status === null) return "";
  return backend === "codex_exec" ? status.default_codex_target_model : status.default_target_model;
}

/**
 * Shared exec-backend + model fields for the eval/train (and taskgen) wizards.
 *
 * Switching backends swaps the target-model prefill only while the field still
 * holds the other backend's default (upstream `applyBackend` semantics) — a
 * hand-typed model is never overwritten.
 */
export function BackendModelFields({
  status,
  idPrefix,
  backend,
  setBackend,
  targetModel,
  setTargetModel,
  judgeModel,
  setJudgeModel,
  judgeMode,
  setJudgeMode,
}: {
  status: SkillLabStatus | null;
  /** data-testid prefix, e.g. "eval" → eval-param-targetModel */
  idPrefix: string;
  backend: SkillLabTargetBackend;
  setBackend: (b: SkillLabTargetBackend) => void;
  targetModel: string;
  setTargetModel: (m: string) => void;
  judgeModel: string;
  setJudgeModel: (m: string) => void;
  judgeMode: SkillLabJudgeMode;
  setJudgeMode: (m: SkillLabJudgeMode) => void;
}) {
  const { t } = useTranslation();
  useModelCatalog(); // the account catalog joins the suggestions once it lands
  const backends = status?.target_backends?.length
    ? status.target_backends
    : (Object.keys(BACKEND_LABELS) as SkillLabTargetBackend[]);
  const judgeModes: SkillLabJudgeMode[] = status?.judge_modes?.length
    ? status.judge_modes
    : ["auto", "chat", "agentic"];
  // Two independent host prerequisites, and BOTH have to hold: the sandbox
  // launcher (shared) and the CLI this judge model's route resolves to. Probing
  // only the codex side is what let prod look ready with no claude CLI at all.
  // auto/agentic still run when one is missing, but artifact tasks fail closed —
  // warn, don't hide.
  const judgeRoutesToCodex = isOpenAiJudge(judgeModel);
  const codexJudgeMissing = judgeRoutesToCodex && status?.judge_codex_ready === false;
  const claudeJudgeMissing = !judgeRoutesToCodex && status?.judge_claude_ready === false;
  const agenticReady =
    status?.agentic_judge_ready !== false && !codexJudgeMissing && !claudeJudgeMissing;

  const applyBackend = (next: SkillLabTargetBackend) => {
    if (next === backend) return;
    const previousDefault = defaultFor(status, backend);
    setBackend(next);
    if (!targetModel.trim() || targetModel === previousDefault) {
      setTargetModel(defaultFor(status, next));
    }
  };

  return (
    <>
      <div className="field">
        <label>{t("skillLab.backend.field")}</label>
        <select
          className="input"
          value={backend}
          data-testid={`${idPrefix}-param-targetBackend`}
          onChange={(e) => applyBackend(e.target.value as SkillLabTargetBackend)}
        >
          {backends.map((option) => (
            <option key={option} value={option} style={{ background: "var(--panel)" }}>
              {BACKEND_LABELS[option] ?? option}
            </option>
          ))}
        </select>
        <span className="mono dim" style={{ fontSize: 10.5 }}>
          {t("skillLab.backend.hint")}
        </span>
      </div>

      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        <div className="field" style={{ flex: 1, minWidth: 240 }}>
          <label>{t("skillLab.eval.wizard.field.targetModel")}</label>
          <input
            className="input mono"
            value={targetModel}
            list={`${idPrefix}-target-models`}
            data-testid={`${idPrefix}-param-targetModel`}
            onChange={(e) => setTargetModel(e.target.value)}
          />
          <datalist id={`${idPrefix}-target-models`}>
            {skillLabTargetSuggestions(status, backend).map((model) => (
              <option key={model} value={model} />
            ))}
          </datalist>
          <span className="mono dim" style={{ fontSize: 10.5 }}>
            {t(
              backend === "codex_exec"
                ? "skillLab.backend.targetHintCodex"
                : "skillLab.backend.targetHintClaude",
            )}
          </span>
        </div>
        <div className="field" style={{ flex: 1, minWidth: 240 }}>
          <label>{t("skillLab.eval.wizard.field.judgeModel")}</label>
          <input
            className="input mono"
            value={judgeModel}
            list="skill-lab-judge-models"
            data-testid={`${idPrefix}-param-judgeModel`}
            onChange={(e) => setJudgeModel(e.target.value)}
          />
          <datalist id="skill-lab-judge-models">
            {skillLabJudgeSuggestions(status).map((model) => (
              <option key={model} value={model} />
            ))}
          </datalist>
          <span className="mono dim" style={{ fontSize: 10.5 }}>
            {t("skillLab.backend.judgeHint")}
          </span>
        </div>
        <div className="field" style={{ minWidth: 170 }}>
          <label>{t("skillLab.backend.judgeMode")}</label>
          <select
            className="input"
            value={judgeMode}
            data-testid={`${idPrefix}-param-judgeMode`}
            onChange={(e) => setJudgeMode(e.target.value as SkillLabJudgeMode)}
          >
            {judgeModes.map((option) => (
              <option key={option} value={option} style={{ background: "var(--panel)" }}>
                {t(`skillLab.backend.judgeModeOption.${option}`)}
                {option !== "chat" && !agenticReady ? " ⚠" : ""}
              </option>
            ))}
          </select>
          <span className="mono dim" style={{ fontSize: 10.5 }}>
            {judgeMode !== "chat" && !agenticReady
              ? t(
                  codexJudgeMissing
                    ? "skillLab.backend.judgeCodexUnready"
                    : claudeJudgeMissing
                      ? "skillLab.backend.judgeClaudeUnready"
                      : "skillLab.backend.judgeModeUnready",
                )
              : t(`skillLab.backend.judgeModeHint.${judgeMode}`)}
          </span>
        </div>
      </div>
    </>
  );
}
