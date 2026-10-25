import { Plus, X } from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { Reasoned } from "../../reason";
import { Btn } from "../../ui";
import { type MembershipRow, suggestedRole } from "./forms";
import type { RoleOption } from "./structure";
import { Select } from "../../select";

/** The legacy workspace grants: folded under "advanced" once the platform has projects. */
export function LegacyGrants({ multiProject, children }: { multiProject: boolean; children: ReactNode }) {
  const { t } = useTranslation();
  if (!multiProject) return <>{children}</>;
  return (
    <details className="v3-field">
      <summary style={{ cursor: "pointer" }}>{t("v3.users.memberships.legacy")}</summary>
      <div style={{ display: "grid", gap: 8, marginTop: 8 }}>
        {children}
        <small className="v3-hint">{t("v3.users.memberships.legacyHint")}</small>
      </div>
    </details>
  );
}

/**
 * Rows of "target + role" with add / remove — the project block of the create,
 * approve and edit dialogs (projects §7.4, §7.5).
 */
export function MembershipRows({
  kind,
  rows,
  onChange,
  targets,
  rolesFor,
  error,
  busy,
}: {
  kind: "projects";
  rows: MembershipRow[];
  onChange: (rows: MembershipRow[]) => void;
  targets: { id: string; name: string }[];
  rolesFor: (id: string) => RoleOption[];
  error?: string | null;
  /** a save is running: every row locks */
  busy?: boolean;
}) {
  const { t } = useTranslation();
  const set = (index: number, next: Partial<MembershipRow>) =>
    onChange(rows.map((row, i) => (i === index ? { ...row, ...next } : row)));
  const taken = new Set(rows.map((row) => row.id));
  const firstFree = targets.find((target) => !taken.has(target.id));
  return (
    <div className="v3-field" data-testid={`v3-user-${kind}`}>
      <span>{t(`v3.users.memberships.${kind}`)}</span>
      {rows.length === 0 && <small className="v3-hint">{t(`v3.users.memberships.${kind}None`)}</small>}
      {rows.map((row, index) => {
        const roles = row.id ? rolesFor(row.id) : [];
        return (
          <div key={index} style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
            <Select className="v3-select" style={{ flex: "2 1 180px" }} value={row.id} disabled={busy}
              aria-label={t(`v3.users.memberships.${kind}Target`)}
              onChange={(e) => {
                const options = rolesFor(e.target.value);
                set(index, { id: e.target.value, role: options.some((o) => o.key === row.role) ? row.role : suggestedRole(options) });
              }}>
              <option value="">—</option>
              {targets.map((target) => (
                <option key={target.id} value={target.id} disabled={target.id !== row.id && taken.has(target.id)}>{target.name}</option>
              ))}
            </Select>
            <Reasoned disabledReason={busy ? null : t("story.reason.pickProject")}>
              <Select className="v3-select" style={{ flex: "1 1 140px" }} value={row.role} disabled={busy || !row.id}
                aria-label={t("v3.users.memberships.role")} onChange={(e) => set(index, { role: e.target.value })}>
                <option value="">—</option>
                {roles.map((role) => <option key={role.key} value={role.key}>{role.label}</option>)}
                {row.role && !roles.some((role) => role.key === row.role) && <option value={row.role}>{row.role}</option>}
              </Select>
            </Reasoned>
            <Btn size="sm" kind="ghost" busy={busy} title={t("v3.users.memberships.remove")} aria-label={t("v3.users.memberships.remove")}
              onClick={() => onChange(rows.filter((_, i) => i !== index))}>
              <X size={13} />
            </Btn>
          </div>
        );
      })}
      <div>
        <Btn size="sm" kind="ghost" busy={busy} disabled={!firstFree} disabledReason={t("story.reason.allListed")}
          onClick={() => {
            const id = firstFree?.id ?? "";
            onChange([...rows, { id, role: suggestedRole(rolesFor(id)) }]);
          }}>
          <Plus size={13} /> {t(`v3.users.memberships.${kind}Add`)}
        </Btn>
      </div>
      {error && <small className="v3-err">{t(error)}</small>}
    </div>
  );
}
