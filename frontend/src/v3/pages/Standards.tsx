import "./standards.css";

import { useEffect, useMemo } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { api, dlcApi } from "../../lib/api";
import type { CriteriaSetPayload } from "../../lib/dlc";
import { useWorkspace } from "../../workspace/workspace-context";
import { useLoad } from "../hooks";
import { Filters, Notice, PageHead, Skeleton } from "../ui";
import { Admission } from "./standards/Admission";
import { Calibration } from "./standards/Calibration";
import { VIEWS, type View } from "./standards/common";
import { CriteriaEditor, CriteriaIndex } from "./standards/Criteria";
import { GoldenCurator, GoldenIndex } from "./standards/Golden";
import { Release } from "./standards/Release";
import { Scorecard } from "./standards/Scorecard";
import { Audit, Compare, Watch } from "./standards/Watch";

/**
 * 判据 — the Agent-DLC workbench in V3. Everything is scoped to one agent (a
 * criteria table, a golden set, a gate and a watch are all that agent's standard),
 * so the agent picker sits in the header. Same `?view=` names and params as V2
 * (`agent`, `lineage`, `golden`, `task`), so every V2 link lands on the same state.
 */
export function V3Standards() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const { current } = useWorkspace();
  const ws = current?.id ?? "";
  const [params, setParams] = useSearchParams();
  const raw = params.get("view") ?? "";
  const view: View = (VIEWS as readonly string[]).includes(raw) ? (raw as View) : "scorecard";
  const agentId = params.get("agent");
  const lineageId = params.get("lineage");
  const goldenId = params.get("golden");
  const taskId = params.get("task");

  const agents = useLoad(() => api.listAgents(), `v3-std-agents:${ws}`);
  const sets = useLoad(() => dlcApi.listSets(agentId ? { agentId } : {}), `v3-std-sets:${ws}:${agentId ?? ""}`);
  const people = useLoad(() => (isAdmin ? api.listUsers({ limit: 100 }) : Promise.resolve(null)), `v3-std-people:${isAdmin}`);
  const payload = useLoad<CriteriaSetPayload | null>(() => (lineageId ? dlcApi.getSet(lineageId) : Promise.resolve(null)), `v3-std-set:${lineageId ?? ""}`);
  const golden = useLoad(() => (goldenId ? dlcApi.getGolden(goldenId) : Promise.resolve(null)), `v3-std-gset:${goldenId ?? ""}`);

  const activeAgents = useMemo(() => (agents.data?.agents ?? []).filter((a) => a.status === "active" || a.status === "deploying"), [agents.data]);
  // default to the first agent so every view has a subject without an extra click
  useEffect(() => {
    if (!agentId && activeAgents.length > 0) {
      const next = new URLSearchParams(params);
      next.set("agent", activeAgents[0].id);
      setParams(next, { replace: true });
    }
  }, [agentId, activeAgents, params, setParams]);

  const setParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(params);
    if (value === null) next.delete(key);
    else next.set(key, value);
    setParams(next);
  };
  const go = (next: string) => {
    const p = new URLSearchParams();
    if (agentId) p.set("agent", agentId);
    p.set("view", next);
    setParams(p);
  };

  const published = (sets.data?.sets ?? []).filter((s) => s.status === "published");
  // the agent's own published table carries the criteria the queue and the bench need
  const agentSet = published.find((s) => s.agent_id === agentId) ?? null;
  const keysPayload = useLoad<CriteriaSetPayload | null>(
    () => (agentSet && agentSet.lineage_id !== lineageId ? dlcApi.getSet(agentSet.lineage_id) : Promise.resolve(null)),
    `v3-std-keys:${agentSet?.lineage_id ?? ""}`,
  );
  const criteriaKeys = ((lineageId ? payload.data : keysPayload.data)?.criteria ?? []).map((c) => c.key);
  const lineageOptions = published.map((s) => ({ value: s.lineage_id, label: `${s.name} v${s.version}` }));
  const annotators = (people.data?.items ?? []).map((u) => u.username);
  const agent = activeAgents.find((a) => a.id === agentId);

  return (
    <div className="v3-reveal v3-std" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={`${t("v3.standards.eyebrow")}${agent ? ` · ${agent.display_name || agent.name}` : ""}`}
        title={t("v3.standards.title")}
        sub={t("v3.standards.sub")}
        end={
          <select className="v3-select" style={{ minWidth: 220 }} value={agentId ?? ""} aria-label={t("v2.dlc.pickAgent")}
            onChange={(e) => setParam("agent", e.target.value || null)}>
            <option value="">{t("v2.dlc.pickAgent")}</option>
            {activeAgents.map((a) => <option key={a.id} value={a.id}>{a.display_name || a.name}</option>)}
          </select>
        }
      />
      <Filters value={view} onChange={go} options={VIEWS.map((v) => ({ value: v, label: t(`v2.dlc.view.${v}`) }))} />

      {agents.loading && !agents.data && <Skeleton rows={4} />}
      {agents.data && activeAgents.length === 0 && <Notice s="wait">{t("v2.dlc.noAgents")}</Notice>}
      {!agentId && activeAgents.length > 0 && <Notice>{t("v2.dlc.pickAgentFirst")}</Notice>}

      {agentId && view === "scorecard" && <Scorecard key={agentId} agentId={agentId} onGo={go} />}

      {view === "criteria" &&
        (lineageId && payload.data ? (
          <CriteriaEditor key={`${payload.data.set.lineage_id}:${payload.data.set.version}`} payload={payload.data} onReload={payload.reload} onBack={() => setParam("lineage", null)} />
        ) : lineageId && payload.loading ? (
          <Skeleton rows={6} />
        ) : (
          <CriteriaIndex agentId={agentId} sets={sets.data?.sets ?? []} loading={sets.loading} error={sets.error} onReload={sets.reload}
            onOpen={(id) => setParam("lineage", id)} />
        ))}

      {view === "golden" &&
        (goldenId && golden.data ? (
          <GoldenCurator goldenSet={golden.data} onReload={golden.reload} onBack={() => setParam("golden", null)} />
        ) : goldenId && golden.loading ? (
          <Skeleton rows={6} />
        ) : (
          <GoldenIndex lineages={lineageOptions} onOpen={(id) => setParam("golden", id)} />
        ))}

      {view === "admission" && <Admission agentId={agentId} criteriaKeys={criteriaKeys} />}

      {view === "calibration" && (
        <Calibration agentId={agentId} lineageId={agentSet?.lineage_id ?? null} criteriaKeys={criteriaKeys} people={annotators}
          taskId={taskId} onOpenTask={(id) => setParam("task", id)} />
      )}

      {agentId && view === "release" && <Release key={agentId} agentId={agentId} />}
      {agentId && view === "watch" && <Watch key={agentId} agentId={agentId} />}
      {agentId && view === "compare" && <Compare key={agentId} agentId={agentId} />}
      {view === "audit" && <Audit agentId={agentId} />}
    </div>
  );
}
