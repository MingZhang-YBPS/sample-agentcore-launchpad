import "./announcements.css";

import { ArrowLeft, ArrowRight, ExternalLink, Plus, RotateCw, ShieldAlert } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { api, type Announcement, type AnnouncementContent } from "../../lib/api";
import { announcementLink } from "../../lib/announcements";
import {
  type ConfirmAction,
  isDirty,
  NEW_ID,
  PAGE_SIZE,
  publishReason,
  saveReason,
  sessionFor,
  validationKey,
} from "../../v2/pages/announcements/common";
import { type AnnouncementSessions, useAnnouncementSessions } from "../../v2/pages/announcements/useSessions";
import { ago } from "../format";
import { useLoad, useToast } from "../hooks";
import { Btn, Chip, Confirm, Empty, Lamp, Notice, PageHead, Panel, type Signal, Skeleton, Stat } from "../ui";

function statusSignal(row: Announcement | null): Signal {
  if (!row) return "info";
  if (row.status === "published") return row.has_unpublished_changes ? "wait" : "ok";
  return "off";
}

function StatusChips({ row, dirty }: { row: Announcement | null; dirty: boolean }) {
  const { t } = useTranslation();
  const status = row?.status ?? "draft";
  return (
    <span className="v3-ann-chips">
      <Chip s={status === "published" ? "ok" : undefined}>{t(`announcements.status.${status}`)}</Chip>
      {row?.status === "published" && row.has_unpublished_changes && <Chip s="wait">{t("announcements.unpublishedChanges")}</Chip>}
      {dirty && <Chip s="info">{t("announcements.unsaved")}</Chip>}
    </span>
  );
}

/** How users read it: text only — no HTML or Markdown; only the validated link is clickable. */
function Preview({ content }: { content: AnnouncementContent }) {
  const { t } = useTranslation();
  const link = content.link_url ? announcementLink(content.link_url.trim()) : null;
  const label = content.link_label?.trim();
  return (
    <div className="v3-ann-preview">
      <h3>{content.title.trim() || <span className="muted">{t("v2.announcements.untitled")}</span>}</h3>
      <p>{content.body.trim() || <span className="muted">{t("v2.announcements.noBody")}</span>}</p>
      {link && content.link_url && label ? (
        <a
          href={content.link_url.trim()}
          target={link === "external" ? "_blank" : undefined}
          rel={link === "external" ? "noopener noreferrer" : undefined}
        >
          {label}
          {link === "external" ? (
            <>
              <ExternalLink size={13} aria-hidden="true" />
              <span className="sr">{t("announcements.newTab")}</span>
            </>
          ) : (
            <ArrowRight size={13} aria-hidden="true" />
          )}
        </a>
      ) : null}
    </div>
  );
}

/* ── list ────────────────────────────────────────────────────────────────── */

function List({
  store,
  offset,
  onOffset,
  onOpen,
}: {
  store: AnnouncementSessions;
  offset: number;
  onOffset: (offset: number) => void;
  onOpen: (id: string) => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const { sessions, act, tick, refresh } = store;
  const list = useLoad(() => api.manageAnnouncements(PAGE_SIZE, offset), `v3-ann:${offset}:${tick}`);
  const total = list.data?.total ?? 0;
  const rows = list.data?.announcements ?? [];
  const [confirm, setConfirm] = useState<{ row: Announcement; action: ConfirmAction } | null>(null);

  // a delete elsewhere can leave this page past the end: step back to the last page
  useEffect(() => {
    if (list.data && offset > 0 && offset >= list.data.total) {
      onOffset(Math.max(0, Math.floor((list.data.total - 1) / PAGE_SIZE) * PAGE_SIZE));
    }
  }, [list.data, offset, onOffset]);

  const buffered = Object.entries(sessions).filter(([id, s]) => id === NEW_ID || isDirty(s) || s.busy || s.error);
  const published = rows.filter((r) => r.status === "published").length;
  const pendingChanges = rows.filter((r) => r.status === "published" && r.has_unpublished_changes).length;

  const run = async (row: Announcement, action: ConfirmAction) => {
    const outcome = await act(row.id, action, row);
    if (!outcome) return;
    if (outcome.ok) toast("ok", t(action === "delete" ? "v2.announcements.deleted" : `announcements.success.${action}`));
    else toast("act", `${t(`announcements.actions.${action}`)}: ${outcome.error}`);
  };

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.announcements.eyebrow")}
        title={t("announcements.title")}
        sub={t("announcements.description")}
        end={
          <>
            <Btn kind="ghost" onClick={refresh}><RotateCw size={14} /></Btn>
            <Btn kind="primary" onClick={() => onOpen(NEW_ID)}><Plus size={14} /> {t("announcements.new")}</Btn>
          </>
        }
      />

      {buffered.length > 0 && (
        <Notice s="wait">
          {t("announcements.buffers")}{" "}
          <span className="v3-ann-chips">
            {buffered.map(([id, s]) => (
              <button key={id} type="button" className="v3-btn sm ghost" onClick={() => onOpen(id)}>
                {s.content.title || t("announcements.newDraft")} ·{" "}
                {t(s.busy ? "announcements.working" : s.error ? "announcements.needsAttention" : "announcements.unsaved")}
              </button>
            ))}
          </span>
        </Notice>
      )}

      <div className="v3-grid c3">
        <Panel><Stat label={t("v3.announcements.total")} value={list.data ? total : "—"} /></Panel>
        <Panel signal={published ? "ok" : undefined}>
          <Stat label={t("v3.announcements.live")} value={list.data ? published : "—"} foot={t("v3.announcements.liveFoot")} />
        </Panel>
        <Panel signal={pendingChanges ? "wait" : undefined}>
          <Stat label={t("v3.announcements.changed")} value={list.data ? pendingChanges : "—"}
            signal={pendingChanges ? "wait" : undefined} foot={t("v3.announcements.changedFoot")} />
        </Panel>
      </div>

      <Panel flush>
        {list.loading && !list.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
        ) : list.error ? (
          <div style={{ padding: 20 }}><Notice s="act">{list.error}</Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={t("announcements.manageEmpty")} />
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v2.announcements.col.title")}</th>
                <th>{t("v2.announcements.col.status")}</th>
                <th className="num">{t("v2.announcements.col.revision")}</th>
                <th className="num">{t("v2.announcements.col.updated")}</th>
                <th className="num">{t("v2.announcements.col.published")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const session = sessions[row.id] ?? sessionFor(row);
                const busy = Boolean(session.busy);
                const blocked = publishReason(session);
                return (
                  <tr key={row.id} className="click" onClick={() => onOpen(row.id)}>
                    <td style={{ width: 30 }}><Lamp s={statusSignal(row)} /></td>
                    <td>
                      <div className="v3-name"><div><b>{row.content.title}</b><small>{row.id}</small></div></div>
                    </td>
                    <td><StatusChips row={row} dirty={sessions[row.id] ? isDirty(sessions[row.id]) : false} /></td>
                    <td className="num">{row.revision}</td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }} title={row.updated_by}>{ago(row.updated_at)}</td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{row.published_at ? ago(row.published_at) : "—"}</td>
                    <td style={{ width: 1, whiteSpace: "nowrap" }} onClick={(e) => e.stopPropagation()}>
                      <div style={{ display: "flex", gap: 6, justifyContent: "flex-end" }}>
                        <Btn size="sm" disabled={busy || Boolean(blocked)} title={blocked ? t(blocked) : undefined}
                          onClick={() => setConfirm({ row, action: "publish" })}>
                          {t("v2.announcements.publish")}
                        </Btn>
                        {row.status === "published" && (
                          <Btn size="sm" kind="ghost" disabled={busy} onClick={() => setConfirm({ row, action: "unpublish" })}>
                            {t("v2.announcements.unpublish")}
                          </Btn>
                        )}
                        <Btn size="sm" kind="ghost" disabled={busy} onClick={() => setConfirm({ row, action: "delete" })}>
                          {t("v3.announcements.delete")}
                        </Btn>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
        {total > PAGE_SIZE && (
          <div className="v3-ann-pager">
            <Btn size="sm" kind="ghost" disabled={offset === 0} onClick={() => onOffset(Math.max(0, offset - PAGE_SIZE))}>‹</Btn>
            <span className="mono">{Math.floor(offset / PAGE_SIZE) + 1} / {Math.max(1, Math.ceil(total / PAGE_SIZE))}</span>
            <Btn size="sm" kind="ghost" disabled={offset + PAGE_SIZE >= total} onClick={() => onOffset(offset + PAGE_SIZE)}>›</Btn>
          </div>
        )}
      </Panel>

      {confirm && (
        <Confirm
          title={t(`announcements.confirm.${confirm.action}.title`)}
          confirmLabel={t(`announcements.actions.${confirm.action}`)}
          cancelLabel={t("v3.common.cancel")}
          danger={confirm.action === "delete"}
          onCancel={() => setConfirm(null)}
          onConfirm={() => {
            const target = confirm;
            setConfirm(null);
            void run(target.row, target.action);
          }}
        >
          {t(`announcements.confirm.${confirm.action}.body`, { title: confirm.row.content.title })}
        </Confirm>
      )}
    </div>
  );
}

/* ── editor ──────────────────────────────────────────────────────────────── */

function Editor({
  id,
  store,
  onBack,
  onMoved,
}: {
  id: string;
  store: AnnouncementSessions;
  onBack: () => void;
  onMoved: (from: string, to: string | null) => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const { sessions, detailError, ensure, setContent, act } = store;
  const session = sessions[id];
  const [confirm, setConfirm] = useState<ConfirmAction | null>(null);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    ensure(id, controller.signal);
    return () => controller.abort();
  }, [id, ensure, retry]);

  const run = async (action: "save" | ConfirmAction) => {
    const outcome = await act(id, action);
    if (!outcome?.ok) return; // failures stay inline with the preserved buffer
    if (action === "delete") {
      toast("ok", t("v2.announcements.deleted"));
      onMoved(id, null);
      return;
    }
    toast("ok", t(`announcements.success.${action}`));
    if (id === NEW_ID && outcome.row) onMoved(id, outcome.row.id);
  };

  const back = (
    <div>
      <button type="button" className="v3-btn ghost sm" onClick={onBack}>
        <ArrowLeft size={14} /> {t("announcements.title")}
      </button>
    </div>
  );

  if (!session) {
    return (
      <div style={{ display: "grid", gap: 16 }}>
        {back}
        {detailError?.id === id ? (
          <Notice s="act">
            {detailError.message}{" "}
            <Btn size="sm" onClick={() => setRetry((v) => v + 1)}>{t("v3.announcements.retry")}</Btn>
          </Notice>
        ) : (
          <Skeleton rows={6} />
        )}
      </div>
    );
  }

  const { row, content, busy, error, errorCode } = session;
  const dirty = isDirty(session);
  const invalid = dirty ? validationKey(content) : null;
  const cantSave = saveReason(session);
  const cantPublish = publishReason(session);
  const edit = (patch: Partial<AnnouncementContent>) => setContent(id, { ...content, ...patch });
  const linkUrlError =
    invalid === "announcements.validation.linkUrl" ||
    (invalid === "announcements.validation.linkPair" && !content.link_url?.trim())
      ? t(invalid)
      : null;
  const linkLabelError = invalid === "announcements.validation.linkPair" && !content.link_label?.trim() ? t(invalid) : null;
  // save leads while there is something to save; afterwards publishing is the next step
  const saveLeads = !cantSave || !row;

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }} aria-busy={Boolean(busy)}>
      {back}
      <PageHead
        eyebrow={row ? `${t("v3.announcements.eyebrow")} · ${t("announcements.revision", { revision: row.revision })}` : t("v3.announcements.eyebrow")}
        title={
          <span style={{ display: "inline-flex", alignItems: "center", gap: 14 }}>
            <Lamp s={statusSignal(row)} />
            {row?.content.title || t("announcements.newDraft")}
          </span>
        }
        sub={<StatusChips row={row} dirty={dirty} />}
        end={
          <>
            {row && (
              <>
                <Btn kind="ghost" disabled={Boolean(busy)} onClick={() => (dirty ? setConfirm("reload") : void run("reload"))}>
                  {t("announcements.actions.reload")}
                </Btn>
                <Btn kind="danger" disabled={Boolean(busy)} onClick={() => setConfirm("delete")}>{t("v3.announcements.delete")}</Btn>
                {row.status === "published" && (
                  <Btn disabled={Boolean(busy)} onClick={() => setConfirm("unpublish")}>{t("announcements.actions.unpublish")}</Btn>
                )}
                <Btn kind={saveLeads ? undefined : "primary"} disabled={Boolean(busy || cantPublish)}
                  title={!busy && cantPublish ? t(cantPublish) : undefined} onClick={() => setConfirm("publish")}>
                  {t(busy === "publish" ? "announcements.publishing" : "announcements.actions.publish")}
                </Btn>
              </>
            )}
            <Btn kind={saveLeads ? "primary" : undefined} disabled={Boolean(busy || cantSave)}
              title={!busy && cantSave ? t(cantSave) : undefined} onClick={() => void run("save")}>
              {t(busy === "save" ? "announcements.saving" : "announcements.actions.save")}
            </Btn>
          </>
        }
      />

      {error && (
        <Notice s="act">
          {error}
          <br />
          {t(errorCode === "announcements.conflict" ? "announcements.conflictHint" : "announcements.preserved")}
        </Notice>
      )}
      {invalid && invalid !== "announcements.validation.linkPair" && invalid !== "announcements.validation.linkUrl" && (
        <Notice s="wait">{t(invalid)}</Notice>
      )}
      <Notice>{t("announcements.draftHint")}</Notice>

      <div className="v3-grid v3-split" style={{ alignItems: "start" }}>
        <Panel title={t("v2.announcements.draft")}>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              if (!cantSave && !busy) void run("save");
            }}
          >
            <fieldset className="v3-ann-form" disabled={Boolean(busy)}>
              <label className="v3-field">
                <span>{t("v2.announcements.field.title")} *</span>
                <input className="v3-input" required maxLength={160} value={content.title} onChange={(e) => edit({ title: e.target.value })} />
                <small className="v3-hint">{t("v2.announcements.hint.title", { count: content.title.length })}</small>
              </label>
              <label className="v3-field">
                <span>{t("v2.announcements.field.body")} *</span>
                <textarea className="v3-input" required maxLength={6000} rows={10} value={content.body} onChange={(e) => edit({ body: e.target.value })} />
                <small className="v3-hint">{t("announcements.contentHint")}</small>
              </label>
              <div className="v3-grid c2" style={{ alignItems: "start" }}>
                <label className="v3-field">
                  <span>{t("v2.announcements.field.linkUrl")}</span>
                  <input className="v3-input mono" maxLength={2048} spellCheck={false} placeholder="/v2/videos · https://…"
                    value={content.link_url ?? ""} aria-invalid={Boolean(linkUrlError)} onChange={(e) => edit({ link_url: e.target.value })} />
                  <small className={linkUrlError ? "v3-err" : "v3-hint"}>{linkUrlError ?? t("announcements.linkHint")}</small>
                </label>
                <label className="v3-field">
                  <span>{t("v2.announcements.field.linkLabel")}</span>
                  <input className="v3-input" maxLength={80} value={content.link_label ?? ""} aria-invalid={Boolean(linkLabelError)}
                    onChange={(e) => edit({ link_label: e.target.value })} />
                  <small className={linkLabelError ? "v3-err" : "v3-hint"}>{linkLabelError ?? t("v2.announcements.hint.linkLabel")}</small>
                </label>
              </div>
            </fieldset>
            <button type="submit" hidden aria-hidden="true" tabIndex={-1} />
          </form>
        </Panel>

        <div style={{ display: "grid", gap: 16 }}>
          <Panel title={t("v2.announcements.draftPreview")} end={<span style={{ color: "var(--v3-text-3)" }}>{t("v2.announcements.draftPreviewSub")}</span>}>
            <Preview content={content} />
          </Panel>
          <Panel title={t("announcements.publishedTitle")} signal={row?.published_content ? "ok" : undefined}
            end={row?.published_at ? <span style={{ color: "var(--v3-text-3)" }}>{ago(row.published_at)}</span> : undefined}>
            {row?.published_content ? (
              <>
                <p style={{ margin: "0 0 10px", color: "var(--v3-text-3)", fontSize: 12.5 }}>{t("announcements.publishedHint")}</p>
                <Preview content={row.published_content} />
              </>
            ) : (
              <p style={{ margin: 0, color: "var(--v3-text-3)" }}>{t("announcements.notPublic")}</p>
            )}
          </Panel>
          {row && (
            <Panel title={t("v2.announcements.info")}>
              <dl className="v3-kv">
                <dt>ID</dt><dd className="mono">{row.id}</dd>
                <dt>{t("v2.announcements.col.revision")}</dt><dd className="mono">{row.revision}</dd>
                <dt>{t("v2.announcements.createdBy")}</dt><dd>{row.created_by} · {ago(row.created_at)}</dd>
                <dt>{t("v2.announcements.updatedBy")}</dt><dd>{row.updated_by} · {ago(row.updated_at)}</dd>
                <dt>{t("v2.announcements.col.published")}</dt><dd>{row.published_at ? ago(row.published_at) : "—"}</dd>
              </dl>
            </Panel>
          )}
        </div>
      </div>

      {confirm && (
        <Confirm
          title={t(`announcements.confirm.${confirm}.title`)}
          confirmLabel={t(`announcements.actions.${confirm}`)}
          cancelLabel={t("v3.common.cancel")}
          danger={confirm === "delete" || confirm === "reload"}
          onCancel={() => setConfirm(null)}
          onConfirm={() => {
            const target = confirm;
            setConfirm(null);
            void run(target);
          }}
        >
          {t(`announcements.confirm.${confirm}.body`, { title: row?.content.title ?? content.title })}
        </Confirm>
      )}
    </div>
  );
}

function Manager() {
  const [params, setParams] = useSearchParams();
  const paramsRef = useRef(params);
  paramsRef.current = params;
  // editing buffers live here, so they survive list ↔ editor navigation
  const store = useAnnouncementSessions();
  const [offset, setOffset] = useState(0);
  const view = params.get("view");
  const selected = view === "new" ? NEW_ID : view === "edit" ? params.get("id") : null;
  const open = useCallback((id: string) => setParams(id === NEW_ID ? { view: "new" } : { view: "edit", id }), [setParams]);
  const back = useCallback(() => setParams({}), [setParams]);
  const moved = useCallback(
    (from: string, to: string | null) => {
      const current = paramsRef.current;
      const shown = current.get("view") === "new" ? NEW_ID : current.get("view") === "edit" ? current.get("id") : null;
      if (shown !== from) return;
      if (to) setParams({ view: "edit", id: to }, { replace: true });
      else setParams({});
    },
    [setParams],
  );
  if (selected) return <Editor key={selected} id={selected} store={store} onBack={back} onMoved={moved} />;
  return <List store={store} offset={offset} onOffset={setOffset} onOpen={open} />;
}

/**
 * Installation-wide notices every user sees on the overview (admin-only, hub-global:
 * not bound to the workspace). `?view=new` and `?view=edit&id=` as in V2.
 */
export function V3Announcements() {
  const { isAdmin } = useAuth();
  const { t } = useTranslation();
  // mount no loaders or editor state for a member, including direct deep links
  if (!isAdmin) {
    return (
      <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
        <PageHead eyebrow={t("v3.announcements.eyebrow")} title={t("announcements.title")} sub={t("auth.adminRequired.meta")} />
        <Panel>
          <div className="v3-empty" data-testid="announcements-forbidden">
            <ShieldAlert size={28} aria-hidden="true" />
            <strong>{t("auth.adminRequired.title")}</strong>
            {t("auth.adminRequired.body")}
          </div>
        </Panel>
      </div>
    );
  }
  return <Manager />;
}
