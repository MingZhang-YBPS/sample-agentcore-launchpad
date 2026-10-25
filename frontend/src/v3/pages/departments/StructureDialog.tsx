import { useState } from "react";
import { useTranslation } from "react-i18next";

import { errorMessage } from "../../../lib/api";
import { Reasoned } from "../../reason";
import { Btn, Dialog, Notice } from "../../ui";
import { slugify, type StructureForm, validateStructure } from "./model";

/**
 * Create / edit a department: id (create only), name and description.
 * `onSubmit` makes the call; a refusal (a taken id, …) keeps the dialog open with
 * what was typed.
 */
export function StructureDialog({
  initial,
  creating,
  onClose,
  onSubmit,
}: {
  initial: StructureForm;
  creating: boolean;
  onClose: () => void;
  onSubmit: (form: StructureForm) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [form, setForm] = useState<StructureForm>(initial);
  // the id follows the name until the admin edits it
  const [idTouched, setIdTouched] = useState(!creating);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const errors = touched ? validateStructure(form, creating) : {};

  const submit = async () => {
    setTouched(true);
    if (Object.keys(validateStructure(form, creating)).length > 0) return;
    setBusy(true);
    setError(null);
    try {
      await onSubmit({ ...form, id: form.id.trim(), name: form.name.trim(), description: form.description.trim() });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      title={t(`v3.departments.form.${creating ? "createTitle" : "editTitle"}`)}
      onClose={() => !busy && onClose()}
      foot={
        <>
          <Btn kind="ghost" disabled={busy} onClick={onClose}>{t("v3.common.cancel")}</Btn>
          <Btn kind="primary" disabled={busy} onClick={() => void submit()}>{t(creating ? "v3.structure.form.create" : "v3.structure.form.save")}</Btn>
        </>
      }
    >
      <form style={{ display: "grid", gap: 12 }} data-testid="v3-department-form" onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <label className="v3-field">
          <span>{t("v3.structure.form.name")}</span>
          <input className="v3-input" value={form.name} aria-invalid={errors.name ? true : undefined}
            onChange={(e) => {
              const name = e.target.value;
              setError(null);
              setForm((prev) => ({ ...prev, name, id: idTouched ? prev.id : slugify(name) }));
            }} />
          {errors.name && <small className="v3-err">{t(errors.name)}</small>}
        </label>
        <label className="v3-field">
          <span>{t("v3.structure.form.id")}</span>
          <Reasoned disabledReason={t("story.reason.fixedOnceCreated")}>
            <input className="v3-input mono" value={form.id} disabled={!creating} aria-invalid={errors.id ? true : undefined}
              onChange={(e) => {
                setIdTouched(true);
                setError(null);
                setForm((prev) => ({ ...prev, id: e.target.value }));
              }} />
          </Reasoned>
          {errors.id ? <small className="v3-err">{t(errors.id)}</small> : <small className="v3-hint">{t(creating ? "v3.structure.form.idHint" : "v3.structure.form.idFixed")}</small>}
        </label>
        <label className="v3-field">
          <span>{t("v3.structure.form.description")}</span>
          <textarea className="v3-input" rows={2} value={form.description}
            onChange={(e) => setForm((prev) => ({ ...prev, description: e.target.value }))} />
        </label>
        {error && <Notice s="act">{error}</Notice>}
        <button type="submit" hidden aria-hidden="true" tabIndex={-1} />
      </form>
    </Dialog>
  );
}
