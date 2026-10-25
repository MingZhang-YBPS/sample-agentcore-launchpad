import "./sso.css";

import { useTranslation } from "react-i18next";

import { ssoApi } from "../../../lib/api/sso";
import { ago } from "../../format";
import { useLoad } from "../../hooks";
import { Chip, Lamp } from "../../ui";

/**
 * The user page's account source (sso §2.7): local, or the SSO provider with the
 * external subject, the last SSO sign-in and a "differs from the SSO mapping" mark
 * when an administrator's role overrides what the mapping computes. Pass the row's
 * `source` from `GET /api/users/{id}` so a local account costs no request.
 */
export function SsoAccountSource({ userId, source }: { userId: string; source?: "local" | "sso" | null }) {
  const { t } = useTranslation();
  const sso = source === "sso";
  const loaded = useLoad(
    () => (sso ? ssoApi.account(userId) : Promise.resolve(null)),
    `sso-account-${userId}-${sso}`,
  );
  if (!sso) {
    return (
      <span className="v3-sso-source" data-testid="sso-account-source">
        <Chip>{t("v3.sso.account.local")}</Chip>
      </span>
    );
  }
  const facts = loaded.data?.sso;
  return (
    <div className="v3-sso-source" data-testid="sso-account-source">
      <Chip s="info">{t("v3.sso.account.sso", { name: facts?.provider_name ?? facts?.provider_id ?? "…" })}</Chip>
      {facts?.subject && <code title={t("v3.sso.account.subject")}>{facts.subject}</code>}
      {facts?.last_login_at && <small>{t("v3.sso.account.lastLogin", { when: ago(facts.last_login_at) })}</small>}
      {facts?.role_conflict && (
        <span className="conflict">
          <Lamp s="wait" />
          {t("v3.sso.account.conflict", { role: t(`v3.sso.role.${facts.role_conflict}`) })}
        </span>
      )}
    </div>
  );
}
