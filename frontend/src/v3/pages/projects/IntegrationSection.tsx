import { Copy, Download } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import {
  invokeAccessApi,
  type InvokeQualifier,
  policyFileName,
  policyText,
  saveBlob,
} from "../../../lib/api/invokeAccess";
import type { ProjectDetail } from "../../../lib/api/projects";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Filters, Lamp, Notice, Panel, Skeleton } from "../../ui";
import { Copyable } from "../agent/Copyable";
import "../agent/invokeAccess.css";

type Scope = "all" | InvokeQualifier;

/**
 * 项目设置 → 集成 (invoke-access §0.5): every callable agent of the project with its
 * ARN and endpoints (CSV / JSON export), and ONE merged least-privilege policy over
 * the agents ticked, so a single backend role can call several agents. JWT agents
 * are listed but take no IAM grant. All of it is the backend's answer.
 */
export function IntegrationSection({ project }: { project: ProjectDetail }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [scope, setScope] = useState<Scope>("all");
  const [excluded, setExcluded] = useState<string[]>([]);
  const inventory = useLoad(() => invokeAccessApi.project(project.id), `v3-ia-project:${project.id}`);
  const agents = useMemo(() => {
    const seen = new Map<string, { id: string; name: string; mode: string }>();
    for (const row of inventory.data?.inventory ?? []) if (!seen.has(row.agent_id)) seen.set(row.agent_id, { id: row.agent_id, name: row.name, mode: row.mode });
    return [...seen.values()];
  }, [inventory.data]);
  const chosen = agents.filter((a) => !excluded.includes(a.id)).map((a) => a.id);
  const qualifier = scope === "all" ? null : scope;
  const policyKey = `v3-ia-project-policy:${project.id}:${scope}:${chosen.join(",")}`;
  const merged = useLoad(
    () => (chosen.length ? invokeAccessApi.project(project.id, { agents: chosen, qualifier }) : Promise.resolve(null)),
    policyKey,
  );

  const exportAs = async (format: "csv" | "json") => {
    try {
      const { blob, name } = await invokeAccessApi.export(project.id, format);
      saveBlob(blob, name);
    } catch (err) {
      toast("act", t("v3.invokeAccess.project.exportFailed"), err);
    }
  };
  const copy = (text: string) =>
    navigator.clipboard
      .writeText(text)
      .then(() => toast("ok", t("v3.invokeAccess.copied", { label: t("v3.invokeAccess.policy") })))
      .catch(() => toast("act", t("v3.invokeAccess.copyFailed")));
  const toggle = (id: string) => setExcluded((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  const policy = merged.data?.policy ?? null;

  return (
    <Panel
      title={t("v3.invokeAccess.project.title")}
      end={
        <span style={{ display: "flex", gap: 6 }}>
          <Btn size="sm" kind="ghost" onClick={() => void exportAs("csv")} disabled={!inventory.data}><Download size={13} /> CSV</Btn>
          <Btn size="sm" kind="ghost" onClick={() => void exportAs("json")} disabled={!inventory.data}><Download size={13} /> JSON</Btn>
        </span>
      }
    >
      <div style={{ display: "grid", gap: 14 }} data-testid="project-integration">
        <span className="v3-hint">{t("v3.invokeAccess.project.sub")}</span>
        {inventory.loading && !inventory.data && <Skeleton rows={3} />}
        {inventory.failure != null && <Notice s="act"><ApiErrorDetail error={inventory.failure} /></Notice>}
        {inventory.data && inventory.data.inventory.length === 0 && <span className="v3-hint">{t("v3.invokeAccess.project.none")}</span>}
        {inventory.data && inventory.data.inventory.length > 0 && (
          <div className="v3-table-scroll" tabIndex={0}>
            <table className="v3-table v3-ia-inv">
              <thead>
                <tr>
                  <th style={{ width: 32 }} aria-label={t("v3.invokeAccess.project.include")} />
                  <th>{t("v3.invokeAccess.project.agent")}</th>
                  <th>{t("v3.invokeAccess.project.endpoint")}</th>
                  <th>{t("v3.invokeAccess.project.mode")}</th>
                  <th>{t("v3.invokeAccess.project.arn")}</th>
                  <th>{t("v3.invokeAccess.region")}</th>
                </tr>
              </thead>
              <tbody>
                {inventory.data.inventory.map((row, i, all) => {
                  const first = i === 0 || all[i - 1].agent_id !== row.agent_id;
                  const off = excluded.includes(row.agent_id);
                  return (
                    <tr key={`${row.agent_id}:${row.qualifier}`} data-off={off}>
                      <td>
                        {first && (
                          <input type="checkbox" checked={!off} onChange={() => toggle(row.agent_id)}
                            aria-label={t("v3.invokeAccess.project.includeAgent", { name: row.name })} />
                        )}
                      </td>
                      <td>{first ? <b>{row.name}</b> : null}</td>
                      <td>
                        <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                          <Lamp s={row.production ? "ok" : "off"} />
                          <span className="mono">{row.qualifier}</span>
                        </span>
                      </td>
                      <td>{first ? <Chip s={row.mode === "jwt" ? "wait" : "info"}>{t(`v3.invokeAccess.mode.${row.mode}`)}</Chip> : null}</td>
                      <td className="arn"><Copyable value={row.endpoint_arn ?? row.arn} label="ARN" /></td>
                      <td className="mono">{row.region}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {(inventory.data?.skipped.length ?? 0) > 0 && (
          <span className="v3-hint">
            {t("v3.invokeAccess.project.skipped", { names: inventory.data!.skipped.map((s) => s.name).join(", ") })}
          </span>
        )}

        {agents.length > 0 && (
          <>
            <div className="v3-ia-tools">
              <span className="v3-ia-label" style={{ minHeight: 0 }}>{t("v3.invokeAccess.project.merged")}</span>
              <span className="grow" />
              <Filters value={scope} onChange={setScope} options={[
                { value: "all" as Scope, label: t("v3.invokeAccess.allEndpoints") },
                { value: "live" as Scope, label: t("v3.invokeAccess.project.liveOnly") },
              ]} />
            </div>
            {merged.failure != null && <Notice s="act"><ApiErrorDetail error={merged.failure} /></Notice>}
            {chosen.length === 0 && <span className="v3-hint">{t("v3.invokeAccess.project.pick")}</span>}
            {chosen.length > 0 && merged.loading && !merged.data && <Skeleton rows={2} />}
            {merged.data && !policy && <span className="v3-hint">{t("v3.invokeAccess.project.onlyJwt")}</span>}
            {policy && (
              <div className="v3-ia-code v3-ia-fresh" key={policyKey}>
                <header>
                  <span>{policyFileName(project.id, qualifier)}</span>
                  <span className="end">
                    <Btn size="sm" kind="ghost" onClick={() => void copy(policyText(policy))}><Copy size={13} /> {t("v3.invokeAccess.copy")}</Btn>
                    <Btn size="sm" kind="ghost"
                      onClick={() => saveBlob(new Blob([policyText(policy)], { type: "application/json" }), policyFileName(project.id, qualifier))}>
                      <Download size={13} /> {t("v3.invokeAccess.download")}
                    </Btn>
                  </span>
                </header>
                <pre className="v3-pre" data-testid="project-integration-policy">{policyText(policy)}</pre>
              </div>
            )}
            {(merged.data?.jwt_agents.length ?? 0) > 0 && (
              <span className="v3-hint">{t("v3.invokeAccess.project.jwtAgents", { count: merged.data!.jwt_agents.length })}</span>
            )}
            <span className="v3-hint">{t("v3.invokeAccess.note.cross_account")}</span>
          </>
        )}
      </div>
    </Panel>
  );
}
