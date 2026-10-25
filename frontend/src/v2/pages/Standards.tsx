/**
 * 判据 (Standards) — the Agent-DLC workbench, one page per the console's `?view=`
 * sub-page convention (`docs/agent-dlc-design.md` §7, §9).
 *
 * Everything on this page is scoped to **one agent**, because a criteria table, a
 * golden set, a gate report and a watch schedule are all that agent's standard; the
 * agent picker is therefore part of the page header rather than a filter.
 *
 * Views: `scorecard` (default) · `criteria` · `golden` · `admission` ·
 * `calibration` · `release` · `watch` · `compare` · `audit`.
 */
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { api, dlcApi, errorMessage } from "../../lib/api";
import type { CriteriaSet, CriteriaSetPayload } from "../../lib/dlc";
import { useLoad, useV2Toast } from "../hooks";
import { Alert, Button, Card, Field, LinkButton, Modal, PageHeader, Select, Spin, SubTabs, Table, Tag } from "../ui";
import { AdmissionReview } from "./dlc/AdmissionReview";
import { CalibrationBench } from "./dlc/CalibrationBench";
import { CriteriaEditor } from "./dlc/CriteriaEditor";
import { GoldenCreate, GoldenCurator } from "./dlc/GoldenCurator";
import { ReleaseGate } from "./dlc/ReleaseGate";
import { Scorecard } from "./dlc/Scorecard";
import { WatchBench } from "./dlc/WatchBench";
import "./dlc/dlc.css";

type View =
  | "scorecard"
  | "criteria"
  | "golden"
  | "admission"
  | "calibration"
  | "release"
  | "watch"
  | "compare"
  | "audit";

const VIEWS: View[] = [
  "scorecard",
  "criteria",
  "golden",
  "admission",
  "calibration",
  "release",
  "watch",
  "compare",
  "audit",
];

/** The criteria tables of one agent, plus the templates a workspace shares. */
function CriteriaIndex({
  agentId,
  sets,
  loading,
  error,
  onReload,
  onOpen,
}: {
  agentId: string | null;
  sets: CriteriaSet[];
  loading: boolean;
  error: string | null;
  onReload: () => void;
  onOpen: (lineageId: string) => void;
}) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const toast = useV2Toast();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [template, setTemplate] = useState("");
  const [busy, setBusy] = useState(false);
  const templates = useLoad(() => dlcApi.listSets({ kind: "template" }), "dlc-templates");

  const create = (kind: "agent" | "template") => {
    setBusy(true);
    const chosen = templates.data?.sets.find((s) => s.lineage_id === template);
    dlcApi
      .createSet({
        kind,
        name,
        agent_id: kind === "agent" ? agentId : null,
        template_lineage_id: kind === "agent" && template ? template : null,
        template_version: kind === "agent" && chosen ? chosen.version : null,
      })
      .then((created) => {
        toast("success", t("v2.dlc.criteria.created"));
        setCreating(false);
        onReload();
        onOpen(created.set.lineage_id);
      })
      .catch((error_: unknown) => toast("error", errorMessage(error_)))
      .finally(() => setBusy(false));
  };

  return (
    <>
      <Card
        title={t("v2.dlc.criteria.indexTitle")}
        sub={t("v2.dlc.criteria.indexSub")}
        end={
          can("criteria.manage") ? (
            <Button
              kind="primary"
              size="sm"
              disabled={!agentId}
              title={agentId ? undefined : t("v2.dlc.pickAgentFirst")}
              onClick={() => {
                setName("");
                setTemplate("");
                setCreating(true);
              }}
            >
              {t("v2.dlc.criteria.create")}
            </Button>
          ) : undefined
        }
        flush
      >
        <Table
          rows={sets}
          rowKey={(row) => `${row.lineage_id}:${row.version}`}
          loading={loading}
          error={error}
          onRetry={onReload}
          empty={t("v2.dlc.criteria.noSets")}
          columns={[
            { key: "name", title: t("v2.common.name"), render: (row) => row.name },
            { key: "kind", title: t("v2.dlc.criteria.kind"), render: (row) => t(`v2.dlc.criteria.kindOf.${row.kind}`) },
            { key: "v", title: t("v2.dlc.criteria.version"), render: (row) => `v${row.version}` },
            {
              key: "status",
              title: t("v2.common.status"),
              render: (row) => (
                <>
                  <Tag tone={row.status === "published" ? "green" : "orange"}>
                    {t(`v2.dlc.criteria.status.${row.status}`)}
                  </Tag>
                  {row.status === "published" && !row.signed_by && (
                    <Tag tone="orange">{t("v2.dlc.criteria.unsigned")}</Tag>
                  )}
                </>
              ),
            },
            { key: "signed", title: t("v2.dlc.criteria.signedByCol"), render: (row) => row.signed_by ?? "—" },
            {
              key: "act",
              title: "",
              render: (row) => <LinkButton onClick={() => onOpen(row.lineage_id)}>{t("v2.common.open")}</LinkButton>,
            },
          ]}
        />
      </Card>

      <Modal
        open={creating}
        title={t("v2.dlc.criteria.createTitle")}
        onClose={() => setCreating(false)}
        footer={
          <>
            <Button onClick={() => setCreating(false)}>{t("v2.common.cancel")}</Button>
            <Button disabled={busy || name.trim() === ""} onClick={() => create("template")}>
              {t("v2.dlc.criteria.createTemplate")}
            </Button>
            <Button kind="primary" disabled={busy || name.trim() === ""} onClick={() => create("agent")}>
              {t("v2.dlc.criteria.createForAgent")}
            </Button>
          </>
        }
      >
        <p className="v2-muted">{t("v2.dlc.criteria.createHint")}</p>
        <Field label={t("v2.common.name")} required>
          <input className="v2-input" value={name} maxLength={96} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label={t("v2.dlc.criteria.fromTemplate")} hint={t("v2.dlc.criteria.fromTemplateHint")}>
          <Select
            value={template}
            onChange={setTemplate}
            placeholder={t("v2.dlc.criteria.noTemplate")}
            options={(templates.data?.sets ?? [])
              .filter((s) => s.status === "published")
              .map((s) => ({ value: s.lineage_id, label: `${s.name} v${s.version}` }))}
          />
        </Field>
      </Modal>
    </>
  );
}

function GoldenIndex({
  lineages,
  onOpen,
}: {
  lineages: { value: string; label: string }[];
  onOpen: (id: string) => void;
}) {
  const { t } = useTranslation();
  const list = useLoad(() => dlcApi.listGolden(), "dlc-golden");
  return (
    <Card
      title={t("v2.dlc.golden.indexTitle")}
      sub={t("v2.dlc.golden.indexSub")}
      end={<GoldenCreate lineages={lineages} onCreated={onOpen} />}
      flush
    >
      <Table
        rows={list.data?.golden_sets ?? []}
        rowKey={(row) => row.id}
        loading={list.loading}
        error={list.error}
        onRetry={list.reload}
        empty={t("v2.dlc.golden.noSets")}
        columns={[
          { key: "name", title: t("v2.common.name"), render: (row) => row.name },
          { key: "dev", title: t("v2.dlc.split.dev"), render: (row) => String(row.splits.dev?.active_items ?? 0) },
          {
            key: "regression",
            title: t("v2.dlc.split.regression"),
            render: (row) => String(row.splits.regression?.active_items ?? 0),
          },
          {
            key: "holdout",
            title: t("v2.dlc.split.holdout"),
            render: (row) =>
              (row.splits.holdout?.items ?? 0) > 0 ? (
                <>
                  {row.splits.holdout?.active_items ?? 0} <Tag tone="green">{t("v2.dlc.golden.sealed")}</Tag>
                </>
              ) : (
                <Tag tone="orange">{t("v2.dlc.golden.unsealed")}</Tag>
              ),
          },
          { key: "act", title: "", render: (row) => <LinkButton onClick={() => onOpen(row.id)}>{t("v2.common.open")}</LinkButton> },
        ]}
      />
    </Card>
  );
}

function AuditTrail({ agentId }: { agentId: string | null }) {
  const { t } = useTranslation();
  const [action, setAction] = useState("");
  const trail = useLoad(() => dlcApi.audit({ action: action || undefined, limit: 200 }), `audit:${action}`);
  const rows = (trail.data?.events ?? []).filter((e) => !agentId || e.target.includes(agentId) || !e.target);
  return (
    <Card
      title={t("v2.dlc.audit.title")}
      sub={t("v2.dlc.audit.sub")}
      end={
        <Select
          value={action}
          onChange={setAction}
          placeholder={t("v2.dlc.audit.allActions")}
          options={["criteria.", "golden.", "calibration.", "release.", "waiver."].map((a) => ({
            value: a,
            label: t(`v2.dlc.audit.family.${a.replace(".", "")}`),
          }))}
        />
      }
      flush
    >
      <Table
        rows={rows}
        rowKey={(row) => row.id}
        loading={trail.loading}
        error={trail.error}
        onRetry={trail.reload}
        empty={t("v2.dlc.audit.empty")}
        columns={[
          { key: "at", title: t("v2.dlc.audit.at"), render: (row) => row.at?.slice(0, 19).replace("T", " ") ?? "—" },
          { key: "actor", title: t("v2.dlc.audit.actor"), render: (row) => row.actor },
          { key: "action", title: t("v2.dlc.audit.action"), render: (row) => <span className="mono">{row.action}</span> },
          { key: "target", title: t("v2.dlc.audit.target"), render: (row) => <span className="mono">{row.target || "—"}</span> },
        ]}
      />
    </Card>
  );
}

export function V2Standards() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const [params, setParams] = useSearchParams();
  const view = (VIEWS.includes((params.get("view") ?? "") as View) ? params.get("view") : "scorecard") as View;
  const agentId = params.get("agent");
  const lineageId = params.get("lineage");
  const goldenId = params.get("golden");
  const taskId = params.get("task");

  const agents = useLoad(() => api.listAgents(), "dlc-agents");
  const sets = useLoad(
    () => dlcApi.listSets(agentId ? { agentId } : {}),
    `dlc-sets:${agentId ?? ""}`,
  );
  const people = useLoad(() => (isAdmin ? api.listUsers({ limit: 100 }) : Promise.resolve(null)), `dlc-people:${isAdmin}`);
  const payload = useLoad<CriteriaSetPayload | null>(
    () => (lineageId ? dlcApi.getSet(lineageId) : Promise.resolve(null)),
    `dlc-set:${lineageId ?? ""}`,
  );
  const golden = useLoad(() => (goldenId ? dlcApi.getGolden(goldenId) : Promise.resolve(null)), `dlc-goldenset:${goldenId ?? ""}`);

  const activeAgents = useMemo(
    () => (agents.data?.agents ?? []).filter((a) => a.status === "active" || a.status === "deploying"),
    [agents.data],
  );
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

  const published = (sets.data?.sets ?? []).filter((s) => s.status === "published");
  const criteriaKeys = (payload.data?.criteria ?? []).map((c) => c.key);
  const lineageOptions = published.map((s) => ({ value: s.lineage_id, label: `${s.name} v${s.version}` }));
  const annotators = (people.data?.items ?? []).map((u) => u.username);

  return (
    <>
      <PageHeader
        title={t("v2.dlc.title")}
        desc={t("v2.dlc.desc")}
        tabs={
          <SubTabs
            value={view}
            onChange={(next) => {
              const params_ = new URLSearchParams();
              if (agentId) params_.set("agent", agentId);
              params_.set("view", next);
              setParams(params_);
            }}
            tabs={VIEWS.map((value) => ({ value, label: t(`v2.dlc.view.${value}`) }))}
          />
        }
        end={
          <Select
            value={agentId ?? ""}
            onChange={(v) => setParam("agent", v || null)}
            placeholder={t("v2.dlc.pickAgent")}
            options={activeAgents.map((a) => ({ value: a.id, label: a.display_name || a.name }))}
          />
        }
      />

      {agents.loading && <Spin />}
      {!agents.loading && activeAgents.length === 0 && <Alert tone="warn">{t("v2.dlc.noAgents")}</Alert>}

      {!agentId && activeAgents.length > 0 && <Alert>{t("v2.dlc.pickAgentFirst")}</Alert>}

      {agentId && view === "scorecard" && <Scorecard key={agentId} agentId={agentId} />}

      {view === "criteria" &&
        (lineageId && payload.data ? (
          <>
            <div className="v2-dlc-crumb">
              <LinkButton onClick={() => setParam("lineage", null)}>{t("v2.dlc.criteria.backToList")}</LinkButton>
            </div>
            <CriteriaEditor payload={payload.data} onReload={payload.reload} />
          </>
        ) : lineageId && payload.loading ? (
          <Spin />
        ) : (
          <CriteriaIndex
            agentId={agentId}
            sets={sets.data?.sets ?? []}
            loading={sets.loading}
            error={sets.error}
            onReload={sets.reload}
            onOpen={(id) => setParam("lineage", id)}
          />
        ))}

      {view === "golden" &&
        (goldenId && golden.data ? (
          <>
            <div className="v2-dlc-crumb">
              <LinkButton onClick={() => setParam("golden", null)}>{t("v2.dlc.golden.backToList")}</LinkButton>
            </div>
            <GoldenCurator goldenSet={golden.data} onReload={golden.reload} />
          </>
        ) : goldenId && golden.loading ? (
          <Spin />
        ) : (
          <GoldenIndex lineages={lineageOptions} onOpen={(id) => setParam("golden", id)} />
        ))}

      {view === "admission" && (
        <AdmissionReview agentId={agentId} criteriaKeys={criteriaKeys.length > 0 ? criteriaKeys : []} />
      )}

      {view === "calibration" && (
        <CalibrationBench
          agentId={agentId}
          lineageId={published.find((s) => s.agent_id === agentId)?.lineage_id ?? null}
          criteriaKeys={criteriaKeys}
          people={annotators}
          taskId={taskId}
          onOpenTask={(id) => setParam("task", id)}
        />
      )}

      {agentId && view === "release" && <ReleaseGate key={agentId} agentId={agentId} />}
      {agentId && (view === "watch" || view === "compare") && (
        <WatchBench key={`${agentId}:${view}`} agentId={agentId} tab={view} />
      )}
      {view === "audit" && <AuditTrail agentId={agentId} />}
    </>
  );
}
