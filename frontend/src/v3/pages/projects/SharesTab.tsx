import "./sharesTab.css";

import { BookOpen, Check, Gauge, ListChecks, Plug, Plus, Ruler, Share2, X } from "lucide-react";
import { type CSSProperties, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { errorMessage } from "../../../lib/api";
import {
  type ProjectDetail,
  projectSharesApi,
  type ResourceShare,
  SHARE_KINDS,
  type ShareAccess,
  type ShareKind,
  type ShareOptions,
} from "../../../lib/api/projects";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Confirm, Dialog, Lamp, Notice, Panel, Skeleton } from "../../ui";
import type { ProjectAbilities } from "./ProjectDetail";
import {
  daysLeft,
  draftPayload,
  draftProblems,
  lifeLeft,
  lifeSignal,
  rowActions,
  type ShareDraft,
  statusSignal,
  typedKind,
} from "./sharesModel";

const KIND_ICON: Record<ShareKind, typeof BookOpen> = {
  knowledge_base: BookOpen,
  evaluator: Gauge,
  eval_dataset: ListChecks,
  criteria_set: Ruler,
  gateway_target: Plug,
};

/**
 * §11.2 "Shares": what this project lends to other projects of its workspaces and
 * what it receives. Every member reads both ledgers; `project.share` holders grant
 * and revoke on the lending side; the project's department administrator (or an
 * administrator) approves a cross-department request on the receiving side. A share
 * is never a write and never passes on.
 */
export function SharesTab({
  project,
  abilities,
  onChanged,
}: {
  project: ProjectDetail;
  abilities: ProjectAbilities;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const { isAdmin } = useAuth();
  const [tick, setTick] = useState(0);
  const [closed, setClosed] = useState(false);
  const loaded = useLoad(
    () => projectSharesApi.list(project.id, { include_closed: closed }),
    `v3-project-shares:${project.id}:${closed}:${tick}`,
  );
  const [granting, setGranting] = useState(false);
  const [revoking, setRevoking] = useState<ResourceShare | null>(null);
  const [busy, setBusy] = useState(false);
  const active = project.status === "active";
  const can = {
    share: active && (isAdmin || project.my_permissions.includes("project.share")),
    approve: active && abilities.structure,
  };
  const reload = () => {
    setTick((n) => n + 1);
    onChanged();
  };

  const act = async (fn: () => Promise<ResourceShare>, done: (row: ResourceShare) => string) => {
    setBusy(true);
    try {
      const row = await fn();
      toast("ok", done(row));
      reload();
    } catch (err) {
      toast("act", errorMessage(err), err);
    } finally {
      setBusy(false);
      setRevoking(null);
    }
  };

  const view = loaded.data;
  return (
    <div className="v3-shr" data-testid="project-shares">
      <Notice s="info">{t("v3.projects.shares.intro")}</Notice>
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        {can.share && (
          <Btn kind="primary" onClick={() => setGranting(true)} disabled={project.workspaces.length === 0}>
            <Plus size={14} /> {t("v3.projects.shares.grant")}
          </Btn>
        )}
        <label style={{ display: "inline-flex", gap: 6, alignItems: "center", fontSize: 13, marginLeft: "auto" }}>
          <input type="checkbox" checked={closed} onChange={(e) => setClosed(e.target.checked)} />
          {t("v3.projects.shares.showClosed")}
        </label>
      </div>
      {loaded.loading && !view && <Panel><Skeleton rows={4} /></Panel>}
      {!view && !loaded.loading && <Notice s="act"><ApiErrorDetail error={loaded.failure ?? loaded.error} /></Notice>}
      {view && (
        <div className="v3-shr-ledgers">
          <Ledger side="outgoing" rows={view.outgoing ?? []} projectId={project.id} can={can} busy={busy}
            onRevoke={setRevoking} onDecide={() => undefined} />
          <Ledger side="incoming" rows={view.incoming ?? []} projectId={project.id} can={can} busy={busy}
            onRevoke={() => undefined}
            onDecide={(row, approve) => void act(
              () => (approve ? projectSharesApi.approve : projectSharesApi.reject)(project.id, row.id),
              (r) => t(approve ? "v3.projects.shares.approved" : "v3.projects.shares.rejected", { name: r.resource_name }),
            )} />
        </div>
      )}
      {granting && (
        <GrantDialog project={project} onClose={() => setGranting(false)}
          onGranted={(row) => {
            setGranting(false);
            toast("ok", t(row.status === "requested" ? "v3.projects.shares.requested" : "v3.projects.shares.granted", {
              name: row.resource_name, project: row.grantee_project_name,
            }));
            reload();
          }} />
      )}
      {revoking && (
        <Confirm
          title={t("v3.projects.shares.revokeTitle", { name: revoking.resource_name })}
          confirmLabel={t("v3.projects.shares.revoke")}
          cancelLabel={t("v3.projects.shares.cancel")}
          danger
          busy={busy}
          onCancel={() => setRevoking(null)}
          onConfirm={() => void act(
            () => projectSharesApi.revoke(project.id, revoking.id),
            (row) => (row.pinned_by?.length
              ? t("v3.projects.shares.revokedPinned", { name: row.resource_name, agents: row.pinned_by.join(", ") })
              : t("v3.projects.shares.revoked", { name: row.resource_name })),
          )}
        >
          {t("v3.projects.shares.revokeBody", { project: revoking.grantee_project_name })}
        </Confirm>
      )}
    </div>
  );
}

function Ledger({
  side,
  rows,
  projectId,
  can,
  busy,
  onRevoke,
  onDecide,
}: {
  side: "outgoing" | "incoming";
  rows: ResourceShare[];
  projectId: string;
  can: { share: boolean; approve: boolean };
  busy: boolean;
  onRevoke: (row: ResourceShare) => void;
  onDecide: (row: ResourceShare, approve: boolean) => void;
}) {
  const { t } = useTranslation();
  return (
    <Panel
      title={
        <span className="v3-shr-head">
          <span className="dir">{t(`v3.projects.shares.dir.${side}`)}</span>
          {t(`v3.projects.shares.${side}`)}
          <span className="count">{rows.length}</span>
        </span>
      }
    >
      {rows.length === 0 ? (
        <div className="v3-shr-empty">{t(`v3.projects.shares.empty.${side}`)}</div>
      ) : (
        <ul className="v3-shr-list" data-testid={`project-shares-${side}`}>
          {rows.map((row, i) => (
            <ShareRow key={row.id} row={row} index={i} projectId={projectId} busy={busy}
              actions={rowActions(row, side, can)} onRevoke={() => onRevoke(row)}
              onDecide={(approve) => onDecide(row, approve)} />
          ))}
        </ul>
      )}
    </Panel>
  );
}

function ShareRow({
  row,
  index,
  projectId,
  busy,
  actions,
  onRevoke,
  onDecide,
}: {
  row: ResourceShare;
  index: number;
  projectId: string;
  busy: boolean;
  actions: { revoke: boolean; decide: boolean };
  onRevoke: () => void;
  onDecide: (approve: boolean) => void;
}) {
  const { t } = useTranslation();
  const Icon = KIND_ICON[row.kind] ?? Share2;
  const days = daysLeft(row.expires_at);
  const life = row.status === "active" ? lifeLeft(row) : 0;
  const style = { "--i": Math.min(index, 8) } as CSSProperties;
  return (
    <li className="v3-shr-row" data-status={row.status} data-access={row.access} style={style}
      data-testid={`project-share-${row.id}`}>
      <span className="v3-shr-glyph" title={t(`v3.projects.shares.kind.${row.kind}`)}><Icon size={16} /></span>
      <div style={{ minWidth: 0 }}>
        <div className="v3-shr-name">{row.resource_name}</div>
        <div className="v3-shr-id">
          {t(`v3.projects.shares.kind.${row.kind}`)} · {row.resource_id} · {row.workspace_id}
        </div>
      </div>
      <span className="v3-shr-acts">
        {row.status !== "active" && (
          <Chip s={statusSignal(row.status)}>{t(`v3.projects.shares.status.${row.status}`)}</Chip>
        )}
        {actions.decide && (
          <>
            <Btn size="sm" kind="primary" disabled={busy} onClick={() => onDecide(true)}>
              <Check size={13} /> {t("v3.projects.shares.approve")}
            </Btn>
            <Btn size="sm" kind="ghost" disabled={busy} onClick={() => onDecide(false)}>
              <X size={13} /> {t("v3.projects.shares.reject")}
            </Btn>
          </>
        )}
        {actions.revoke && (
          <Btn size="sm" kind="danger" disabled={busy} onClick={onRevoke}>{t("v3.projects.shares.revoke")}</Btn>
        )}
      </span>
      <div className="v3-shr-flow" aria-label={t("v3.projects.shares.flowLabel", {
        owner: row.owner_project_name, grantee: row.grantee_project_name, access: t(`v3.projects.shares.access.${row.access}`),
      })}>
        <span className="end" data-self={row.owner_project_id === projectId}>{row.owner_project_name}</span>
        <span className="wire" aria-hidden="true" />
        <Chip s={row.access === "use" ? "ok" : "info"}>{t(`v3.projects.shares.access.${row.access}`)}</Chip>
        <span className="wire" aria-hidden="true" />
        <span className="end" data-self={row.grantee_project_id === projectId}>{row.grantee_project_name}</span>
      </div>
      <div className="v3-shr-life">
        <span className="rail" aria-hidden="true"><i data-s={lifeSignal(row)} style={{ width: `${Math.round(life * 100)}%` }} /></span>
        <small>
          {row.status === "active" || row.status === "requested"
            ? days === null ? t("v3.projects.shares.noExpiry") : t("v3.projects.shares.daysLeft", { count: days })
            : t("v3.projects.shares.endedBy", { who: row.revoked_by ?? row.approved_by ?? "—" })}
        </small>
      </div>
      {row.note && <div className="v3-shr-note">{row.note}</div>}
      <div className="v3-shr-id" style={{ gridColumn: "2 / -1", marginTop: 6 }}>
        {t("v3.projects.shares.grantedBy", { who: row.granted_by })}
        {row.approved_by && ` · ${t("v3.projects.shares.approvedBy", { who: row.approved_by })}`}
      </div>
    </li>
  );
}

function GrantDialog({
  project,
  onClose,
  onGranted,
}: {
  project: ProjectDetail;
  onClose: () => void;
  onGranted: (row: ResourceShare) => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState<ShareDraft>({
    workspace_id: project.workspaces[0]?.workspace_id ?? "",
    kind: "eval_dataset",
    resource_id: "",
    resource_name: "",
    grantee_project_id: "",
    access: "read",
    days: "90",
    note: "",
  });
  const [options, setOptions] = useState<ShareOptions | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!draft.workspace_id) return;
    let live = true;
    setOptions(null);
    setLoadError(null);
    projectSharesApi.options(project.id, draft.workspace_id).then(
      (next) => {
        if (!live) return;
        setOptions(next);
        setDraft((d) => ({ ...d, days: String(next.default_expiry_days) }));
      },
      (err) => live && setLoadError(err),
    );
    return () => {
      live = false;
    };
  }, [project.id, draft.workspace_id]);
  const resources = useMemo(() => (options?.resources ?? []).filter((r) => r.kind === draft.kind), [options, draft.kind]);
  const grantee = options?.grantees.find((g) => g.id === draft.grantee_project_id);
  const problems = draftProblems(draft, options?.max_expiry_days ?? 365);
  const set = (patch: Partial<ShareDraft>) => setDraft((d) => ({ ...d, ...patch }));

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      onGranted(await projectSharesApi.create(project.id, draftPayload(draft)));
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      wide
      title={t("v3.projects.shares.grantTitle", { name: project.name })}
      onClose={onClose}
      foot={
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>{t("v3.projects.shares.cancel")}</Btn>
          <Btn kind="primary" onClick={() => void submit()} disabled={busy || !options || problems.length > 0}>
            <Share2 size={14} /> {grantee?.cross_department ? t("v3.projects.shares.request") : t("v3.projects.shares.grant")}
          </Btn>
        </>
      }
    >
      <div className="v3-shr-form" data-testid="project-share-form">
        <div className="v3-shr-row2">
          <label className="field">
            {t("v3.projects.shares.field.workspace")}
            <select className="v3-input" value={draft.workspace_id}
              onChange={(e) => set({ workspace_id: e.target.value, resource_id: "", grantee_project_id: "" })}>
              {project.workspaces.map((w) => <option key={w.workspace_id} value={w.workspace_id}>{w.name || w.workspace_id}</option>)}
            </select>
          </label>
          <label className="field">
            {t("v3.projects.shares.field.days")}
            <input className="v3-input" inputMode="numeric" value={draft.days}
              aria-invalid={problems.includes("days")} onChange={(e) => set({ days: e.target.value })} />
          </label>
        </div>
        <div className="field" style={{ display: "grid", gap: 6, fontSize: 12.5, color: "var(--v3-text-2)" }}>
          {t("v3.projects.shares.field.kind")}
          <div className="v3-shr-kinds" role="group">
            {SHARE_KINDS.map((kind) => {
              const Icon = KIND_ICON[kind];
              return (
                <button key={kind} type="button" className={`v3-btn sm${draft.kind === kind ? "" : " ghost"}`}
                  aria-pressed={draft.kind === kind} onClick={() => set({ kind, resource_id: "", resource_name: "" })}>
                  <Icon size={13} /> {t(`v3.projects.shares.kind.${kind}`)}
                </button>
              );
            })}
          </div>
        </div>
        {loadError != null && <Notice s="act"><ApiErrorDetail error={loadError} /></Notice>}
        {!options && loadError == null && <Skeleton rows={2} />}
        {options && (
          <>
            <label className="field">
              {t("v3.projects.shares.field.resource")}
              {resources.length > 0 && (
                <select className="v3-input" value={resources.some((r) => r.id === draft.resource_id) ? draft.resource_id : ""}
                  onChange={(e) => {
                    const picked = resources.find((r) => r.id === e.target.value);
                    set({ resource_id: e.target.value, resource_name: picked?.name ?? "" });
                  }}>
                  <option value="">{t("v3.projects.shares.pick")}</option>
                  {resources.map((r) => (
                    <option key={r.id} value={r.id}>{r.name}{r.detail ? ` · ${r.detail}` : ""}</option>
                  ))}
                </select>
              )}
              {typedKind(draft.kind) && (
                <input className="v3-input mono" value={draft.resource_id} placeholder={t("v3.projects.shares.typedId")}
                  onChange={(e) => set({ resource_id: e.target.value })} />
              )}
              {resources.length === 0 && !typedKind(draft.kind) && (
                <small className="v3-hint">{t("v3.projects.shares.noResources")}</small>
              )}
            </label>
            <label className="field">
              {t("v3.projects.shares.field.grantee")}
              <select className="v3-input" value={draft.grantee_project_id}
                onChange={(e) => set({ grantee_project_id: e.target.value })}>
                <option value="">{t("v3.projects.shares.pick")}</option>
                {options.grantees.map((g) => (
                  <option key={g.id} value={g.id}>{g.name} ({g.id}){g.cross_department ? ` · ${t("v3.projects.shares.crossShort")}` : ""}</option>
                ))}
              </select>
            </label>
            {grantee?.cross_department && <Notice s="wait">{t("v3.projects.shares.crossDepartment")}</Notice>}
            <div className="field" style={{ display: "grid", gap: 6, fontSize: 12.5, color: "var(--v3-text-2)" }}>
              {t("v3.projects.shares.field.access")}
              <div className="v3-shr-seg" role="group">
                {(["read", "use"] as ShareAccess[]).map((level) => (
                  <button key={level} type="button" aria-pressed={draft.access === level} onClick={() => set({ access: level })}>
                    <strong><Lamp s={level === "use" ? "ok" : "info"} /> {t(`v3.projects.shares.access.${level}`)}</strong>
                    <small>{t(`v3.projects.shares.accessHint.${level}`)}</small>
                  </button>
                ))}
              </div>
            </div>
            <label className="field">
              {t("v3.projects.shares.field.note")}
              <textarea className="v3-input" rows={2} value={draft.note} maxLength={2000}
                onChange={(e) => set({ note: e.target.value })} />
            </label>
          </>
        )}
        {error != null && <Notice s="act"><ApiErrorDetail error={error} /></Notice>}
      </div>
    </Dialog>
  );
}
