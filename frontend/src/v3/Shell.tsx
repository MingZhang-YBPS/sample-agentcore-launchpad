import "@fontsource-variable/archivo/wdth.css";
import "../v2/v2.css";
import "../v2/glossary.css";
import "./v3.css";
import "./host.css";
import "./onboarding/onboarding.css";

import {
  ArrowLeftRight,
  BookOpen,
  Bot,
  ChevronRight,
  CircleHelp,
  Compass,
  LayoutDashboard,
  ListChecks,
  LogOut,
  MessagesSquare,
  Menu,
  PlayCircle,
  Scale,
  Search,
  X,
} from "lucide-react";
import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import { RouteChunk } from "../layout/RouteChunk";
import { api } from "../lib/api";
import { useNavMode } from "../lib/nav-mode";
import { setUiVersion } from "../lib/ui-version";
import { GLOSSARY_TERMS } from "../v2/Glossary";
import { ModuleDemoProvider } from "../v2/pages/videos/ModuleDemo";
import { V2ToastProvider } from "../v2/ui";
import { useWorkspace } from "../workspace/workspace-context";
import { type Command, CommandPalette } from "./CommandPalette";
import { ToastProvider } from "./ui";
import { useLoad } from "./hooks";
import { hostedGroups, isActive } from "./nav";
import { GlossaryDialog } from "./onboarding/Glossary";
import { openGlossary, requestTour, tourDone, useLaunchHidden, useTourRequest } from "./onboarding/state";
import { Tour } from "./onboarding/Tour";

const RAIL_KEY = "launchpad_v3_rail_open";

function readOpen(): Record<string, boolean> {
  try {
    return JSON.parse(localStorage.getItem(RAIL_KEY) ?? "{}") as Record<string, boolean>;
  } catch {
    return {};
  }
}

interface RailItem {
  to: string;
  labelKey: string;
  icon: ReactNode;
  end?: boolean;
}

const NATIVE: RailItem[] = [
  { to: "/v3", labelKey: "v3.nav.home", icon: <LayoutDashboard size={16} />, end: true },
  { to: "/v3/agents", labelKey: "v3.nav.agents", icon: <Bot size={16} /> },
  { to: "/v3/chat", labelKey: "v3.nav.chat", icon: <MessagesSquare size={16} /> },
  { to: "/v3/gate", labelKey: "v3.nav.gate", icon: <Scale size={16} /> },
];

/** "?" in the top bar: the tour, the glossary, the launch sequence and the videos, any time. */
function HelpMenu() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { current } = useWorkspace();
  const [, setLaunchHidden] = useLaunchHidden(current?.id ?? "");
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    window.addEventListener("click", close);
    return () => window.removeEventListener("click", close);
  }, [open]);
  const item = (icon: ReactNode, label: string, run: () => void) => (
    <button type="button" role="menuitem" onClick={() => { setOpen(false); run(); }}>{icon} {label}</button>
  );
  return (
    <div className="v3-help" data-tour="help" onClick={(e) => e.stopPropagation()}>
      <button type="button" className="v3-btn ghost sm" aria-haspopup="menu" aria-expanded={open}
        aria-label={t("v3.onboard.help.label")} title={t("v3.onboard.help.label")} onClick={() => setOpen((v) => !v)}>
        <CircleHelp size={15} />
      </button>
      {open && (
        <div className="v3-help-menu" role="menu">
          {item(<Compass size={15} />, t("v3.onboard.help.tour"), requestTour)}
          {item(<ListChecks size={15} />, t("v3.onboard.help.launch"), () => { setLaunchHidden(false); navigate("/v3"); })}
          {item(<BookOpen size={15} />, t("v3.onboard.help.glossary"), () => openGlossary("all"))}
          {item(<PlayCircle size={15} />, t("v3.onboard.help.videos"), () => navigate("/v3/videos"))}
        </div>
      )}
    </div>
  );
}

function WorkspaceSwitch() {
  const { t } = useTranslation();
  const { workspaces, current, select } = useWorkspace();
  if (workspaces.length === 0) return null;
  return (
    <label className="v3-ws" data-tier={current?.tier ?? "dev"} title={t("v3.top.workspace")}>
      <span className="v3-ws-label">{t("v3.top.workspace")}</span>
      <select value={current?.id ?? ""} onChange={(e) => select(e.target.value)} aria-label={t("v3.top.workspace")}>
        {workspaces.map((ws) => (
          <option key={ws.id} value={ws.id}>
            {ws.name} · {ws.region}
            {ws.tier && ws.tier !== "dev" ? ` · ${ws.tier}` : ""}
          </option>
        ))}
      </select>
    </label>
  );
}

function Lang() {
  const { i18n } = useTranslation();
  const zh = (i18n.resolvedLanguage ?? "en").startsWith("zh");
  return (
    <button
      type="button"
      className="v3-btn ghost sm mono"
      onClick={() => void i18n.changeLanguage(zh ? "en" : "zh-CN")}
      aria-label={zh ? "English" : "中文"}
    >
      {zh ? "EN" : "中"}
    </button>
  );
}

/**
 * Console V3 — the command center. A fixed top bar (brand, ⌘K, workspace,
 * language, V2 switch, account) over a rail and the page. Pages V3 has not
 * rebuilt are still in the rail, marked, and open in V2 rather than being
 * wrapped in a shell whose look they do not share.
 */
/** Pages whose content belongs to the installation, not a workspace: they must not
 *  remount (and lose an admin's draft) when the workspace switches. */
const HUB_GLOBAL = new Set(["/announcements", "/v2/announcements", "/v3/announcements", "/videos", "/v2/videos", "/v3/videos", "/v2/video-management", "/v3/video-management"]);

export function V3Shell({ hosted }: { hosted?: "v2" | "classic" } = {}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const { isAdmin, authRequired, username, logout } = useAuth();
  const { current } = useWorkspace();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [railOpen, setRailOpen] = useState(false);
  const railToggle = useRef<HTMLButtonElement>(null);
  useEffect(() => setRailOpen(false), [location.pathname, location.search]);
  useEffect(() => {
    if (!railOpen) return;
    const close = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") {
        setRailOpen(false);
        railToggle.current?.focus();
      }
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [railOpen]);
  const agents = useLoad(() => api.listAgents(), `v3-shell-agents:${current?.id ?? ""}`);

  // Opening a /v3 page is choosing V3 — reloads and "/" land back here.
  useEffect(() => {
    setUiVersion("v3");
    document.body.classList.add("v3-body");
    return () => document.body.classList.remove("v3-body");
  }, []);

  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      const typing = /input|textarea|select/i.test((e.target as HTMLElement)?.tagName ?? "");
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((open) => !open);
      } else if (e.key === "/" && !typing) {
        e.preventDefault();
        setPaletteOpen(true);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  // A hosted page has the same URL in V2, which simply re-renders it in the V2
  // shell; a native V3 page goes to its V2 counterpart.
  const backToV2 = () => {
    setUiVersion("v2");
    const { pathname, search } = location;
    if (!pathname.startsWith("/v3")) return;
    const id = new URLSearchParams(search).get("id");
    if (pathname === "/v3/chat") navigate(`/v2/chat${search}`);
    else if (pathname === "/v3/agents") navigate(id ? `/v2/agents?view=detail&id=${encodeURIComponent(id)}` : "/v2/agents");
    else if (pathname === "/v3/gate") navigate("/v2/eval/standards");
    else navigate("/v2");
  };

  // business mode shows the build → run essentials; expert shows every module.
  // A view filter only: every page stays reachable by URL and by ⌘K.
  const [navMode, setNavMode] = useNavMode(isAdmin);
  const allGroups = useMemo(() => hostedGroups(isAdmin), [isAdmin]);
  const groups = useMemo(
    () =>
      navMode === "expert"
        ? allGroups
        : allGroups
            .map((g) => ({ ...g, items: g.items.filter((item) => item.business === true) }))
            .filter((g) => g.items.length > 0),
    [allGroups, navMode],
  );
  // the tour opens on its own on a first visit to the command center, and on request
  const [touring, setTouring] = useState(false);
  const tourAsk = useTourRequest();
  useEffect(() => {
    if (tourAsk === 0) return;
    if (location.pathname !== "/v3") navigate("/v3");
    const timer = window.setTimeout(() => setTouring(true), 700);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only a new request opens it
  }, [tourAsk]);
  useEffect(() => {
    if (location.pathname !== "/v3" || tourDone()) return;
    // after the page has drawn, so every stop can be measured
    const timer = window.setTimeout(() => setTouring(true), 1200);
    return () => window.clearTimeout(timer);
  }, [location.pathname]);
  // the module groups start folded, except the one holding the current page
  const [open, setOpen] = useState<Record<string, boolean>>(readOpen);
  const toggle = (key: string, wasOpen: boolean) =>
    setOpen((prev) => {
      const next = { ...prev, [key]: !wasOpen };
      try {
        localStorage.setItem(RAIL_KEY, JSON.stringify(next));
      } catch {
        // per-browser convenience only
      }
      return next;
    });

  const pages = useMemo<Command[]>(() => {
    const group = t("v3.cmdk.pages");
    const all = [
      ...NATIVE.map((item) => ({ to: item.to, labelKey: item.labelKey, icon: item.icon })),
      ...allGroups.flatMap((g) =>
        g.items.map(({ to, labelKey, icon: Icon }) => ({ to, labelKey, icon: <Icon size={16} /> })),
      ),
    ];
    return all.map((item) => ({
      id: `p:${item.to}`,
      group,
      label: t(item.labelKey),
      icon: item.icon,
      // the English name and the route, so "chat" finds 对话 under zh-CN
      keywords: `${t(item.labelKey, { lng: "en" })} ${item.to.replace(/[/?=&]/g, " ")}`,
      run: () => navigate(item.to),
    }));
  }, [allGroups, navigate, t]);

  // help in the palette: the tour, the glossary, and one entry per term, so typing
  // a word you do not know ("harness", "金标集") explains it
  const help = useMemo<Command[]>(() => {
    const group = t("v3.onboard.help.group");
    return [
      { id: "h:tour", group, label: t("v3.onboard.help.tour"), icon: <Compass size={16} />, keywords: "tour guide help 导览 引导", run: requestTour },
      { id: "h:glossary", group, label: t("v3.onboard.help.glossary"), icon: <BookOpen size={16} />, keywords: "glossary terms help 术语", run: () => openGlossary("all") },
      ...GLOSSARY_TERMS.map((term) => ({
        id: `g:${term}`,
        group,
        label: t("v3.onboard.glossary.explain", { term: t(`glossary.name.${term}`) }),
        icon: <BookOpen size={16} />,
        keywords: `${term} ${t(`glossary.name.${term}`, { lng: "en" })}`,
        run: () => openGlossary(term),
      })),
    ];
  }, [t]);

  const managingAgent =
    location.pathname === "/v2/agents" && new URLSearchParams(location.search).get("view") !== "new";
  const closePalette = useCallback(() => setPaletteOpen(false), []);
  const displayName = authRequired ? (username ?? "—") : "operator";
  const failing = (agents.data?.agents ?? []).filter((a) => a.status === "failed").length;

  return (
    <div className="v3" data-testid="v3-shell" data-rail-open={railOpen}>
      <ToastProvider>
        <header className="v3-top">
          <button ref={railToggle} type="button" className="v3-btn ghost sm v3-rail-toggle"
            aria-label={t(railOpen ? "v3.ux.closeNav" : "v3.ux.openNav")}
            aria-expanded={railOpen} aria-controls="v3-navigation" onClick={() => setRailOpen((v) => !v)}>
            {railOpen ? <X size={18} /> : <Menu size={18} />}
          </button>
          <Link to="/v3" className="v3-brand">
            <span className="v3-brand-mark" aria-hidden="true" />
            Launchpad
            <sup>V3</sup>
          </Link>
          <button type="button" className="v3-cmdk-trigger" data-tour="cmdk" aria-label={t("v3.top.search")} title={t("v3.top.search")} onClick={() => setPaletteOpen(true)}>
            <Search size={15} aria-hidden="true" />
            <span>{t("v3.top.search")}</span>
            <kbd>⌘K</kbd>
          </button>
          <div className="v3-top-right">
            <WorkspaceSwitch />
            <Lang />
            <HelpMenu />
            <button type="button" className="v3-btn sm v3-version-switch" onClick={backToV2} data-testid="v3-switch-v2" aria-label={t("v3.switch.back")} title={t("v3.switch.back")}>
              <ArrowLeftRight size={13} aria-hidden="true" />
              <span>{t("v3.switch.back")}</span>
            </button>
            <span className="v3-chip v3-account" title={displayName}>
              {displayName}
            </span>
            {authRequired && (
              <button type="button" className="v3-btn ghost sm" onClick={() => void logout()} aria-label={`${t("auth.logout")} · ${displayName}`} title={displayName}>
                <LogOut size={14} />
              </button>
            )}
          </div>
        </header>

        <nav id="v3-navigation" className="v3-rail" aria-label={t("v3.nav.label")}>
          {NATIVE.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              // an agent's V2 management page (hosted) still belongs to Agents
              className={({ isActive: on }) => (on || (item.to === "/v3/agents" && managingAgent) ? "on" : "")}
            >
              {item.icon}
              {t(item.labelKey)}
              {item.to === "/v3/agents" && failing > 0 && <span className="count">{failing}</span>}
            </NavLink>
          ))}
          <div data-tour="modules" style={{ display: "grid", gap: 2 }}>
          <div className="v3-rail-label">{t("v3.nav.more")}</div>
          {groups.map((group) => {
            const current = group.items.some((item) => isActive(item, location.pathname, location.search));
            const isOpen = open[group.key] ?? current;
            return (
              <div key={group.key} className="v3-rail-group">
                <button
                  type="button"
                  className="v3-rail-item v3-rail-group-head"
                  aria-expanded={isOpen}
                  onClick={() => toggle(group.key, isOpen)}
                  data-testid={`v3-rail-group-${group.key}`}
                >
                  <ChevronRight size={14} className="chev" aria-hidden="true" />
                  {t(group.labelKey)}
                  <span className="ext">{group.items.length}</span>
                </button>
                {isOpen &&
                  group.items.map((item) => {
                    const Icon = item.icon;
                    const on = isActive(item, location.pathname, location.search);
                    return (
                      <Link
                        key={item.to}
                        to={item.to}
                        className={on ? "v3-rail-sub on" : "v3-rail-sub"}
                        aria-current={on ? "page" : undefined}
                        title={item.hintKey ? t(item.hintKey) : undefined}
                      >
                        <Icon size={15} aria-hidden="true" />
                        {t(item.labelKey)}
                      </Link>
                    );
                  })}
              </div>
            );
          })}
          <button type="button" className="v3-rail-mode" data-testid="v3-nav-mode"
            title={t("v3.onboard.mode.hint")}
            onClick={() => setNavMode(navMode === "business" ? "expert" : "business")}>
            {navMode === "business" ? t("v3.onboard.mode.showExpert") : t("v3.onboard.mode.showBusiness")}
          </button>
          </div>
          <div className="v3-rail-foot">
            <span className="mono" style={{ color: "var(--v3-text-3)", fontSize: 11 }}>
              {current ? `${current.region}` : ""}
            </span>
          </div>
        </nav>

        <main className={hosted ? "v3-main hosted" : "v3-main"}>
          {hosted === "v2" ? (
            <V2ToastProvider>
              {/* workspace-bound pages refetch on a switch; hub-global ones keep drafts */}
              {/* V2's per-module demo video rides in the hosted page header too */}
              <ModuleDemoProvider>
                <div
                  className="v2 v3-host"
                  key={HUB_GLOBAL.has(location.pathname) ? "hub-global-content" : current?.id ?? "none"}
                >
                  <RouteChunk key={location.pathname}>
                    <Outlet />
                  </RouteChunk>
                </div>
              </ModuleDemoProvider>
            </V2ToastProvider>
          ) : hosted === "classic" ? (
            <div
              className="view v3-classic"
              key={HUB_GLOBAL.has(location.pathname) ? "hub-global-content" : current?.id ?? "none"}
            >
              <RouteChunk key={location.pathname}>
                <Outlet />
              </RouteChunk>
            </div>
          ) : (
            <div key={HUB_GLOBAL.has(location.pathname) ? "hub-global-content" : current?.id ?? "none"}>
              <RouteChunk>
                <Outlet />
              </RouteChunk>
            </div>
          )}
        </main>

        <CommandPalette open={paletteOpen} onClose={closePalette} agents={agents.data?.agents ?? []} pages={[...pages, ...help]} />
        <GlossaryDialog />
        {touring && <Tour onClose={() => setTouring(false)} />}
      </ToastProvider>
    </div>
  );
}
