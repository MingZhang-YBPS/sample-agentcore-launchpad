import "../users.css";

import { Archive, Pencil, Plus, Radar, RefreshCw, ShieldAlert, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { errorMessage } from "../../../lib/api";
import {
  countSeverities,
  emptyProfileForm,
  formToBody,
  groupResults,
  type NetworkProfile,
  networkApi,
  profileProjects,
  profileToForm,
  resultsOf,
} from "../../../lib/api/networkProfiles";
import { useWorkspace } from "../../../workspace/workspace-context";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Confirm, Empty, Notice, PageHead, Panel, Skeleton } from "../../ui";
import { Enforcement } from "./Enforcement";
import { azsOf, STATUS_SIGNAL } from "./model";
import { ProfileDetail } from "./ProfileDetail";
import { ProfileDialog } from "./ProfileDialog";
import { ProjectAccess } from "./ProjectAccess";

const PROBE_POLL_MS = 5000;

/**
 * Configure → Network profiles (projects §12.12; platform administrators): the
 * workspace's VPC profiles agents may run in — list, create / edit / archive, run the
 * preflight and the probe and read their results, the per-project allowed set, and
 * the workspace's network switches with the condition-key policy. Workspace-scoped
 * (`X-Workspace`); `?id=` opens one profile's detail.
 */
export function V3NetworkProfiles() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const { current } = useWorkspace();
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const selectedId = params.get("id") ?? "";
  const [version, setVersion] = useState(0);
  const list = useLoad(async () => (isAdmin ? networkApi.list() : { profiles: [] }), `v3-network:${isAdmin}:${current?.id ?? ""}:${version}`);
  const [editing, setEditing] = useState<NetworkProfile | "new" | null>(null);
  const [archiving, setArchiving] = useState<NetworkProfile | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const profiles = list.data?.profiles ?? [];
  const probing = profiles.some((p) => groupResults(resultsOf(p)).probe?.status === "running");
  const reload = () => setVersion((v) => v + 1);
  const reloadList = list.reload;

  // a background probe lands on the profile: re-read until it has
  useEffect(() => {
    if (!probing) return;
    const timer = window.setInterval(reloadList, PROBE_POLL_MS);
    return () => window.clearInterval(timer);
  }, [probing, reloadList]);

  if (!isAdmin) {
    return (
      <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
        <PageHead eyebrow={t("v3.network.eyebrow")} title={t("v3.network.title")} sub={t("auth.adminRequired.meta")} />
        <Panel>
          <Empty title={<span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}><ShieldAlert size={16} /> {t("auth.adminRequired.title")}</span>}>
            {t("usersPage.forbiddenBody")}
          </Empty>
        </Panel>
      </div>
    );
  }

  const run = async (p: NetworkProfile, call: () => Promise<unknown>, okKey: string) => {
    setBusyId(p.id);
    try {
      await call();
      toast("ok", t(okKey, { name: p.name }));
      reload();
      return true;
    } catch (err) {
      toast("act", errorMessage(err), err);
      return false;
    } finally {
      setBusyId(null);
    }
  };
  const selected = profiles.find((p) => p.id === selectedId) ?? null;

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.network.eyebrow")}
        title={t("v3.network.title")}
        sub={t("v3.network.sub", { workspace: current?.name ?? current?.id ?? "—", region: current?.region ?? "—" })}
        end={
          <>
            <Btn kind="ghost" onClick={reload} disabled={list.loading}><RefreshCw size={14} /> {t("v3.users.refresh")}</Btn>
            <Btn kind="primary" onClick={() => setEditing("new")}><Plus size={14} /> {t("v3.network.create")}</Btn>
          </>
        }
      />
      <Panel flush>
        {list.loading && !list.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
        ) : list.error ? (
          <div style={{ padding: 20 }}><Notice s="act">{list.error}</Notice></div>
        ) : profiles.length === 0 ? (
          <Empty title={t("v3.network.empty")}>{t("v3.network.emptyBody")}</Empty>
        ) : (
          <table className="v3-table" data-testid="v3-network-table">
            <thead>
              <tr>
                <th>{t("v3.network.col.name")}</th>
                <th>VPC</th>
                <th>{t("v3.network.col.subnets")}</th>
                <th>{t("v3.network.col.purpose")}</th>
                <th>{t("v3.network.col.preflight")}</th>
                <th>{t("v3.network.col.users")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {profiles.map((p) => {
                const busy = busyId === p.id;
                const counts = countSeverities(resultsOf(p));
                const azs = azsOf(p);
                const projects = profileProjects(p);
                const probe = groupResults(resultsOf(p)).probe;
                return (
                  <tr key={p.id} aria-selected={p.id === selectedId || undefined}>
                    <td>
                      <button type="button" className="v3-inline-link" style={{ background: "none", border: 0, padding: 0, cursor: "pointer" }}
                        onClick={() => setParams(p.id === selectedId ? {} : { id: p.id })}>
                        <b>{p.name}</b>
                      </button>
                      <div><small className="mono">{p.id}</small> {p.legacy && <Chip s="off">{t("v3.network.legacy")}</Chip>}</div>
                    </td>
                    <td className="mono">{p.vpc_id || "—"}</td>
                    <td>{p.subnet_ids.length}{azs.length ? ` · ${azs.join(", ")}` : ""}</td>
                    <td>{t(`v3.network.purpose.${p.purpose}`)}</td>
                    <td>
                      <Chip s={STATUS_SIGNAL[p.status] ?? "off"}>{t(`v3.network.status.${p.status}`, { defaultValue: p.status })}</Chip>
                      {counts.block + counts.warn > 0 && <small> {t("v3.network.counts", { block: counts.block, warn: counts.warn })}</small>}
                      {probe?.status === "running" && <small> · {t("v3.network.probeRunning")}</small>}
                    </td>
                    <td>
                      {t("v3.network.usage", { projects: projects.length, agents: p.agents ?? 0 })}
                      {projects.length > 1 && <> <Chip s="wait">{t("v3.network.sharedChip")}</Chip></>}
                    </td>
                    <td style={{ width: 1, whiteSpace: "nowrap" }}>
                      <div style={{ display: "flex", gap: 4, justifyContent: "flex-end" }}>
                        <Btn size="sm" kind="ghost" disabled={busy} onClick={() => void run(p, () => networkApi.check(p.id), "v3.network.checked")}>
                          <ShieldCheck size={13} /> {t("v3.network.check")}
                        </Btn>
                        <Btn size="sm" kind="ghost" disabled={busy || p.reachable.length === 0 || p.status === "blocked" || probe?.status === "running"}
                          title={p.reachable.length === 0 ? t("v3.network.probeNeedsTargets") : t("v3.network.probeHint")}
                          onClick={() => void run(p, () => networkApi.probe(p.id), "v3.network.probeStarted")}>
                          <Radar size={13} /> {t("v3.network.probe")}
                        </Btn>
                        <Btn size="sm" kind="ghost" disabled={busy} onClick={() => setEditing(p)}><Pencil size={13} /> {t("v3.groups.edit")}</Btn>
                        <Btn size="sm" kind="ghost" disabled={busy || (p.agents ?? 0) > 0}
                          title={(p.agents ?? 0) > 0 ? t("v3.network.inUse", { count: p.agents ?? 0 }) : t("v3.groups.archive")}
                          onClick={() => setArchiving(p)}>
                          <Archive size={13} />
                        </Btn>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>

      {selected && <ProfileDetail profile={selected} />}
      <ProjectAccess profiles={profiles} onSaved={reload} />
      <Enforcement version={version} />

      {editing && (
        <ProfileDialog
          creating={editing === "new"}
          initial={editing === "new" ? emptyProfileForm() : profileToForm(editing)}
          onClose={() => setEditing(null)}
          onSubmit={async (form) => {
            const body = formToBody(form);
            if (editing === "new") await networkApi.create(body);
            else await networkApi.update(editing.id, body);
            setEditing(null);
            toast("ok", t(editing === "new" ? "v3.network.created" : "v3.network.saved", { name: body.name }));
            reload();
          }} />
      )}
      {archiving && (
        <Confirm
          title={t("v3.network.archiveTitle", { name: archiving.name })}
          confirmLabel={t("v3.groups.archive")}
          cancelLabel={t("v3.common.cancel")}
          danger
          busy={busyId !== null}
          onCancel={() => setArchiving(null)}
          onConfirm={() => {
            const p = archiving;
            void run(p, () => networkApi.archive(p.id), "v3.network.archived").then((ok) => ok && setArchiving(null));
          }}
        >
          {t("v3.network.archiveBody", { name: archiving.name })}
        </Confirm>
      )}
    </div>
  );
}
