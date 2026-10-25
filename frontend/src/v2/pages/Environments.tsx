import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import {
  type DriftAgent,
  type DriftReport,
  type DriftState,
  type EnvironmentComparison,
  type EnvironmentRow,
  environmentApi,
  errorMessage,
  type WorkspaceTier,
} from "../../lib/api";
import { fmtTime } from "../format";
import { useV2Toast } from "../hooks";
import {
  Alert,
  Button,
  Card,
  type Column,
  Field,
  Kpi,
  PageHeader,
  SubTabs,
  Table,
  Tag,
  type TagTone,
} from "../ui";
import { TierTag } from "./workspaces/tags";

/**
 * T32 — what each environment runs, and whether AWS still matches the ledger.
 *
 * A page of its own (two `?view=` states, the same pattern as Fleet and Costs) rather
 * than a tab of Releases: the promotion queue is about *requests*, this is about the
 * *current state* of an agent across environments, and drift is a workspace-wide check
 * that has nothing to do with any one promotion.
 *
 * Drift reads AWS, so it runs only when asked. "Unknown" is its own colour and is never
 * folded into "in sync": a green cell must mean that AWS was read and agreed.
 */

const VS_TONE: Record<EnvironmentRow["vs_reference"], TagTone> = {
  reference: "blue",
  same: "green",
  differs: "orange",
  absent: "gray",
};

const STATE_TONE: Record<DriftState, TagTone> = {
  in_sync: "green",
  drift: "red",
  unknown: "orange",
};

function CompareView() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [params, setParams] = useSearchParams();
  const [draft, setDraft] = useState(params.get("agent") ?? "");
  const [result, setResult] = useState<EnvironmentComparison | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async (name: string) => {
    const agent = name.trim();
    if (!agent) return;
    setBusy(true);
    try {
      setResult(await environmentApi.compare(agent));
      const next = new URLSearchParams(params);
      next.set("agent", agent);
      setParams(next, { replace: true });
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const columns: Column<EnvironmentRow>[] = [
    {
      key: "env",
      title: t("v2.environments.colEnv"),
      render: (row) => (
        <>
          {row.workspace.name}
          {row.current && <Tag tone="outline">{t("v2.environments.current")}</Tag>}
          <span className="sub mono">
            {row.workspace.id} · {row.workspace.region}
          </span>
        </>
      ),
    },
    {
      key: "tier",
      title: t("v2.environments.colTier"),
      render: (row) => <TierTag tier={row.workspace.tier as WorkspaceTier} />,
    },
    {
      key: "status",
      title: t("v2.environments.colStatus"),
      render: (row) => (row.agent ? row.agent.status : <span className="v2-muted">—</span>),
    },
    {
      key: "version",
      title: t("v2.environments.colVersion"),
      className: "nowrap",
      render: (row) => (row.agent?.version ? `v${row.agent.version}` : "—"),
    },
    {
      key: "spec",
      title: t("v2.environments.colSpec"),
      render: (row) =>
        row.agent ? <span className="mono">{row.agent.spec_digest.slice(0, 12)}</span> : "—",
    },
    {
      key: "deploy",
      title: t("v2.environments.colDeploy"),
      render: (row) =>
        row.agent?.last_deploy ? (
          <>
            {fmtTime(row.agent.last_deploy.started_at)}
            <span className="sub">{row.agent.last_deploy.status}</span>
          </>
        ) : (
          <span className="v2-muted">{t("v2.environments.notDeployed")}</span>
        ),
    },
    {
      key: "vs",
      title: t("v2.environments.colVs"),
      render: (row) => (
        <Tag tone={VS_TONE[row.vs_reference]}>{t(`v2.environments.vs.${row.vs_reference}`)}</Tag>
      ),
    },
  ];

  return (
    <>
      <Card>
        <form
          style={{ display: "flex", gap: 12, alignItems: "flex-end", flexWrap: "wrap" }}
          onSubmit={(e) => {
            e.preventDefault();
            void run(draft);
          }}
        >
          <Field label={t("v2.environments.agentLabel")}>
            <input
              className="v2-input"
              value={draft}
              maxLength={64}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={t("v2.environments.agentPlaceholder")}
              data-testid="env-agent"
            />
          </Field>
          <Button kind="primary" type="submit" disabled={busy || !draft.trim()} testId="env-compare">
            {t("v2.environments.compare")}
          </Button>
        </form>
      </Card>
      {!result && <p className="v2-muted">{t("v2.environments.pickAgent")}</p>}
      {result && (
        <Card
          title={t("v2.environments.compareTitle", { agent: result.agent })}
          sub={t("v2.environments.compareSub")}
          flush
        >
          <div style={{ padding: "0 24px 16px" }}>
            {result.summary.present > 0 &&
              (result.summary.aligned ? (
                <Alert tone="success">{t("v2.environments.aligned")}</Alert>
              ) : (
                <Alert tone="warn">{t("v2.environments.notAligned")}</Alert>
              ))}
            <Table
              columns={columns}
              rows={result.environments}
              rowKey={(row) => row.workspace.id}
              empty={t("v2.environments.empty")}
              testId="env-compare-table"
            />
          </div>
        </Card>
      )}
    </>
  );
}

function detail(t: (key: string, opts?: Record<string, unknown>) => string, agent: DriftAgent) {
  if (agent.findings.length > 0) {
    return agent.findings
      .map((f) =>
        t(`v2.environments.finding.${f.code}`, { expected: f.expected, observed: f.observed }),
      )
      .join("; ");
  }
  return agent.reason ? t(`v2.environments.reason.${agent.reason}`) : "";
}

function DriftView() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [report, setReport] = useState<DriftReport | null>(null);
  const [busy, setBusy] = useState(false);

  const check = async () => {
    setBusy(true);
    try {
      setReport(await environmentApi.drift());
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const columns: Column<DriftAgent>[] = [
    { key: "agent", title: t("v2.environments.colAgent"), render: (row) => row.name },
    {
      key: "state",
      title: t("v2.environments.colState"),
      render: (row) => (
        <Tag tone={STATE_TONE[row.state]}>{t(`v2.environments.state.${row.state}`)}</Tag>
      ),
    },
    { key: "detail", title: t("v2.environments.colDetail"), render: (row) => detail(t, row) },
  ];

  return (
    <>
      <Card
        title={t("v2.environments.driftTitle")}
        sub={t("v2.environments.driftSub")}
        end={
          <Button kind="primary" disabled={busy} onClick={() => void check()} testId="env-drift-check">
            {busy ? t("v2.environments.checking") : t("v2.environments.check")}
          </Button>
        }
      >
        {!report && <p className="v2-muted">{t("v2.environments.notChecked")}</p>}
        {report && (
          <>
            <div className="v2-kpis">
              <Kpi label={t("v2.environments.kpiChecked")} value={report.checked} />
              <Kpi label={t("v2.environments.kpiInSync")} value={report.counts.in_sync} />
              <Kpi
                label={t("v2.environments.kpiDrift")}
                value={report.counts.drift}
                tone={report.counts.drift > 0 ? "bad" : undefined}
              />
              <Kpi label={t("v2.environments.kpiUnknown")} value={report.counts.unknown} />
            </div>
            {report.truncated && <Alert tone="warn">{t("v2.environments.truncated")}</Alert>}
            {report.counts.unknown > 0 && (
              <Alert tone="info">{t("v2.environments.unknownNote")}</Alert>
            )}
            <Table
              columns={columns}
              rows={report.agents}
              rowKey={(row) => row.agent_id}
              empty={t("v2.environments.noAgents")}
              testId="env-drift-table"
            />
          </>
        )}
      </Card>
    </>
  );
}

export function V2Environments() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const view = params.get("view") === "drift" ? "drift" : "compare";

  return (
    <>
      <PageHeader
        title={t("v2.environments.title")}
        desc={t("v2.environments.desc")}
        tabs={
          <SubTabs
            value={view}
            onChange={(next) => setParams(next === "compare" ? {} : { view: next })}
            tabs={(["compare", "drift"] as const).map((value) => ({
              value,
              label: t(`v2.environments.tab.${value}`),
            }))}
          />
        }
      />
      {view === "compare" ? <CompareView /> : <DriftView />}
    </>
  );
}
