import "./observability.css";

import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, type V2Range } from "../../lib/api";
import { asRange, asTab, type ObsTab, useCacheAge } from "../../v2/pages/observability/common";
import { useWorkspace } from "../../workspace/workspace-context";
import { useLoad } from "../hooks";
import { Btn, Filters, Notice, PageHead, Panel, Skeleton } from "../ui";
import { type DashTarget, DashboardView } from "./observability/Dashboard";
import { findings } from "./observability/attention";
import { SessionsView, TracesView } from "./observability/Lists";
import { SessionView } from "./observability/SessionView";
import { TraceView } from "./observability/TraceView";

const RANGES: V2Range[] = ["1h", "6h", "24h", "7d"];

function CacheAge({ age, stamp, loading }: { age: number | null | undefined; stamp: unknown; loading: boolean }) {
  const { t } = useTranslation();
  const seconds = useCacheAge(age, stamp);
  if (loading) return <span className="v3-obs-cache">{t("v3.obs.loading")}</span>;
  return seconds == null ? null : <span className="v3-obs-cache">{t("v3.obs.cachedAgo", { s: seconds })}</span>;
}

/**
 * 可观测 — AgentCore telemetry (aws/spans via Transaction Search), same routes and
 * cache as V2: `?tab=dashboard|sessions|traces`, `?range=1h|6h|24h|7d`, and the
 * detail sub-pages `?view=session&id=` / `?view=trace&id=`. Refresh bypasses the
 * backend cache (`force`). The dashboard leads with what needs a look.
 */
export function V3Observability() {
  const { t } = useTranslation();
  const { current } = useWorkspace();
  const ws = current?.id ?? "";
  const [params, setParams] = useSearchParams();
  const tab = asTab(params.get("tab"));
  const range = asRange(params.get("range"));
  const view = params.get("view");
  const id = params.get("id");
  const statusParam = params.get("status");
  // bumped by refresh: the active tab refetches with force=true
  const [force, setForce] = useState(0);

  const rangeParam = (r: V2Range): Record<string, string> => (r !== "24h" ? { range: r } : {});
  const go = (next: { tab?: ObsTab; range?: V2Range; status?: string }) => {
    const p: Record<string, string> = { tab: next.tab ?? tab, ...rangeParam(next.range ?? range) };
    if (next.status) p.status = next.status;
    setParams(p);
  };
  const openSession = (sid: string) => setParams({ tab: "sessions", view: "session", id: sid, ...rangeParam(range) });
  const openTrace = (tid: string) => setParams({ tab: "traces", view: "trace", id: tid, ...rangeParam(range) });

  // only the active tab loads, as in V2
  const dash = useLoad(() => (tab === "dashboard" && !view ? api.obsDashboard(range, force > 0) : Promise.resolve(null)), `v3-obs-dash:${ws}:${tab}:${view}:${range}:${force}`);
  const sessions = useLoad(() => (tab === "sessions" && !view ? api.obsSessions(range, force > 0) : Promise.resolve(null)), `v3-obs-sessions:${ws}:${tab}:${view}:${range}:${force}`);
  const traces = useLoad(() => (tab === "traces" && !view ? api.obsTraces(range, force > 0) : Promise.resolve(null)), `v3-obs-traces:${ws}:${tab}:${view}:${range}:${force}`);

  if (view === "trace" && id) {
    return <div className="v3-obs"><TraceView key={`${id}:${range}`} traceId={id} range={range} onBack={() => go({ tab: "traces" })} onOpenSession={openSession} /></div>;
  }
  if (view === "session" && id) {
    return <div className="v3-obs"><SessionView key={`${id}:${range}`} sessionId={id} range={range} onBack={() => go({ tab: "sessions" })} onOpenTrace={openTrace} /></div>;
  }

  const active = tab === "dashboard" ? dash : tab === "sessions" ? sessions : traces;
  const cache = (active.data as { cache?: { age_seconds: number } } | null)?.cache;
  const list = dash.data ? findings(dash.data) : [];
  const title =
    tab !== "dashboard" || !dash.data
      ? t("v3.obs.title")
      : list.length
        ? t("v3.obs.headNeeds", { count: list.length })
        : dash.data.tiles.traces.total || dash.data.tiles.sessions.total
          ? t("v3.obs.headQuiet")
          : t("v3.obs.headNoTraffic");

  return (
    <div className="v3-reveal v3-obs" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={`${t("v3.obs.eyebrow")} · ${t(`v2.range.${range}`)}`}
        title={title}
        sub={t("v3.obs.sub")}
        end={
          <>
            <CacheAge age={cache?.age_seconds} stamp={active.data} loading={active.loading} />
            <Btn kind="ghost" onClick={() => setForce((n) => n + 1)} disabled={active.loading} title={t("v3.obs.refresh")}>
              <RefreshCw size={14} /> {t("v3.obs.refresh")}
            </Btn>
          </>
        }
      />

      <div className="v3-obs-bar">
        <Filters
          value={tab}
          onChange={(next) => go({ tab: next })}
          options={(["dashboard", "sessions", "traces"] as const).map((k) => ({ value: k, label: t(`v3.obs.tab.${k}`) }))}
        />
        <div className="end">
          <Filters value={range} onChange={(r) => go({ range: r })} options={RANGES.map((r) => ({ value: r, label: r.toUpperCase() }))} />
        </div>
      </div>

      {tab === "dashboard" &&
        (dash.data ? (
          <>
            {dash.error && <Notice s="act">{t("obs.loadFailed", { msg: dash.error })}</Notice>}
            <DashboardView
              data={dash.data}
              onOpen={(target: DashTarget) => go({ tab: target.tab, status: target.status })}
              onPricesRefreshed={() => setForce((n) => n + 1)}
            />
          </>
        ) : dash.error ? (
          <Notice s="act">{t("obs.loadFailed", { msg: dash.error })}</Notice>
        ) : (
          <Panel><Skeleton rows={6} /></Panel>
        ))}
      {tab === "sessions" && (
        <SessionsView rows={sessions.data?.sessions ?? null} loading={sessions.loading} error={sessions.error} range={range} onOpen={openSession} />
      )}
      {tab === "traces" && (
        <TracesView
          key={statusParam ?? "all"}
          rows={traces.data?.traces ?? null}
          loading={traces.loading}
          error={traces.error}
          range={range}
          initialStatus={statusParam === "error" || statusParam === "ok" ? statusParam : "all"}
          onOpen={openTrace}
          onOpenSession={openSession}
        />
      )}
    </div>
  );
}
