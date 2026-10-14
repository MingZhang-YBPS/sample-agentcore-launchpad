import { CheckCircle2, RotateCcw, ShieldAlert, XCircle } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiErrorDetail } from "../../components/ApiErrorDetail";
import { useAuth } from "../../auth/auth-context";
import { api, errorMessage, type ModelProbeResult, type ModelSettingRole } from "../../lib/api";
import { activeCatalogIds, useModelCatalog } from "../../lib/model-catalog";
import { reloadModelDefaults } from "../../lib/model-defaults";
import { isSavable, roleModelOptions, sourceSignal } from "../../lib/model-settings";
import { CUSTOM_MODEL_OPTION } from "../../lib/models";
import { ago } from "../format";
import { useLoad, useToast } from "../hooks";
import { Btn, Chip, Notice, PageHead, Panel, Skeleton } from "../ui";

const NONE = "";

type Probe = { result: ModelProbeResult | null; error: string | null; busy: boolean };

function ProbeResult({ probe }: { probe: Probe }) {
  const { t } = useTranslation();
  if (probe.busy) return <small className="muted">{t("v3.models.probing")}</small>;
  if (probe.error) return <small style={{ color: "var(--v3-act, var(--crit-text))" }}>{probe.error}</small>;
  if (!probe.result) return null;
  const { result } = probe;
  return (
    <div style={{ display: "grid", gap: 4 }} data-testid="v3-models-probe-result">
      {result.checks.length === 0 && <small className="muted">{t("v3.models.probeNone")}</small>}
      {result.checks.map((check) => (
        <small key={check.name} style={{ display: "flex", gap: 6, alignItems: "flex-start" }}>
          {check.ok ? <CheckCircle2 size={13} aria-hidden="true" /> : <XCircle size={13} aria-hidden="true" />}
          <span>
            <b>{t(`v3.models.check.${check.name}`)}</b>
            {" · "}
            {check.ok ? t("v3.models.checkOk", { ms: check.latency_ms }) : check.error}
          </span>
        </small>
      ))}
      <small className="muted">{t("v3.models.probeRegion", { region: result.region })}</small>
    </div>
  );
}

function RoleRow({ role, onSaved }: { role: ModelSettingRole; onSaved: (row: ModelSettingRole) => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [draft, setDraft] = useState(role.value);
  const [custom, setCustom] = useState(false);
  const [saving, setSaving] = useState(false);
  const [probe, setProbe] = useState<Probe>({ result: null, error: null, busy: false });
  const options = roleModelOptions(role, activeCatalogIds());

  const run = async (work: () => Promise<ModelSettingRole>, okKey: string) => {
    setSaving(true);
    try {
      const row = await work();
      onSaved(row);
      setDraft(row.value);
      setCustom(false);
      void reloadModelDefaults();
      toast("ok", t(okKey, { role: t(`v3.models.role.${role.field}.name`, { defaultValue: role.field }) }));
    } catch (err) {
      toast("act", errorMessage(err), err);
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    setProbe({ result: null, error: null, busy: true });
    try {
      setProbe({ result: await api.probeModel(role.field, draft.trim()), error: null, busy: false });
    } catch (err) {
      setProbe({ result: null, error: errorMessage(err), busy: false });
    }
  };

  return (
    <tr data-testid={`v3-models-row-${role.field}`}>
      <td style={{ minWidth: 220 }}>
        <div className="v3-name">
          <div>
            <b>{t(`v3.models.role.${role.field}.name`, { defaultValue: role.field })}</b>
            <small>{t(`v3.models.role.${role.field}.desc`, { defaultValue: "" })}</small>
            <small className="mono">{role.env}</small>
          </div>
        </div>
      </td>
      <td style={{ minWidth: 220 }}>
        <div style={{ display: "grid", gap: 4 }}>
          <span className="mono">{role.value || t("v3.models.none")}</span>
          <span>
            <Chip s={sourceSignal(role.source)}>{t(`v3.models.source.${role.source}`)}</Chip>
          </span>
          {role.source === "console" && role.updated_at && (
            <small className="muted">{t("v3.models.updated", { who: role.updated_by ?? "—", when: ago(role.updated_at) })}</small>
          )}
          {role.families && <small className="muted">{t("v3.models.familyOnly", { families: role.families.join(" / ") })}</small>}
        </div>
      </td>
      <td style={{ minWidth: 280 }}>
        <div style={{ display: "grid", gap: 6 }}>
          <select
            className="v3-select"
            aria-label={t("v3.models.pick")}
            data-testid={`v3-models-select-${role.field}`}
            value={custom ? CUSTOM_MODEL_OPTION : draft}
            onChange={(e) => {
              setProbe({ result: null, error: null, busy: false });
              setCustom(e.target.value === CUSTOM_MODEL_OPTION);
              if (e.target.value !== CUSTOM_MODEL_OPTION) setDraft(e.target.value);
            }}
          >
            {role.allow_empty && <option value={NONE}>{t("v3.models.none")}</option>}
            {options.map((id) => (
              <option key={id} value={id}>
                {id}
                {id === role.default ? ` · ${t("v3.models.buildDefault")}` : ""}
              </option>
            ))}
            {draft && !options.includes(draft) && !custom && <option value={draft}>{draft}</option>}
            <option value={CUSTOM_MODEL_OPTION}>{t("models.picker.custom")}</option>
          </select>
          {custom && (
            <input
              className="v3-input mono"
              aria-label={t("models.picker.custom")}
              value={draft}
              placeholder={t("models.picker.customPlaceholder", { example: role.default || role.value })}
              onChange={(e) => setDraft(e.target.value.trim())}
            />
          )}
          <ProbeResult probe={probe} />
        </div>
      </td>
      <td style={{ width: 1, whiteSpace: "nowrap" }}>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
          <Btn size="sm" disabled={probe.busy || !draft.trim()} onClick={() => void test()}>
            {t("v3.models.test")}
          </Btn>
          <Btn
            size="sm"
            kind="primary"
            disabled={saving || !isSavable(role, draft)}
            onClick={() => void run(() => api.updateModelSetting(role.field, draft.trim()), "v3.models.saved")}
          >
            {t("v3.models.save")}
          </Btn>
          {role.source === "console" && (
            <Btn
              size="sm"
              kind="ghost"
              disabled={saving}
              title={t("v3.models.resetHint")}
              onClick={() => void run(() => api.resetModelSetting(role.field), "v3.models.resetDone")}
            >
              <RotateCcw size={13} aria-hidden="true" /> {t("v3.models.reset")}
            </Btn>
          )}
        </div>
      </td>
    </tr>
  );
}

function Manager() {
  const { t } = useTranslation();
  const settings = useLoad(() => api.modelSettings(), "v3-model-settings");
  const catalog = useModelCatalog();
  // rows replaced by a save / reset, over the loaded list
  const [saved, setSaved] = useState<Record<string, ModelSettingRole>>({});
  const roles = (settings.data?.roles ?? []).map((row) => saved[row.field] ?? row);

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead eyebrow={t("v2.nav.groupAdmin")} title={t("v3.models.title")} sub={t("v3.models.sub")} />
      <Notice s="wait">
        <div style={{ display: "grid", gap: 4 }}>
          <span>{t("v3.models.noteOverride")}</span>
          <span>{t("v3.models.noteJudge")}</span>
          <span>{t("v3.models.noteCatalog", { count: catalog.length })}</span>
        </div>
      </Notice>
      {settings.error && <Notice s="act"><ApiErrorDetail error={settings.failure ?? settings.error} /></Notice>}
      {settings.loading && !settings.data ? (
        <Skeleton rows={8} />
      ) : (
        <Panel title={t("v3.models.roles")} flush>
          <table className="v3-table" data-testid="v3-models-table">
            <thead>
              <tr>
                <th>{t("v3.models.col.role")}</th>
                <th>{t("v3.models.col.current")}</th>
                <th>{t("v3.models.col.model")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {roles.map((role) => (
                <RoleRow
                  // a save/reset remounts the row on its new value
                  key={`${role.field}:${role.value}:${role.source}`}
                  role={role}
                  onSaved={(row) => setSaved((prev) => ({ ...prev, [row.field]: row }))}
                />
              ))}
            </tbody>
          </table>
        </Panel>
      )}
    </div>
  );
}

/**
 * Models (admin-only): every model role the platform calls or pre-selects, where its
 * value comes from, a live probe in the active workspace's account/Region, and a
 * console override (applied at once, no restart).
 */
export function V3Models() {
  const { isAdmin } = useAuth();
  const { t } = useTranslation();
  // mount no loaders for a member, including direct deep links
  if (!isAdmin) {
    return (
      <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
        <PageHead eyebrow={t("v2.nav.groupAdmin")} title={t("v3.models.title")} sub={t("auth.adminRequired.meta")} />
        <Panel>
          <div className="v3-empty" data-testid="v3-models-forbidden">
            <ShieldAlert size={28} aria-hidden="true" />
            <strong>{t("auth.adminRequired.title")}</strong>
            {t("auth.adminRequired.body")}
          </div>
        </Panel>
      </div>
    );
  }
  return <Manager />;
}
