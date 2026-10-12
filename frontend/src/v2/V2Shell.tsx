import "./v2.css";
import "./v2-classic.css";
import "./glossary.css";

import { ChevronDown, LogOut, Repeat } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, Outlet, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import { RouteChunk } from "../layout/RouteChunk";
import { getUiVersion, setUiVersion } from "../lib/ui-version";
import { useNavMode } from "../lib/nav-mode";
import { useWorkspace } from "../workspace/workspace-context";
import { V2Lang } from "./Lang";
import { V2Logo } from "./Logo";
import { isNavItemActive, navGroupsFor } from "./nav";
import { ModuleDemoProvider } from "./pages/videos/ModuleDemo";
import { TierTag } from "./pages/workspaces/tags";
import { FilterSelect, V2ToastProvider } from "./ui";

const COLLAPSE_KEY = "launchpad_v2_nav_collapsed";

function readCollapsed(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem(COLLAPSE_KEY) ?? "{}") as Record<string, boolean>;
  } catch {
    return {};
  }
}

function WorkspaceSelect() {
  const { t } = useTranslation();
  const { workspaces, current, select } = useWorkspace();
  if (workspaces.length === 0) return null;
  return (
    <>
      <FilterSelect
        label={t("topbar.workspaceLabel")}
        title={t("topbar.workspaceTitle")}
        value={current?.id ?? ""}
        options={workspaces.map((ws) => ({
          value: ws.id,
          // T05: a non-dev tier is part of the name, so prod is never picked by mistake
          label: `${ws.name} · ${ws.region}${
            ws.tier && ws.tier !== "dev" ? ` · ${t(`v2.workspaces.tier.${ws.tier}`)}` : ""
          }`,
        }))}
        onChange={select}
        testId="v2-workspace-select"
      />
      {current && (
        <span data-testid="v2-workspace-tier" data-tier={current.tier ?? "dev"}>
          <TierTag tier={current.tier} />
        </span>
      )}
    </>
  );
}

/**
 * Chrome of the V2 console: top bar (brand, workspace, language, user, switch
 * back to the classic console) and a grouped, collapsible sidebar. It wraps
 * both the native V2 pages under /v2 and — with `classic` — the classic
 * modules' pages, which render on the V2 light theme (v2-classic.css) until
 * they are rebuilt natively.
 */
export function V2Shell({ classic = false }: { classic?: boolean }) {
  const { t } = useTranslation();
  const location = useLocation();
  const navigate = useNavigate();
  const { isAdmin, authRequired, username, logout } = useAuth();
  const { current } = useWorkspace();
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>(readCollapsed);
  // T11: members land on the pared-down sidebar, admins on the full one; either
  // can switch. A filter only — every route stays reachable by URL.
  const [navMode, setNavMode] = useNavMode(isAdmin);
  const navGroups = navGroupsFor(navMode, isAdmin);

  // Opening a /v2 page (a bookmark, a shared link) is choosing V2 over the classic
  // console: its modules reached from the sidebar then stay inside this shell. It
  // does NOT undo a V3 choice — V3 links to pages it has not rebuilt yet, and
  // following one must not silently switch the operator's console back.
  useEffect(() => {
    if (!classic && getUiVersion() === "v1") setUiVersion("v2");
  }, [classic]);

  // The classic console styles <body> for its dark theme; V2 overrides it
  // only while mounted.
  useEffect(() => {
    document.body.classList.add("v2-body");
    return () => document.body.classList.remove("v2-body");
  }, []);

  const toggle = (key: string) => {
    setCollapsed((prev) => {
      const next = { ...prev, [key]: !prev[key] };
      try {
        localStorage.setItem(COLLAPSE_KEY, JSON.stringify(next));
      } catch {
        // per-browser convenience only
      }
      return next;
    });
  };

  // A classic module stays on its page (the classic shell takes over the same
  // route); a native V2 page has no classic twin, so it lands on the overview.
  const switchToClassic = () => {
    setUiVersion("v1");
    if (!classic) navigate("/");
  };

  const displayName = authRequired ? (username ?? "—") : "operator";

  return (
    <div className="v2" data-testid="v2-shell">
      <V2ToastProvider>
        <ModuleDemoProvider>
          <header className="v2-top">
            <Link to="/v2" className="v2-brand">
              <V2Logo className="v2-brand-logo" />
              {t("v2.brand")}
              <small>V2</small>
            </Link>
            <nav className="v2-top-tabs" aria-label={t("v2.nav.products")}>
              <span className="on">{t("v2.nav.productAgents")}</span>
            </nav>
            <div className="v2-top-right">
              <WorkspaceSelect />
              <V2Lang />
              <button
                type="button"
                className="v2-btn sm"
                // every V2 page has a V3 home (hosted or rebuilt): stay on this one
                onClick={() => setUiVersion("v3")}
                data-testid="v2-switch-v3"
                title={t("v3.switch.tryHint")}
              >
                {t("v3.switch.try")}
              </button>
              <button type="button" className="v2-btn sm" onClick={switchToClassic} data-testid="v2-switch-classic">
                <Repeat size={13} aria-hidden="true" />
                {t("v2.switchClassic")}
              </button>
              <div className="v2-user">
                <span className="v2-avatar">{displayName.slice(0, 1).toUpperCase()}</span>
                <span>{displayName}</span>
                {authRequired && (
                  <button
                    type="button"
                    className="v2-link"
                    onClick={() => void logout()}
                    title={t("auth.logout")}
                    aria-label={t("auth.logout")}
                  >
                    <LogOut size={14} />
                  </button>
                )}
              </div>
            </div>
          </header>
          <div className="v2-layout">
            <aside className="v2-side" aria-label={t("v2.nav.label")}>
              {navGroups.map((group) => {
                const items = group.items;
                const closed = collapsed[group.key] === true;
                return (
                  <div key={group.key} className="v2-side-group">
                    <button
                      type="button"
                      className="v2-side-head"
                      aria-expanded={!closed}
                      onClick={() => toggle(group.key)}
                    >
                      {t(group.labelKey)}
                      <ChevronDown size={14} className={closed ? "chev closed" : "chev"} aria-hidden="true" />
                    </button>
                    {!closed &&
                      items.map((item) => {
                        const Icon = item.icon;
                        const active = isNavItemActive(item, location.pathname, location.search);
                        return (
                          <Link
                            key={item.to}
                            to={item.to}
                            className={active ? "v2-side-item active" : "v2-side-item"}
                            aria-current={active ? "page" : undefined}
                            data-testid={`v2-nav-${item.to}`}
                            title={item.hintKey ? t(item.hintKey) : undefined}
                          >
                            <Icon size={16} aria-hidden="true" />
                            {t(item.labelKey)}
                          </Link>
                        );
                      })}
                  </div>
                );
              })}
              <button
                type="button"
                className="v2-side-mode"
                data-testid="v2-nav-mode"
                onClick={() => setNavMode(navMode === "business" ? "expert" : "business")}
              >
                {navMode === "business" ? t("v2.nav.showExpert") : t("v2.nav.showBusiness")}
              </button>
            </aside>
            <div className="v2-main">
              {/* Workspace-bound pages refetch on selection; hub-global content
                  and admin drafts survive a workspace switch. */}
              <div
                className={classic ? "v2-main-inner view v2-classic" : "v2-main-inner"}
                key={
                  location.pathname === "/announcements" || location.pathname === "/v2/announcements" ||
                  location.pathname === "/videos" || location.pathname === "/v2/videos" ||
                  location.pathname === "/v2/video-management"
                    ? "hub-global-content"
                    : current?.id ?? "none"
                }
              >
                <RouteChunk key={location.pathname}>
                  <Outlet />
                </RouteChunk>
              </div>
            </div>
          </div>
        </ModuleDemoProvider>
      </V2ToastProvider>
    </div>
  );
}
