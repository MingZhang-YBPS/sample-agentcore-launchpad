/**
 * "From project X (read / use)" — the label of a row another project lent this one
 * (projects design §11.2). A loan, not a possession: the outline is dashed like a
 * ticket stub, the lending project's name leads, and the access level sits in the
 * stub — `use` in the info signal (agents and evaluations may use it), `read` muted.
 * Renders nothing for the project's own rows, so lists stay as they were.
 */
import { ArrowDownLeft } from "lucide-react";
import { useTranslation } from "react-i18next";

import { sharedFromOf } from "../../lib/api/sharedFrom";
import "./sharedFrom.css";

export function SharedFromTag({ row }: { row: unknown }) {
  const { t } = useTranslation();
  const lent = sharedFromOf(row);
  if (!lent) return null;
  const title = t(
    lent.access === "use" ? "v3.projects.shares.label.titleUse" : "v3.projects.shares.label.titleRead",
    { name: lent.name },
  );
  return (
    <span className="v3-lent" data-access={lent.access} title={title} aria-label={title}>
      <span className="v3-lent-from" aria-hidden>
        <ArrowDownLeft size={11} strokeWidth={2.2} />
        {t("v3.projects.shares.label.from", { name: lent.name })}
      </span>
      <span className="v3-lent-stub" aria-hidden>
        {t(`v3.projects.shares.label.${lent.access}`)}
      </span>
    </span>
  );
}
