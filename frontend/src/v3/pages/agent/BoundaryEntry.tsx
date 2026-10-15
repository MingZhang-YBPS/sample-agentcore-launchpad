import { ShieldAlert, ShieldCheck } from "lucide-react";
import { useTranslation } from "react-i18next";

import { projectIsolationApi } from "../../../lib/api/projects";
import { useProject } from "../../../workspace/project-context";
import { useLoad } from "../../hooks";
import { Chip, Lamp, Panel } from "../../ui";
import "../projects/isolation.css";

/**
 * Identity & access: the project permissions boundary the agent's role carries
 * (projects design §10.14) — "launchpad-project-<pid> boundary-v3 ✓", or a drift
 * warning when the role carries none or an older version than the project's.
 * Nothing at all for an agent of a shared project that never had one (every
 * single-project workspace), so those pages read exactly as before.
 */
export function BoundaryEntry({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const { current } = useProject();
  const projectId = current?.id ?? null;
  const loaded = useLoad(
    () => (projectId ? projectIsolationApi.agentBoundary(projectId, agentId) : Promise.resolve(null)),
    `v3-agent-boundary:${projectId ?? "-"}:${agentId}`,
  );
  const b = loaded.data;
  if (!b || (b.mode === "shared" && !b.applied)) return null;

  const ok = !b.drift && b.applied !== null;
  const moving = b.migration && (b.migration.status === "running" || b.migration.status === "pending");
  return (
    <Panel title={t("v3.projects.isolation.boundary.title")} signal={b.drift && !moving ? "wait" : undefined}>
      <div className="v3-boundary" data-testid="v3-agent-boundary">
        {ok ? <ShieldCheck size={15} color="var(--v3-ok)" /> : <ShieldAlert size={15} color="var(--v3-wait)" />}
        <span className="policy">{b.policy_name ?? t("v3.projects.isolation.boundary.noPolicy")}</span>
        <span className="ver">{b.applied ?? t("v3.projects.isolation.boundary.none")}</span>
        {ok && <Chip s="ok"><Lamp s="ok" /> {t("v3.projects.isolation.boundary.current")}</Chip>}
        {b.drift && !moving && (
          <Chip s="wait">
            {b.applied
              ? t("v3.projects.isolation.boundary.drift", { current: b.current ?? "?" })
              : t("v3.projects.isolation.boundary.missing")}
          </Chip>
        )}
        {moving && <Chip s="info"><Lamp s="info" live /> {t("v3.projects.isolation.boundary.migrating")}</Chip>}
        {b.mode === "shared" && b.applied && <Chip s="wait">{t("v3.projects.isolation.boundary.leftover")}</Chip>}
      </div>
      {b.drift && !moving && <small className="v3-hint">{t("v3.projects.isolation.boundary.driftHint")}</small>}
    </Panel>
  );
}
