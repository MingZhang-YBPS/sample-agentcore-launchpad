import { Check, X } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { errorMessage } from "../../../lib/api";
import { type StewardChange, stewardApi } from "../../../lib/api/steward";
import { ago } from "../../format";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Notice, Panel } from "../../ui";
import type { ProjectAbilities } from "./ProjectDetail";
import { changeSummary, stewardTraceFor } from "./stewardModel";

function Changes({ changes }: { changes: StewardChange[] }) {
  const { t } = useTranslation();
  const { upserts, removes } = changeSummary(changes);
  return (
    <span className="v3-prj-chips">
      {upserts.map((u) => (
        <Chip key={`u:${u.name}`} s="ok">
          + {u.name}{u.role ? ` · ${t(`v3.project.roles.${u.role}`, { defaultValue: u.role })}` : ""}
        </Chip>
      ))}
      {removes.map((name) => <Chip key={`r:${name}`} s="act">− {name}</Chip>)}
    </span>
  );
}

/**
 * §11.4 for the project's owners: what an organization admin (steward) changed in
 * this project's members, and — under the two-person rule — the changes waiting for
 * an owner's confirmation. Hub-global reads (`/api/steward/*`), narrowed to this
 * project; nothing is drawn while the project has no trace.
 */
export function StewardSection({
  projectId,
  abilities,
  onChanged,
}: {
  projectId: string;
  abilities: ProjectAbilities;
  /** a confirmed change moved the members */
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const [tick, setTick] = useState(0);
  const notices = useLoad(() => stewardApi.notices(), `v3-steward-notices:${tick}`);
  const pending = useLoad(() => stewardApi.pending(), `v3-steward-pending:${tick}`);
  const [busy, setBusy] = useState<string | null>(null);
  const trace = stewardTraceFor(projectId, notices.data?.items ?? [], pending.data?.items ?? []);
  if (!abilities.manage || (trace.notices.length === 0 && trace.pending.length === 0)) return null;

  const run = async (id: string, call: () => Promise<unknown>, doneKey: string, moved = false) => {
    setBusy(id);
    try {
      await call();
      toast("ok", t(doneKey));
      setTick((n) => n + 1);
      if (moved) onChanged();
    } catch (err) {
      toast("act", errorMessage(err), err);
    } finally {
      setBusy(null);
    }
  };

  return (
    <Panel title={t("v3.steward.trace.title")}>
      <div style={{ display: "grid", gap: 12 }} data-testid="project-steward-trace">
        <small className="v3-hint">{t("v3.steward.trace.sub")}</small>
        {trace.pending.map((p) => (
          <Notice key={p.id} s="wait">
            <div style={{ display: "grid", gap: 6 }} data-testid={`steward-pending-${p.id}`}>
              <span>{t("v3.steward.trace.pending", { actor: p.requested_by, when: ago(p.created_at) })}</span>
              <Changes changes={p.changes} />
              <span style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                {p.can_confirm && (
                  <Btn size="sm" kind="primary" disabled={busy !== null}
                    onClick={() => void run(p.id, () => stewardApi.confirm(p.id), "v3.steward.trace.confirmed", true)}>
                    <Check size={13} /> {t("v3.steward.trace.confirm")}
                  </Btn>
                )}
                <Btn size="sm" disabled={busy !== null}
                  onClick={() => void run(p.id, () => stewardApi.reject(p.id), p.mine ? "v3.steward.trace.withdrawn" : "v3.steward.trace.rejected")}>
                  <X size={13} /> {t(p.mine ? "v3.steward.trace.withdraw" : "v3.steward.trace.reject")}
                </Btn>
              </span>
            </div>
          </Notice>
        ))}
        {trace.notices.map((n) => (
          <div key={n.id} style={{ display: "grid", gap: 6 }} data-testid={`steward-notice-${n.id}`}>
            <span style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              {!n.read && <Chip s="info">{t("v3.steward.trace.unread")}</Chip>}
              {t(n.kind === "pending" ? "v3.steward.trace.noticePending" : "v3.steward.trace.noticeChanged", { actor: n.actor, when: ago(n.created_at) })}
              {!n.read && (
                <Btn size="sm" kind="ghost" disabled={busy !== null}
                  onClick={() => void run(n.id, () => stewardApi.acknowledge(n.id), "v3.steward.trace.acknowledged")}>
                  {t("v3.steward.trace.acknowledge")}
                </Btn>
              )}
            </span>
            <Changes changes={n.changes} />
          </div>
        ))}
      </div>
    </Panel>
  );
}
