import { ArrowRight, Plus, X } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { ErrorCallout } from "../../../components/ErrorCallout";
import { ssoApi, type SsoMapping, type SsoMappingKind, SSO_ROLES } from "../../../lib/api/sso";
import { useFailure } from "../../failure";
import { useToast } from "../../hooks";
import { Btn, Panel } from "../../ui";
import { blankMapping, mappingProblems } from "./model";
import { Select } from "../../select";

export interface MappingTargets {
  projects: { id: string; name: string }[];
  roleTemplates: string[];
}

const KINDS: readonly SsoMappingKind[] = ["role", "project"];

/** One rule: `IdP group  →  global role | project + role`. */
export function MappingRow({ row, index, targets, onChange, onRemove }: {
  row: SsoMapping;
  index: number;
  targets: MappingTargets;
  onChange: (next: SsoMapping) => void;
  onRemove: () => void;
}) {
  const { t } = useTranslation();
  const problems = mappingProblems(row);
  const roles = row.kind === "role" ? [...SSO_ROLES] : targets.roleTemplates;
  const options = row.kind === "project" ? targets.projects : [];
  return (
    <li className="v3-sso-map" data-kind={row.kind} data-testid={`sso-mapping-${index}`}>
      <span className="idx mono" aria-hidden="true">{String(index + 1).padStart(2, "0")}</span>
      <input
        className="v3-input mono"
        value={row.group}
        placeholder={t("v3.sso.mapping.groupPlaceholder")}
        aria-label={t("v3.sso.mapping.group")}
        aria-invalid={problems.includes("group")}
        onChange={(e) => onChange({ ...row, group: e.target.value })}
      />
      <ArrowRight className="arrow" size={14} aria-hidden="true" />
      <div className="v3-sso-kind" role="group" aria-label={t("v3.sso.mapping.kind")}>
        {KINDS.map((kind) => (
          <button
            key={kind}
            type="button"
            aria-pressed={row.kind === kind}
            className={row.kind === kind ? "on" : ""}
            onClick={() => kind !== row.kind && onChange({ ...blankMapping(kind), group: row.group })}
          >
            {t(`v3.sso.mapping.kinds.${kind}`)}
          </button>
        ))}
      </div>
      {row.kind !== "role" ? (
        <Select
          className="v3-select"
          value={row.target_id}
          aria-label={t(`v3.sso.mapping.kinds.${row.kind}`)}
          aria-invalid={problems.includes("target")}
          onChange={(e) => onChange({ ...row, target_id: e.target.value })}
        >
          <option value="">{t("v3.sso.mapping.pick")}</option>
          {options.map((option) => <option key={option.id} value={option.id}>{option.name}</option>)}
          {row.target_id && !options.some((o) => o.id === row.target_id) && (
            <option value={row.target_id}>{row.target_id}</option>
          )}
        </Select>
      ) : <span className="v3-sso-map-gap" aria-hidden="true" />}
      <Select
        className="v3-select"
        value={row.role_key}
        aria-label={t("v3.sso.mapping.role")}
        onChange={(e) => onChange({ ...row, role_key: e.target.value })}
      >
        {roles.map((role) => (
          <option key={role} value={role}>
            {row.kind === "role" ? t(`v3.sso.role.${role}`) : t(`v3.sso.mapping.template.${role}`, { defaultValue: role })}
          </option>
        ))}
        {!roles.includes(row.role_key) && <option value={row.role_key}>{row.role_key}</option>}
      </Select>
      <button type="button" className="v3-btn ghost sm" onClick={onRemove} aria-label={t("v3.sso.mapping.remove")} title={t("v3.sso.mapping.remove")}>
        <X size={13} aria-hidden="true" />
      </button>
    </li>
  );
}

/** The provider's group mappings, saved as one list (`PUT …/mappings`). */
/** Any provider row (OIDC or SAML): its id, mappings, groups claim and default role. */
export function MappingsEditor<P extends { id: string; mappings: SsoMapping[]; claims: { groups: string }; default_role: string }>({ provider, targets, onSaved }: {
  provider: P;
  targets: MappingTargets;
  onSaved: (next: P) => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const [rows, setRows] = useState<SsoMapping[]>(provider.mappings);
  const [busy, setBusy] = useState(false);
  useEffect(() => setRows(provider.mappings), [provider.mappings]);
  const dirty = JSON.stringify(rows) !== JSON.stringify(provider.mappings);
  const invalid = rows.some((row) => mappingProblems(row).length > 0);
  const save = async () => {
    setBusy(true);
    try {
      failed.clear();
      const answer = await ssoApi.putMappings(provider.id, rows);
      toast("ok", t("v3.sso.mapping.saved"));
      onSaved({ ...provider, mappings: answer.mappings });
    } catch (err) {
      failed.fail(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Panel
      title={t("v3.sso.section.mappings")}
      end={(
        <span style={{ display: "inline-flex", gap: 8 }}>
          {dirty && <Btn size="sm" kind="ghost" onClick={() => setRows(provider.mappings)} disabled={busy}>{t("v3.sso.discard")}</Btn>}
          <Btn size="sm" kind="primary" onClick={() => void save()} busy={busy} disabled={!dirty || invalid}
            disabledReason={invalid ? t("story.reason.fixErrors") : t("story.reason.noChanges")}>{t("v3.sso.mapping.save")}</Btn>
        </span>
      )}
    >
      <div style={{ display: "grid", gap: 12 }}>
        <p className="v3-sso-muted">
          {t("v3.sso.mapping.body", { claim: provider.claims.groups || "groups", role: t(`v3.sso.role.${provider.default_role}`) })}
        </p>
        <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-sso-mappings-error" />
        {rows.length > 0 && (
          <ol className="v3-sso-maps">
            {rows.map((row, index) => (
              <MappingRow
                key={index}
                row={row}
                index={index}
                targets={targets}
                onChange={(next) => setRows((list) => list.map((item, i) => (i === index ? next : item)))}
                onRemove={() => setRows((list) => list.filter((_, i) => i !== index))}
              />
            ))}
          </ol>
        )}
        <div>
          <Btn size="sm" onClick={() => setRows((list) => [...list, blankMapping()])}>
            <Plus size={13} aria-hidden="true" /> {t("v3.sso.mapping.add")}
          </Btn>
        </div>
        <small className="v3-hint">{t("v3.sso.mapping.rules")}</small>
      </div>
    </Panel>
  );
}
