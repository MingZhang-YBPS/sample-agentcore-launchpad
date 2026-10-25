import { LogOut, ShieldAlert } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../auth/auth-context";
import { AnnouncementContentView } from "../components/AnnouncementContentView";
import { api } from "../lib/api";
import { V3AuthFrame } from "./AuthFrame";
import { useLoad } from "./hooks";
import { Btn, Lamp, Notice, PageHead, Panel, Skeleton } from "./ui";

const PAGE_SIZE = 3;

/** Installation-wide announcements: they need no workspace (or project), so a member
 *  without one still reads them — here and on `NoProjectAssigned`. */
export function V3Announcements() {
  const { t, i18n } = useTranslation();
  const [offset, setOffset] = useState(0);
  const page = useLoad(() => api.listAnnouncements(PAGE_SIZE, offset), `v3-nows-ann:${offset}`);
  const total = page.data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  return (
    <Panel title={t("announcements.feedTitle")} className="v3-nows-feed" flush>
      <div data-testid="announcement-feed" aria-busy={page.loading}>
        {page.error ? (
          <div className="v3-nows-pad" data-testid="announcements-public-error">
            <Notice s="act">
              {page.error}{" "}
              <Btn size="sm" kind="ghost" onClick={page.reload}>{t("v3.announcements.retry")}</Btn>
            </Notice>
          </div>
        ) : page.loading && !page.data ? (
          <div className="v3-nows-pad"><Skeleton rows={3} /></div>
        ) : total === 0 ? (
          <p className="v3-nows-pad v3-nows-empty">{t("announcements.publicEmpty")}</p>
        ) : (
          page.data?.announcements.map((row) => (
            <article className="v3-nows-notice" key={row.id} data-testid={`announcement-${row.id}`}>
              <time dateTime={row.published_at} className="mono">
                {new Date(row.published_at).toLocaleDateString(i18n.language, {
                  year: "numeric", month: "short", day: "numeric",
                })}
              </time>
              <AnnouncementContentView content={row} />
            </article>
          ))
        )}
        {total > PAGE_SIZE && (
          <nav className="v3-nows-pager" aria-label={t("announcements.pagination")}>
            <span className="mono">{t("pager.total", { count: total })}</span>
            <Btn size="sm" kind="ghost" busy={page.loading} disabled={offset === 0} disabledReason={t("story.reason.firstPage")}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>‹ {t("pager.prev")}</Btn>
            <span className="mono">{t("pager.page", { page: Math.floor(offset / PAGE_SIZE) + 1, pages })}</span>
            <Btn size="sm" kind="ghost" busy={page.loading} disabled={offset + PAGE_SIZE >= total} disabledReason={t("story.reason.lastPage")}
              onClick={() => setOffset(offset + PAGE_SIZE)}>{t("pager.next")} ›</Btn>
          </nav>
        )}
      </div>
    </Panel>
  );
}

/**
 * A member whose account has no workspace grant yet, on the V3 chrome: who can fix
 * it, signing out, and the installation-wide announcements below.
 */
export function V3NoWorkspace() {
  const { t } = useTranslation();
  const { authRequired, username, logout } = useAuth();
  return (
    <V3AuthFrame
      testId="no-workspace-page"
      end={authRequired ? (
        <button type="button" className="v3-btn sm" onClick={() => void logout()} data-testid="no-workspace-logout">
          <LogOut size={13} aria-hidden="true" />
          {t("auth.logout")}
        </button>
      ) : null}
    >
      <div className="v3-nows">
        <PageHead
          eyebrow={t("v3.noWorkspace.eyebrow")}
          title={t("workspacesPage.noneTitle")}
          sub={t("workspacesPage.noneMeta", { username: username ?? "—" })}
        />
        <Panel signal="wait">
          <div className="v3-nows-state" data-testid="no-workspace-body">
            <span className="ico"><ShieldAlert size={22} aria-hidden="true" /></span>
            <div>
              <b><Lamp s="wait" live /> {t("v3.noWorkspace.waiting")}</b>
              <p>{t("workspacesPage.noneBody")}</p>
            </div>
          </div>
        </Panel>
        <V3Announcements />
      </div>
    </V3AuthFrame>
  );
}
