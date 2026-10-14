import { Archive, ArchiveRestore, ArrowLeft, ArrowRightLeft, Lock, RefreshCw } from "lucide-react";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { errorMessage } from "../../../lib/api";
import { type ProjectDetail, projectsApi } from "../../../lib/api/projects";
import { useProject } from "../../../workspace/project-context";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Confirm, Filters, Notice, PageHead, Panel, Skeleton } from "../../ui";
import { AuditTab } from "./AuditTab";
import { MembersTab } from "./MembersTab";
import { type ProjectTab, visibleProjectTabs } from "./model";
import { OverviewTab } from "./OverviewTab";
import { RolesTab } from "./RolesTab";
import { SettingsTab } from "./SettingsTab";
import { TransferDialog } from "./TransferDialog";
import { WorkspacesTab } from "./WorkspacesTab";

/** What the caller may do on this project's pages. */
export interface ProjectAbilities {
  /** a platform administrator: transfer, lifecycle, workspace defaults */
  admin: boolean;
  /** structure, bindings (open workspaces), archive, `sod_strict`: an administrator,
   *  or an administrator of the project's department (`can_manage`, §11.1) */
  structure: boolean;
  /** `project.members` here (or `structure`): members, custom roles, owner settings */
  manage: boolean;
}

export function ProjectDetailPage({ projectId, tab: requested }: { projectId: string; tab: ProjectTab }) {
  const { t } = useTranslation();
  const toast = useToast();
  const { isAdmin } = useAuth();
  const { refresh: refreshAccess } = useProject();
  const [params, setParams] = useSearchParams();
  const [tick, setTick] = useState(0);
  const loaded = useLoad(() => projectsApi.get(projectId), `v3-project:${projectId}:${tick}`);
  const [confirm, setConfirm] = useState<"archive" | "unarchive" | null>(null);
  const [transferring, setTransferring] = useState(false);
  const [busy, setBusy] = useState(false);
  // a reload after an edit keeps the page (and the open tab's state) on screen
  const last = useRef<ProjectDetail | null>(null);
  if (loaded.data) last.current = loaded.data;
  const project = loaded.data ?? (loaded.error ? null : last.current);
  const reload = () => setTick((n) => n + 1);
  const pick = (next: ProjectTab) => {
    const q = new URLSearchParams(params);
    if (next === "overview") q.delete("tab");
    else q.set("tab", next);
    setParams(q);
  };
  const back = () => setParams({});

  if (loaded.loading && !project) return <Panel><Skeleton rows={5} /></Panel>;
  if (!project) {
    return (
      <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
        <PageHead eyebrow={t("v3.projects.eyebrow")} title={projectId}
          end={<Btn kind="ghost" onClick={back}><ArrowLeft size={14} /> {t("v3.projects.back")}</Btn>} />
        <Notice s="act"><ApiErrorDetail error={loaded.failure ?? loaded.error} /></Notice>
      </div>
    );
  }
  const structure = isAdmin || project.can_manage === true;
  const abilities: ProjectAbilities = {
    admin: isAdmin,
    structure,
    manage: structure || project.my_permissions.includes("project.members"),
  };
  // §11.6: the audit tab needs `project.audit` here (or an administrator)
  const canAudit = isAdmin || project.my_permissions.includes("project.audit");
  const tabs = visibleProjectTabs(canAudit);
  const tab: ProjectTab = tabs.includes(requested) ? requested : "overview";
  const archived = project.status === "archived";

  const changeStatus = async (action: "archive" | "unarchive") => {
    setBusy(true);
    try {
      if (action === "archive") {
        const result = await projectsApi.archive(project.id);
        toast("ok", t("v3.projects.archive.done", {
          name: project.name, links: result.links_revoked, keys: result.keys_disabled,
        }));
      } else {
        await projectsApi.unarchive(project.id);
        toast("ok", t("v3.projects.archive.unarchived", { name: project.name }));
      }
      reload();
      await refreshAccess();
    } catch (err) {
      toast("act", errorMessage(err), err);
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };
  /** a change to members, roles, bindings or settings moves the counts and the caller's access too */
  const changed = () => {
    reload();
    void refreshAccess();
  };

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.projects.eyebrow")}
        title={
          <span style={{ display: "inline-flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
            {project.name}
            {project.is_default && <Chip title={t("v3.projects.defaultLocked")}><Lock size={11} /> {t("v3.projects.defaultChip")}</Chip>}
            <Chip s={archived ? undefined : "ok"}>{t(`v3.projects.status.${project.status}`)}</Chip>
          </span>
        }
        sub={<span className="mono">{project.id}</span>}
        end={
          <>
            <Btn kind="ghost" onClick={back}><ArrowLeft size={14} /> {t("v3.projects.back")}</Btn>
            <Btn kind="ghost" onClick={reload} title={t("v3.projects.reload")}><RefreshCw size={14} /></Btn>
            {abilities.admin && !archived && (
              <Btn onClick={() => setTransferring(true)}><ArrowRightLeft size={14} /> {t("v3.projects.transfer.action")}</Btn>
            )}
            {abilities.structure && !project.is_default && (
              archived ? (
                <Btn onClick={() => setConfirm("unarchive")} disabled={busy}><ArchiveRestore size={14} /> {t("v3.projects.archive.unarchive")}</Btn>
              ) : (
                <Btn kind="danger" onClick={() => setConfirm("archive")} disabled={busy}><Archive size={14} /> {t("v3.projects.archive.action")}</Btn>
              )
            )}
          </>
        }
      />
      {archived && <Notice s="wait">{t("v3.projects.archive.notice")}</Notice>}

      <Filters
        value={tab}
        onChange={pick}
        options={tabs.map((key) => ({ value: key, label: t(`v3.projects.tab.${key}`) }))}
      />

      {tab === "overview" && <OverviewTab project={project} />}
      {tab === "members" && <MembersTab project={project} abilities={abilities} onChanged={changed} />}
      {tab === "roles" && <RolesTab project={project} abilities={abilities} onChanged={changed} />}
      {tab === "workspaces" && <WorkspacesTab project={project} abilities={abilities} onChanged={changed} />}
      {tab === "settings" && <SettingsTab project={project} abilities={abilities} onChanged={changed} />}
      {tab === "audit" && <AuditTab project={project} />}

      {confirm && (
        <Confirm
          title={t(confirm === "archive" ? "v3.projects.archive.title" : "v3.projects.archive.unarchiveTitle", { name: project.name })}
          confirmLabel={t(confirm === "archive" ? "v3.projects.archive.action" : "v3.projects.archive.unarchive")}
          cancelLabel={t("v3.projects.cancel")}
          danger={confirm === "archive"}
          busy={busy}
          onConfirm={() => void changeStatus(confirm)}
          onCancel={() => setConfirm(null)}
        >
          {t(confirm === "archive" ? "v3.projects.archive.body" : "v3.projects.archive.unarchiveBody", { name: project.name })}
        </Confirm>
      )}
      {transferring && (
        <TransferDialog
          project={project}
          onClose={() => setTransferring(false)}
          onDone={() => {
            setTransferring(false);
            reload();
          }}
        />
      )}
    </div>
  );
}
