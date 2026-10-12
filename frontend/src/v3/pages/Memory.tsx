import "./memory.css";

import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, type MemoryActor } from "../../lib/api";
import { useTokenPaged } from "../../v2/pages/memory/common";
import { useWorkspace } from "../../workspace/workspace-context";
import { useLoad } from "../hooks";
import { Btn, Filters, Notice, PageHead, Panel, Skeleton } from "../ui";
import { LongTerm } from "./memory/LongTerm";
import { Overview } from "./memory/Overview";
import { Resources } from "./memory/Resources";
import { ShortTerm } from "./memory/ShortTerm";

type Tab = "overview" | "short-term" | "long-term" | "resources";
const TABS: Tab[] = ["overview", "short-term", "long-term", "resources"];

/**
 * Memory — the platform memory resource and its strategies, short-term events
 * (actor → session → event), long-term records (listing + semantic retrieval)
 * and memory resources. Same URL contract as V2: `?tab=` plus the selection in
 * `?actor=` / `?session=` / `?strategy=`; a resource's detail and editor
 * (`?view=resource…`) stay the hosted V2 sub-pages.
 */
export function V3Memory() {
  const { t } = useTranslation();
  const { current } = useWorkspace();
  const ws = current?.id ?? "";
  const [params, setParams] = useSearchParams();
  const tabParam = params.get("tab") as Tab | null;
  const tab: Tab = tabParam && TABS.includes(tabParam) ? tabParam : "overview";
  const actor = params.get("actor");
  const session = params.get("session");
  const strategy = params.get("strategy");

  // fetched once for every tab: all need `configured`, long-term needs the strategies
  const overview = useLoad(() => api.memoryOverview(), `v3-mem-overview:${ws}`);
  const needsActors = tab === "short-term" || tab === "long-term";
  const actors = useTokenPaged<MemoryActor>(needsActors ? (token) => api.memoryActors(token) : null, `v3-actors:${ws}:${needsActors}`);

  const update = (patch: Record<string, string | null>) =>
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      for (const [k, v] of Object.entries(patch)) {
        if (v) next.set(k, v);
        else next.delete(k);
      }
      return next;
    });

  const data = overview.data;
  let body;
  if (tab === "resources") body = <Resources />;
  else if (!data) body = overview.error ? <Notice s="act">{t("v3.memory.loadFailed", { msg: overview.error })}</Notice> : <Panel><Skeleton rows={5} /></Panel>;
  // before `make bootstrap` there is no platform memory to browse; resources still work
  else if (!data.configured)
    body = (
      <Panel title={t("v3.memory.notConfiguredTitle")} signal="wait">
        <p style={{ margin: "0 0 12px", color: "var(--v3-text-2)" }}>{t("v3.memory.notConfiguredBody")}</p>
        <pre className="v3-pre">make bootstrap</pre>
        <div style={{ marginTop: 12 }}><Btn onClick={overview.reload}>{t("v3.memory.refresh")}</Btn></div>
      </Panel>
    );
  else if (tab === "overview") body = <Overview overview={data} onReload={overview.reload} />;
  else if (tab === "short-term")
    body = (
      <ShortTerm actors={actors} actorId={actor} sessionId={session}
        // a session belongs to one actor: picking an actor clears it
        onSelectActor={(next) => update({ actor: next, session: null })}
        onSelectSession={(next) => update({ session: next })} />
    );
  else
    body = (
      <LongTerm actors={actors} strategies={data.strategies} actorId={actor} strategyId={strategy}
        onSelectActor={(next) => update({ actor: next, session: null })}
        onSelectStrategy={(next) => update({ strategy: next })} />
    );

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead eyebrow={t("v3.memory.eyebrow")} title={t("v3.memory.title")} sub={t("v3.memory.sub")} />
      <Filters
        value={tab}
        // actor / session / strategy survive a tab switch (short- and long-term share the actor)
        onChange={(next) => update({ tab: next === "overview" ? null : next })}
        options={TABS.map((value) => ({ value, label: t(`v3.memory.tab.${value}`) }))}
      />
      {body}
    </div>
  );
}
