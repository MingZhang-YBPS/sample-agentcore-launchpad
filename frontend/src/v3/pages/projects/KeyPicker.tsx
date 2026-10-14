import { useTranslation } from "react-i18next";

import { Notice } from "../../ui";
import { keySlug, sodPairsIn } from "./model";

/**
 * The permission keys of a project, in two groups (project-level and agent-level,
 * as `GET /api/role-templates` lists them), each with its label and raw key — and
 * the combined-duties hint when the selection holds both sides of an SoD pair.
 */
export function KeyPicker({
  projectKeys,
  agentKeys,
  value,
  onChange,
  disabled,
  strict,
}: {
  projectKeys: readonly string[];
  agentKeys: readonly string[];
  value: readonly string[];
  onChange: (next: string[]) => void;
  disabled?: boolean;
  /** the project refuses combined duties (`sod_strict`): the hint says so */
  strict?: boolean;
}) {
  const { t } = useTranslation();
  const picked = new Set(value);
  const toggle = (key: string) => {
    const next = new Set(picked);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    onChange([...next].sort());
  };
  const pairs = sodPairsIn(value);
  const group = (legend: string, keys: readonly string[]) => (
    <fieldset disabled={disabled}>
      <legend>{legend}</legend>
      <div className="grid">
        {keys.map((key) => (
          <label key={key}>
            <input type="checkbox" checked={picked.has(key)} onChange={() => toggle(key)} />
            <span>{t(`v3.projects.key.${keySlug(key)}`, key)} <small>{key}</small></span>
          </label>
        ))}
      </div>
    </fieldset>
  );
  return (
    <div className="v3-prj-keys" data-testid="key-picker">
      {group(t("v3.projects.keyGroup.project"), projectKeys)}
      {group(t("v3.projects.keyGroup.agent"), agentKeys)}
      {pairs.length > 0 && (
        <Notice s={strict ? "act" : "wait"}>
          {t(strict ? "v3.projects.sod.strictHint" : "v3.projects.sod.hint", {
            pairs: pairs.map((p) => p.join(" + ")).join("; "),
          })}
        </Notice>
      )}
    </div>
  );
}
