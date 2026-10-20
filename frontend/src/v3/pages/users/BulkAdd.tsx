import { useState } from "react";
import { useTranslation } from "react-i18next";

import { errorMessage } from "../../../lib/api";
import { usersApi } from "../../../lib/api/users";
import { useToast } from "../../hooks";
import { Reasoned } from "../../reason";
import { Btn, Dialog, Notice } from "../../ui";
import { suggestedRole } from "./forms";
import { type Structure, useProjectRoleOptions } from "./structure";
import { Select } from "../../select";

/**
 * "Add to project…" of the user list (projects §7.6): the selected accounts get a
 * direct membership with one role (`POST /api/users/bulk/projects`); an existing
 * member's role is replaced, nobody is removed, administrators are skipped.
 */
export function BulkAddDialog({
  users,
  structure,
  onClose,
  onDone,
}: {
  users: { id: string; username: string }[];
  structure: Structure;
  onClose: () => void;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const [projectId, setProjectId] = useState(structure.projects.find((p) => !p.is_default)?.id ?? structure.projects[0]?.id ?? "");
  const roles = useProjectRoleOptions([projectId], structure.templates);
  const [role, setRole] = useState("");
  const options = projectId ? roles(projectId) : [];
  const chosenRole = options.some((o) => o.key === role) ? role : suggestedRole(options);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (!projectId || !chosenRole) return;
    setBusy(true);
    setError(null);
    try {
      const result = await usersApi.bulkAddToProject({ user_ids: users.map((u) => u.id), project_id: projectId, role_key: chosenRole });
      toast("ok", t("v3.users.bulk.done", {
        added: result.added.length,
        updated: result.updated.length,
        unchanged: result.unchanged.length,
        skipped: result.skipped.length,
      }));
      onDone();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={t("v3.users.bulk.title", { count: users.length })}
      onClose={() => !busy && onClose()}
      foot={
        <>
          <Btn kind="ghost" disabled={busy} onClick={onClose}>{t("v3.common.cancel")}</Btn>
          <Btn kind="primary" busy={busy} disabled={!projectId || !chosenRole}
            disabledReason={!projectId ? t("story.reason.pickProject") : t("story.reason.pickRole")} onClick={() => void submit()}>{t("v3.users.bulk.submit")}</Btn>
        </>
      }
    >
      <div style={{ display: "grid", gap: 12 }} data-testid="v3-user-bulk">
        <p style={{ margin: 0 }}>{t("v3.users.bulk.body")}</p>
        <small className="v3-hint">{users.map((u) => u.username).join(", ")}</small>
        <label className="v3-field">
          <span>{t("v3.users.access.project")}</span>
          <Select className="v3-select" value={projectId} onChange={(e) => setProjectId(e.target.value)}>
            {structure.projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </Select>
        </label>
        <label className="v3-field">
          <span>{t("v3.users.access.role")}</span>
          <Reasoned disabledReason={t("story.reason.pickProject")}>
            <Select className="v3-select" value={chosenRole} onChange={(e) => setRole(e.target.value)} disabled={!projectId}>
              {options.map((o) => <option key={o.key} value={o.key}>{o.label}</option>)}
            </Select>
          </Reasoned>
        </label>
        <small className="v3-hint">{t("v3.users.bulk.hint")}</small>
        {error && <Notice s="act">{error}</Notice>}
      </div>
    </Dialog>
  );
}
