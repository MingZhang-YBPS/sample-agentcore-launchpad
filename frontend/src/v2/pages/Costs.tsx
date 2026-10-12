import { Bell, BellOff, RefreshCw, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import {
  type AlertKind,
  type AlertRuleInfo,
  api,
  type CostActorRow,
  type CostRow,
  errorMessage,
} from "../../lib/api";
import { fmtTime } from "../format";
import { useLoad, useV2Toast } from "../hooks";
import {
  Alert,
  Button,
  Card,
  type Column,
  Field,
  Kpi,
  Modal,
  PageHeader,
  Select,
  Spin,
  SubTabs,
  Table,
  Tag,
  type TagTone,
} from "../ui";

/**
 * T28/T29 — where the money went, and what the platform watches.
 *
 * One page with two `?view=` states because the two answer the same operator question at
 * different moments: "what is this costing" and "tell me when something is wrong". Both
 * read values the platform already computes; neither adds a telemetry path.
 *
 * Spend is labelled an estimate everywhere, and models the price map cannot value are
 * named rather than silently counted as free.
 */

const RANGES = ["1h", "6h", "24h", "7d", "30d"] as const;
// error rate and latency read the observability dashboard, which has no 30-day range
const DASHBOARD_RANGES = ["1h", "6h", "24h", "7d"] as const;

const STATE_TONE: Record<AlertRuleInfo["state"], TagTone> = {
  firing: "red",
  ok: "green",
  unknown: "gray",
};

/** Money to two decimals; a null price is "—", never 0. */
function usd(value: number | null | undefined): string {
  return value == null ? "—" : `$${value.toFixed(2)}`;
}

function CostsView() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [range, setRange] = useState<string>("24h");
  const [tick, setTick] = useState(0);
  const report = useLoad(() => api.costs(range, tick > 0), `costs:${range}:${tick}`);
  const data = report.data;

  const agentColumns: Column<CostRow>[] = [
    {
      key: "agent",
      title: t("v2.costs.colAgent"),
      render: (row) => (
        <>
          <span>{row.display_name || row.service}</span>
          {!row.known && <Tag tone="gray">{t("v2.costs.notInLedger")}</Tag>}
        </>
      ),
    },
    {
      key: "cost",
      title: t("v2.costs.colCost"),
      className: "nowrap",
      render: (row) => usd(row.est_cost_usd),
    },
    {
      key: "tokens",
      title: t("v2.costs.colTokens"),
      className: "nowrap",
      render: (row) => row.tokens.toLocaleString(),
    },
    { key: "calls", title: t("v2.costs.colCalls"), render: (row) => row.llm_calls },
  ];

  const actorColumns: Column<CostActorRow>[] = [
    {
      key: "actor",
      title: t("v2.costs.colActor"),
      render: (row) =>
        row.actor === "—" ? <span className="v2-muted">{t("v2.costs.unattributed")}</span> : row.actor,
    },
    { key: "cost", title: t("v2.costs.colCost"), className: "nowrap", render: (row) => usd(row.est_cost_usd) },
    {
      key: "tokens",
      title: t("v2.costs.colTokens"),
      className: "nowrap",
      render: (row) => row.tokens.toLocaleString(),
    },
    { key: "sessions", title: t("v2.costs.colSessions"), render: (row) => row.sessions },
  ];

  return (
    <>
      <div className="v2-kpis">
        <Kpi label={t("v2.costs.kpiTotal")} value={usd(data?.total_est_cost_usd)} sub={t("v2.costs.estimate")} />
        <Kpi
          label={t("v2.costs.kpiTokens")}
          value={data ? data.total_tokens.toLocaleString() : "—"}
          sub={t("v2.costs.window", { range })}
        />
        <Kpi label={t("v2.costs.kpiAgents")} value={data?.by_agent.length ?? "—"} />
      </div>
      {data && data.unpriced_models.length > 0 && (
        <Alert tone="warn">
          {t("v2.costs.unpriced", { models: data.unpriced_models.join(", ") })}
        </Alert>
      )}
      <Card
        title={t("v2.costs.byAgent")}
        sub={t("v2.costs.byAgentSub")}
        flush
        end={
          <>
            <Select
              value={range}
              options={RANGES.map((value) => ({ value, label: value }))}
              onChange={setRange}
              testId="v2-costs-range"
            />
            <Button
              size="sm"
              onClick={() => {
                setTick((n) => n + 1);
                toast("success", t("v2.costs.refreshing"));
              }}
            >
              <RefreshCw size={13} aria-hidden="true" />
              {t("v2.costs.refresh")}
            </Button>
          </>
        }
      >
        <div style={{ padding: "0 24px 16px" }}>
          <Table
            columns={agentColumns}
            rows={data?.by_agent ?? []}
            rowKey={(row) => row.service}
            loading={report.loading}
            error={report.error}
            onRetry={report.reload}
            empty={t("v2.costs.empty")}
          />
        </div>
      </Card>
      <Card title={t("v2.costs.byActor")} sub={t("v2.costs.byActorSub")} flush>
        <div style={{ padding: "0 24px 16px" }}>
          <Table
            columns={actorColumns}
            rows={data?.by_actor ?? []}
            rowKey={(row) => row.actor}
            loading={report.loading}
            empty={t("v2.costs.empty")}
          />
        </div>
      </Card>
    </>
  );
}

const KIND_UNIT: Record<AlertKind, string> = {
  error_rate: "%",
  latency_p95_ms: "ms",
  online_quality: "",
  cost_mtd_usd: "$",
};

/** A rule's value in the unit the operator typed it in. */
function shown(kind: AlertKind, value: number | null): string {
  if (value == null) return "—";
  if (kind === "error_rate") return `${(value * 100).toFixed(1)}%`;
  if (kind === "cost_mtd_usd") return `$${value.toFixed(2)}`;
  if (kind === "online_quality") return value.toFixed(2);
  return `${Math.round(value)} ms`;
}

function NewRuleModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [kind, setKind] = useState<AlertKind>("error_rate");
  const [name, setName] = useState("");
  const [threshold, setThreshold] = useState("");
  const [window, setWindow] = useState("24h");
  const [webhook, setWebhook] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    // Typed in percent for a rate, stored as the 0–1 fraction the backend compares.
    const raw = Number(threshold);
    const value = kind === "error_rate" ? raw / 100 : raw;
    setBusy(true);
    try {
      await api.createAlertRule({
        kind,
        name: name.trim() || undefined,
        threshold: value,
        window: kind === "cost_mtd_usd" ? undefined : window,
        webhook_url: webhook.trim() || null,
      });
      toast("success", t("v2.alerts.created"));
      onDone();
      onClose();
    } catch (error) {
      toast("error", errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  const valid = threshold.trim() !== "" && Number.isFinite(Number(threshold));

  return (
    <Modal
      title={t("v2.alerts.newTitle")}
      open
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>{t("common.cancel")}</Button>
          <Button kind="primary" disabled={busy || !valid} onClick={() => void submit()} testId="v2-alert-submit">
            {busy ? <Spin /> : t("v2.alerts.create")}
          </Button>
        </>
      }
    >
      <div className="v2-form">
        <Field label={t("v2.alerts.kind")} hint={t(`v2.alerts.kindHint.${kind}`)}>
          <Select
            value={kind}
            options={(Object.keys(KIND_UNIT) as AlertKind[]).map((value) => ({
              value,
              label: t(`v2.alerts.kindName.${value}`),
            }))}
            onChange={(v) => {
              setKind(v as AlertKind);
              if (v !== "online_quality" && window === "30d") setWindow("7d");
            }}
            testId="v2-alert-kind"
          />
        </Field>
        <Field label={t("v2.alerts.name")}>
          <input className="v2-input" maxLength={64} value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field
          label={t("v2.alerts.threshold", {
            direction: t(`v2.alerts.direction.${kind === "online_quality" ? "below" : "above"}`),
            unit: KIND_UNIT[kind] || "",
          })}
          required
        >
          <input
            className="v2-input"
            inputMode="decimal"
            value={threshold}
            onChange={(e) => setThreshold(e.target.value)}
            data-testid="v2-alert-threshold"
          />
        </Field>
        {kind !== "cost_mtd_usd" && (
          <Field label={t("v2.alerts.window")} hint={t("v2.alerts.windowHint")}>
            <Select
              value={window}
              options={(kind === "online_quality" ? RANGES : DASHBOARD_RANGES).map((value) => ({
                value,
                label: value,
              }))}
              onChange={setWindow}
            />
          </Field>
        )}
        <Field label={t("v2.alerts.webhook")} hint={t("v2.alerts.webhookHint")}>
          <input
            className="v2-input"
            placeholder="https://…"
            value={webhook}
            onChange={(e) => setWebhook(e.target.value)}
            data-testid="v2-alert-webhook"
          />
        </Field>
      </div>
    </Modal>
  );
}

function AlertsView() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [tick, setTick] = useState(0);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const list = useLoad(() => api.alertRules(), `alerts:${tick}`);
  const rows = list.data?.rules ?? [];

  const refresh = () => setTick((n) => n + 1);

  const recheck = async () => {
    setBusy(true);
    try {
      // notify=false: opening or refreshing the page must never page anyone
      const result = await api.evaluateAlerts(false);
      toast("success", t("v2.alerts.rechecked", { firing: result.firing }));
      refresh();
    } catch (error) {
      toast("error", errorMessage(error));
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (rule: AlertRuleInfo) => {
    try {
      await api.updateAlertRule(rule.id, { enabled: !rule.enabled });
      refresh();
    } catch (error) {
      toast("error", errorMessage(error));
    }
  };

  const remove = async (rule: AlertRuleInfo) => {
    try {
      await api.deleteAlertRule(rule.id);
      toast("success", t("v2.alerts.deleted"));
      refresh();
    } catch (error) {
      toast("error", errorMessage(error));
    }
  };

  const columns: Column<AlertRuleInfo>[] = [
    {
      key: "rule",
      title: t("v2.alerts.colRule"),
      render: (row) => (
        <>
          <span>{row.name || t(`v2.alerts.kindName.${row.kind}`)}</span>
          <span className="sub">
            {t(`v2.alerts.kindName.${row.kind}`)} {t(`v2.alerts.direction.${row.comparison}`)}{" "}
            {shown(row.kind, row.threshold)}
            {row.kind !== "cost_mtd_usd" ? ` · ${row.window}` : ""}
          </span>
        </>
      ),
    },
    {
      key: "state",
      title: t("v2.alerts.colState"),
      render: (row) => (
        <Tag tone={row.enabled ? STATE_TONE[row.state] : "gray"}>
          {row.enabled ? t(`v2.alerts.state.${row.state}`) : t("v2.alerts.state.disabled")}
        </Tag>
      ),
    },
    {
      key: "value",
      title: t("v2.alerts.colValue"),
      render: (row) => (
        <>
          <span>{shown(row.kind, row.last_value)}</span>
          {row.last_detail && <span className="sub">{row.last_detail}</span>}
        </>
      ),
    },
    {
      key: "checked",
      title: t("v2.alerts.colChecked"),
      className: "nowrap",
      render: (row) => fmtTime(row.last_checked_at),
    },
    {
      key: "actions",
      title: "",
      render: (row) => (
        <>
          <Button size="sm" onClick={() => void toggle(row)} title={t("v2.alerts.toggle")}>
            {row.enabled ? <BellOff size={13} aria-hidden="true" /> : <Bell size={13} aria-hidden="true" />}
          </Button>
          <Button size="sm" kind="danger" onClick={() => void remove(row)} title={t("v2.alerts.delete")}>
            <Trash2 size={13} aria-hidden="true" />
          </Button>
        </>
      ),
    },
  ];

  const firing = rows.filter((row) => row.state === "firing" && row.enabled).length;
  const unknown = rows.filter((row) => row.state === "unknown" && row.enabled).length;

  return (
    <>
      <div className="v2-kpis">
        <Kpi label={t("v2.alerts.kpiFiring")} value={firing} tone={firing ? "bad" : undefined} />
        <Kpi label={t("v2.alerts.kpiUnknown")} value={unknown} sub={t("v2.alerts.unknownSub")} />
        <Kpi label={t("v2.alerts.kpiTotal")} value={rows.length} />
      </div>
      <Card
        title={t("v2.alerts.listTitle")}
        sub={t("v2.alerts.listSub")}
        flush
        end={
          <>
            <Button size="sm" disabled={busy} onClick={() => void recheck()} testId="v2-alerts-recheck">
              {busy ? <Spin /> : <RefreshCw size={13} aria-hidden="true" />}
              {t("v2.alerts.recheck")}
            </Button>
            <Button size="sm" kind="primary" onClick={() => setAdding(true)} testId="v2-alerts-new">
              {t("v2.alerts.new")}
            </Button>
          </>
        }
      >
        <div style={{ padding: "0 24px 16px" }}>
          <Table
            columns={columns}
            rows={rows}
            rowKey={(row) => row.id}
            loading={list.loading}
            error={list.error}
            onRetry={list.reload}
            empty={t("v2.alerts.empty")}
          />
        </div>
      </Card>
      {adding && <NewRuleModal onClose={() => setAdding(false)} onDone={refresh} />}
    </>
  );
}

export function V2Costs() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const view = params.get("view") === "alerts" ? "alerts" : "spend";

  return (
    <>
      <PageHeader
        title={t("v2.costs.title")}
        desc={t("v2.costs.desc")}
        tabs={
          <SubTabs
            value={view}
            onChange={(next) => setParams(next === "spend" ? {} : { view: next })}
            tabs={(["spend", "alerts"] as const).map((value) => ({
              value,
              label: t(`v2.costs.tab.${value}`),
            }))}
          />
        }
      />
      {view === "spend" ? <CostsView /> : <AlertsView />}
    </>
  );
}
