import { Download, RefreshCw } from "lucide-react";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { errorMessage } from "../../../lib/api";
import {
  AUDIT_OUTCOMES,
  type AuditExportFormat,
  type AuditOutcome,
  type ProjectAuditEvent,
  type ProjectDetail,
  projectAuditApi,
} from "../../../lib/api/projects";
import { ago } from "../../format";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Empty, Notice, Panel, Skeleton } from "../../ui";
import { AUDIT_RANGES, type AuditRange, auditFilter, outcomeSignal, rangeFrom } from "./auditModel";

const PAGE = 50;

function save(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}

/**
 * §11.6 "Audit": the project's journal (its own rows in every bound workspace and
 * the structure changes naming it), newest first — filters by actor, action, time
 * and outcome, keyset "load more", CSV / JSONL export of the same filter. Shown to
 * holders of `project.audit` and administrators; the API decides either way.
 */
export function AuditTab({ project }: { project: ProjectDetail }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [actor, setActor] = useState("");
  const [action, setAction] = useState("");
  const [outcome, setOutcome] = useState<AuditOutcome | "">("");
  const [range, setRange] = useState<AuditRange>("30d");
  // fixed when the range is picked, so a re-render does not move the window (or the load key)
  const [from, setFrom] = useState<string | undefined>(() => rangeFrom("30d"));
  const [tick, setTick] = useState(0);
  const filter = auditFilter({ actor, action, outcome, from });
  const key = `v3-project-audit:${project.id}:${JSON.stringify(filter)}:${tick}`;
  const first = useLoad(() => projectAuditApi.list(project.id, filter, { limit: PAGE }), key);
  // the pages after the first, for the filter they were loaded under
  const [more, setMore] = useState<{ key: string; events: ProjectAuditEvent[]; cursor: string | null } | null>(null);
  const [busy, setBusy] = useState<"more" | AuditExportFormat | null>(null);
  const extra = more?.key === key ? more : null;
  const events = [...(first.data?.events ?? []), ...(extra?.events ?? [])];
  const cursor = extra ? extra.cursor : first.data?.next_cursor ?? null;
  // the filter lists (the whole project's, whatever the filter), kept across reloads
  const lastFacets = useRef<{ actors: string[]; actions: string[] }>({ actors: [], actions: [] });
  if (first.data?.facets) lastFacets.current = first.data.facets;
  const facets = lastFacets.current;

  const pickRange = (next: AuditRange) => {
    setRange(next);
    setFrom(rangeFrom(next));
  };
  const loadMore = async () => {
    if (!cursor) return;
    setBusy("more");
    try {
      const page = await projectAuditApi.list(project.id, filter, { limit: PAGE, cursor });
      setMore({ key, events: [...(extra?.events ?? []), ...page.events], cursor: page.next_cursor });
    } catch (err) {
      toast("act", errorMessage(err), err);
    } finally {
      setBusy(null);
    }
  };
  const download = async (format: AuditExportFormat) => {
    setBusy(format);
    try {
      const file = await projectAuditApi.export(project.id, filter, format);
      save(file.blob, file.name);
      setTick((n) => n + 1); // the download is journaled: show it
    } catch (err) {
      toast("act", errorMessage(err), err);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div style={{ display: "grid", gap: 12 }} data-testid="project-audit">
      <div className="v3-prj-filters">
        <select className="v3-input" aria-label={t("v3.projects.audit.actor")} value={actor} onChange={(e) => setActor(e.target.value)}>
          <option value="">{t("v3.projects.audit.allActors")}</option>
          {facets.actors.map((a) => <option key={a} value={a}>{a}</option>)}
        </select>
        <select className="v3-input" aria-label={t("v3.projects.audit.action")} value={action} onChange={(e) => setAction(e.target.value)}>
          <option value="">{t("v3.projects.audit.allActions")}</option>
          {facets.actions.map((a) => <option key={a} value={a}>{a}</option>)}
        </select>
        <select className="v3-input" aria-label={t("v3.projects.audit.range")} value={range} onChange={(e) => pickRange(e.target.value as AuditRange)}>
          {AUDIT_RANGES.map((r) => <option key={r} value={r}>{t(`v3.projects.audit.ranges.${r}`)}</option>)}
        </select>
        <select className="v3-input" aria-label={t("v3.projects.audit.outcome")} value={outcome} onChange={(e) => setOutcome(e.target.value as AuditOutcome | "")}>
          <option value="">{t("v3.projects.audit.allOutcomes")}</option>
          {AUDIT_OUTCOMES.map((o) => <option key={o} value={o}>{t(`v3.projects.audit.outcomes.${o}`)}</option>)}
        </select>
        <span style={{ marginLeft: "auto", display: "inline-flex", gap: 6 }}>
          <Btn kind="ghost" onClick={() => setTick((n) => n + 1)} title={t("v3.projects.reload")}><RefreshCw size={14} /></Btn>
          <Btn onClick={() => void download("csv")} disabled={busy !== null}><Download size={14} /> {t("v3.projects.audit.exportCsv")}</Btn>
          <Btn kind="ghost" onClick={() => void download("jsonl")} disabled={busy !== null}>{t("v3.projects.audit.exportJsonl")}</Btn>
        </span>
      </div>
      <Panel flush>
        {first.loading && !first.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={5} /></div>
        ) : first.error ? (
          <div style={{ padding: 20 }}><Notice s="act"><ApiErrorDetail error={first.failure ?? first.error} /></Notice></div>
        ) : events.length === 0 ? (
          <Empty title={t("v3.projects.audit.empty")} />
        ) : (
          <table className="v3-table" data-testid="project-audit-events">
            <thead>
              <tr>
                <th>{t("v3.projects.audit.col.at")}</th>
                <th>{t("v3.projects.audit.col.actor")}</th>
                <th>{t("v3.projects.audit.col.action")}</th>
                <th>{t("v3.projects.audit.col.target")}</th>
                <th>{t("v3.projects.audit.col.outcome")}</th>
              </tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <tr key={e.id}>
                  <td className="mono" title={e.at ?? undefined} style={{ whiteSpace: "nowrap" }}>
                    {e.at ? new Date(e.at).toLocaleString() : "—"}
                    <small className="v3-prj-muted"> · {ago(e.at)}</small>
                  </td>
                  <td>{e.actor}</td>
                  <td className="mono">{e.action}</td>
                  <td className="mono v3-prj-muted" style={{ wordBreak: "break-all" }}>
                    {e.target || "—"}
                    {e.workspace_id && e.project_id && <small> · {e.workspace_id}</small>}
                  </td>
                  <td><Chip s={outcomeSignal(e.outcome)}>{t(`v3.projects.audit.outcomes.${e.outcome}`)}</Chip></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
      {cursor && (
        <div>
          <Btn onClick={() => void loadMore()} disabled={busy !== null}>{t("v3.projects.audit.more")}</Btn>
        </div>
      )}
      <small className="v3-hint">{t("v3.projects.audit.hint", { project: project.name })}</small>
    </div>
  );
}
