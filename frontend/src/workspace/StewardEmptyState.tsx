import { LogOut, ShieldCheck } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import { V3AuthFrame } from "../v3/AuthFrame";
import { PageHead, Panel } from "../v3/ui";

const LINKS: { to: string; labelKey: string }[] = [
  { to: "/v3/users", labelKey: "v3.steward.links.users" },
  { to: "/v3/admins", labelKey: "v3.steward.links.admins" },
  { to: "/v3/projects", labelKey: "v3.steward.links.projects" },
  { to: "/v3/departments", labelKey: "v3.steward.links.departments" },
  { to: "/v2/workspaces", labelKey: "v3.steward.links.workspaces" },
];

function Body() {
  const { t } = useTranslation();
  return (
    <Panel signal="info">
      <div className="v3-nows-state" data-testid="steward-empty-state">
        <span className="ico"><ShieldCheck size={22} aria-hidden="true" /></span>
        <div style={{ display: "grid", gap: 10 }}>
          <b>{t("v3.steward.empty.title")}</b>
          <p style={{ margin: 0 }}>{t("v3.steward.empty.body")}</p>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {LINKS.map((link) => (
              <Link key={link.to} to={link.to} className="v3-btn sm">{t(link.labelKey)}</Link>
            ))}
          </div>
        </div>
      </div>
    </Panel>
  );
}

/**
 * What a steward sees on a business page (§11.4): "your role manages structure
 * only, it does not access project data", and the way to the configuration pages.
 * `page` renders it on the sign-in chrome with signing out — for the gates that
 * would otherwise say "no workspace / no project" (a steward holds no grant).
 */
export function StewardEmptyState({ page = false }: { page?: boolean }) {
  const { t } = useTranslation();
  const { authRequired, username, logout } = useAuth();
  const head = (
    <PageHead eyebrow={t("v3.steward.empty.eyebrow")} title={t("v3.steward.empty.heading")}
      sub={t("v3.steward.empty.meta", { username: username ?? "—" })} />
  );
  if (!page) {
    return (
      <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
        {head}
        <Body />
      </div>
    );
  }
  const logoutButton = authRequired ? (
    <button type="button" className="v3-btn sm" onClick={() => void logout()} data-testid="steward-logout">
      <LogOut size={13} aria-hidden="true" />
      {t("auth.logout")}
    </button>
  ) : null;
  return (
    <V3AuthFrame testId="steward-page" end={logoutButton}>
      <div className="v3-nows">
        {head}
        <Body />
      </div>
    </V3AuthFrame>
  );
}
