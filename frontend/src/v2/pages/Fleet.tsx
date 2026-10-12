import { AlertTriangle, ExternalLink, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import {
  api,
  errorMessage,
  type FleetRow,
  type SharedTemplateInfo,
} from "../../lib/api";
import { fmtTime } from "../format";
import { useLoad, useV2Toast } from "../hooks";
import { TierTag } from "./workspaces/tags";
import type { WorkspaceTier } from "../../lib/api";
import {
  Alert,
  Button,
  Card,
  type Column,
  Kpi,
  PageHeader,
  SubTabs,
  Table,
  Tag,
  type TagTone,
} from "../ui";

/**
 * T37/T38/T39 — the cross-environment view.
 *
 * Three `?view=` states because they answer the same administrator question at different
 * altitudes: the fleet (where is anything wrong), governance health for the selected
 * workspace (what specifically, and the link that fixes it), and the shared templates
 * teams publish for each other.
 *
 * The fleet table is a ledger read, so a workspace that cannot be served reports as
 * unreadable rather than as a healthy empty row — the counts are blanked, not zeroed.
 */

const SEVERITY_TONE: Record<string, TagTone> = {
  action: "red",
  warn: "orange",
  info: "gray",
};

function FleetView() {
  const { t } = useTranslation();
  const report = useLoad(() => api.fleet(), "fleet");
  const data = report.data;

  const columns: Column<FleetRow>[] = [
    {
      key: "ws",
      title: t("v2.fleet.colWorkspace"),
      render: (row) => (
        <>
          <Link to="/v2/workspaces">{row.name}</Link>
          <span className="sub mono">
            {row.account_id} · {row.region}
          </span>
        </>
      ),
    },
    {
      key: "tier",
      title: t("v2.fleet.colTier"),
      render: (row) => <TierTag tier={row.tier as WorkspaceTier} />,
    },
    {
      key: "state",
      title: t("v2.fleet.colState"),
      render: (row) =>
        row.readable ? (
          <Tag tone="green">{t("v2.fleet.ready")}</Tag>
        ) : (
          <Tag tone="orange">{t(`v2.workspaces.status.${row.bootstrap_status}`)}</Tag>
        ),
    },
    {
      key: "agents",
      title: t("v2.fleet.colAgents"),
      className: "nowrap",
      // A blank, not a zero: this environment could not be read.
      render: (row) => (row.readable ? row.agents_active : "—"),
    },
    {
      key: "problems",
      title: t("v2.fleet.colProblems"),
      render: (row) =>
        row.readable ? (
          <>
            {(row.agents_failed || 0) > 0 && (
              <Tag tone="red">{t("v2.fleet.agentsFailed", { count: row.agents_failed ?? 0 })}</Tag>
            )}
            {(row.jobs_failed || 0) > 0 && (
              <Tag tone="orange">{t("v2.fleet.jobsFailed", { count: row.jobs_failed ?? 0 })}</Tag>
            )}
            {(row.alerts_firing || 0) > 0 && (
              <Tag tone="red">{t("v2.fleet.alertsFiring", { count: row.alerts_firing ?? 0 })}</Tag>
            )}
            {(row.promotions_pending || 0) > 0 && (
              <Tag tone="blue">
                {t("v2.fleet.promotionsPending", { count: row.promotions_pending ?? 0 })}
              </Tag>
            )}
          </>
        ) : (
          <span className="v2-muted">{t("v2.fleet.unreadable")}</span>
        ),
    },
  ];

  return (
    <>
      <div className="v2-kpis">
        <Kpi label={t("v2.fleet.kpiWorkspaces")} value={data?.totals.workspaces ?? "—"} />
        <Kpi
          label={t("v2.fleet.kpiAttention")}
          value={data?.totals.needs_attention ?? "—"}
          tone={data && data.totals.needs_attention > 0 ? "bad" : undefined}
        />
        <Kpi label={t("v2.fleet.kpiAgents")} value={data?.totals.agents_active ?? "—"} />
        <Kpi
          label={t("v2.fleet.kpiAlerts")}
          value={data?.totals.alerts_firing ?? "—"}
          tone={data && data.totals.alerts_firing > 0 ? "bad" : undefined}
        />
      </div>
      <Card title={t("v2.fleet.tableTitle")} sub={t("v2.fleet.tableSub")} flush>
        <div style={{ padding: "0 24px 16px" }}>
          <Table
            columns={columns}
            rows={data?.workspaces ?? []}
            rowKey={(row) => row.id}
            loading={report.loading}
            error={report.error}
            onRetry={report.reload}
            empty={t("v2.fleet.empty")}
          />
        </div>
      </Card>
    </>
  );
}

function HealthView() {
  const { t } = useTranslation();
  const health = useLoad(() => api.governanceHealth(), "gov-health");
  const data = health.data;
  const tone = data?.grade === "good" ? undefined : data?.grade === "poor" ? "bad" : undefined;

  return (
    <>
      <div className="v2-kpis">
        <Kpi
          label={t("v2.fleet.kpiScore")}
          value={data ? data.score : "—"}
          tone={tone}
          sub={data ? t(`v2.fleet.grade.${data.grade}`) : undefined}
        />
        <Kpi label={t("v2.fleet.kpiFindings")} value={data?.findings.length ?? "—"} />
        <Kpi label={t("v2.fleet.kpiConsidered")} value={data?.agents_considered ?? "—"} />
      </div>
      <Card title={t("v2.fleet.findings")} sub={t("v2.fleet.findingsSub")}>
        {data && data.findings.length === 0 && (
          <p className="v2-muted">{t("v2.fleet.noFindings")}</p>
        )}
        {(data?.findings ?? []).map((finding) => (
          <div key={finding.key} className="v2-inbox-item" data-testid={`v2-health-${finding.key}`}>
            <Tag tone={SEVERITY_TONE[finding.severity]}>
              {t(`v2.inbox.severity.${finding.severity}`)}
            </Tag>
            <span className="n">{finding.count}</span>
            <span>{t(`v2.fleet.finding.${finding.key}`, { count: finding.count })}</span>
            {finding.sample.length > 0 && <span className="s">{finding.sample.join(" · ")}</span>}
            <Link to={finding.to} style={{ marginLeft: "auto", whiteSpace: "nowrap" }}>
              {t("v2.fleet.fix")}
              <ExternalLink size={11} aria-hidden="true" />
            </Link>
          </div>
        ))}
      </Card>
    </>
  );
}

function MarketplaceView() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useV2Toast();
  const [tick, setTick] = useState(0);
  const list = useLoad(() => api.sharedTemplates(), `templates:${tick}`);
  const rows = list.data?.templates ?? [];

  const take = async (row: SharedTemplateInfo) => {
    try {
      const entry = await api.useSharedTemplate(row.id);
      // The template only hands over defaults — the consumer still goes through the
      // wizard and supplies their own knowledge bases and tools.
      navigate(`/v2/agents?${new URLSearchParams({ view: "new", template: entry.id })}`);
    } catch (error) {
      toast("error", errorMessage(error));
    }
  };

  const withdraw = async (row: SharedTemplateInfo) => {
    try {
      await api.unpublishTemplate(row.id);
      toast("success", t("v2.fleet.withdrawn"));
      setTick((n) => n + 1);
    } catch (error) {
      toast("error", errorMessage(error));
    }
  };

  return (
    <Card title={t("v2.fleet.marketTitle")} sub={t("v2.fleet.marketSub")}>
      {list.loading && <p className="v2-muted">…</p>}
      {!list.loading && rows.length === 0 && <p className="v2-muted">{t("v2.fleet.marketEmpty")}</p>}
      {rows.map((row) => (
        <div key={row.id} className="v2-market-row" data-testid={`v2-template-${row.id}`}>
          <div>
            <strong>{row.title}</strong>
            {row.own && <Tag tone="blue">{t("v2.fleet.ours")}</Tag>}
            <Tag tone="gray">{t(`v2.agents.wizard.method.${row.method}`)}</Tag>
            <div className="v2-muted">{row.summary}</div>
            <div className="sub">
              {t("v2.fleet.publishedBy", {
                who: row.published_by ?? "—",
                where: row.source_workspace_id,
                when: fmtTime(row.updated_at),
              })}
              {" · "}
              {t("v2.fleet.uses", { count: row.uses })}
            </div>
            {row.requirements.length > 0 && (
              <Alert tone="warn">
                <AlertTriangle size={12} aria-hidden="true" />{" "}
                {t("v2.fleet.needs", {
                  items: row.requirements.map((need) => need.label).join(", "),
                })}
              </Alert>
            )}
          </div>
          <div className="v2-market-actions">
            <Button size="sm" kind="primary" onClick={() => void take(row)}>
              {t("v2.fleet.use")}
            </Button>
            {row.own && (
              <Button size="sm" kind="danger" onClick={() => void withdraw(row)}>
                <Trash2 size={13} aria-hidden="true" />
              </Button>
            )}
          </div>
        </div>
      ))}
    </Card>
  );
}

export function V2Fleet() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const raw = params.get("view");
  const view = raw === "health" || raw === "market" ? raw : "fleet";

  return (
    <>
      <PageHeader
        title={t("v2.fleet.title")}
        desc={t("v2.fleet.desc")}
        tabs={
          <SubTabs
            value={view}
            onChange={(next) => setParams(next === "fleet" ? {} : { view: next })}
            tabs={(["fleet", "health", "market"] as const).map((value) => ({
              value,
              label: t(`v2.fleet.tab.${value}`),
            }))}
          />
        }
      />
      {view === "fleet" && <FleetView />}
      {view === "health" && <HealthView />}
      {view === "market" && <MarketplaceView />}
    </>
  );
}
