import { EyeOff, KeyRound, RefreshCw, ShieldOff } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ErrorCallout } from "../../../components/ErrorCallout";
import { type LoginSettings, type Provider, type ScimBlock, ssoAdminApi } from "../../../lib/api/sso-saml";
import { useFailure } from "../../failure";
import { ago } from "../../format";
import { useToast } from "../../hooks";
import { Reasoned } from "../../reason";
import { Btn, Chip, Confirm, Lamp, Notice, Panel } from "../../ui";
import { CopyPlate } from "./parts";

/**
 * SCIM provisioning for one provider (sso §2.4): the base URL, the bearer token —
 * minted here and shown exactly once, on a slip that says so — and what the IdP has
 * pushed so far. Rotating replaces the token; revoking turns SCIM off.
 */
export function ScimPanel({ provider, onScim, subject, onSubject }: {
  provider: Provider;
  onScim: (scim: ScimBlock) => void;
  subject: ScimBlock["subject"];
  onSubject: (value: ScimBlock["subject"]) => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const scim = provider.scim;
  const [token, setToken] = useState<string | null>(null);
  const [busy, setBusy] = useState<"mint" | "revoke" | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState(false);

  const mint = async () => {
    setBusy("mint");
    try {
      failed.clear();
      const answer = await ssoAdminApi.mintScimToken(provider.id);
      setToken(answer.token);
      onScim(answer.scim);
    } catch (err) {
      failed.fail(err);
    } finally {
      setBusy(null);
    }
  };
  const revoke = async () => {
    setBusy("revoke");
    try {
      failed.clear();
      const answer = await ssoAdminApi.revokeScimToken(provider.id);
      setToken(null);
      setConfirmRevoke(false);
      onScim(answer.scim);
      toast("ok", t("v3.sso.scim.revoked"));
    } catch (err) {
      failed.fail(err);
    } finally {
      setBusy(null);
    }
  };

  return (
    <Panel
      className="v3-sso-scim"
      signal={scim.enabled ? "ok" : undefined}
      title={(
        <span className="v3-sso-scim-title">
          <Lamp s={scim.enabled ? "ok" : "off"} live={scim.enabled} />
          {t("v3.sso.section.scim")}
        </span>
      )}
      end={scim.enabled ? <Chip s="ok">{t("v3.sso.scim.on")}</Chip> : <Chip s="off">{t("v3.sso.scim.off")}</Chip>}
    >
      {failed.error != null && (
        <div style={{ marginBottom: 14 }}>
          <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-sso-scim-error" />
        </div>
      )}
      <div className="v3-sso-scim-body">
        <div className="lane">
          <p className="v3-sso-muted">{t(`v3.sso.scim.body.${provider.preset}`, { defaultValue: t("v3.sso.scim.body.default") })}</p>
          <CopyPlate label={t("v3.sso.scim.baseUrl")} value={scim.base_url} testId="sso-scim-url" />
          {token && (
            <div className="v3-sso-slip" role="status" data-testid="sso-scim-token">
              <span className="band">{t("v3.sso.scim.once")}</span>
              <CopyPlate label={t("v3.sso.scim.token")} value={token} />
            </div>
          )}
          <div className="v3-sso-scim-actions">
            <Btn kind={scim.enabled ? "ghost" : "primary"} size="sm" onClick={() => void mint()} disabled={busy !== null}>
              {scim.enabled
                ? <><RefreshCw size={13} className={busy === "mint" ? "spin" : undefined} aria-hidden="true" /> {t("v3.sso.scim.rotate")}</>
                : <><KeyRound size={13} aria-hidden="true" /> {t("v3.sso.scim.mint")}</>}
            </Btn>
            {scim.enabled && (
              <Btn kind="ghost" size="sm" onClick={() => setConfirmRevoke(true)} disabled={busy !== null}>
                <ShieldOff size={13} aria-hidden="true" /> {t("v3.sso.scim.revoke")}
              </Btn>
            )}
            {scim.enabled && scim.token_hint && (
              <small className="v3-sso-muted mono">
                {t("v3.sso.scim.hint", { hint: scim.token_hint, when: ago(scim.token_created_at) })}
              </small>
            )}
          </div>
          <label className="v3-field v3-sso-claim">
            <span>{t("v3.sso.scim.subject")}</span>
            <select className="v3-select mono" value={subject} onChange={(e) => onSubject(e.target.value as ScimBlock["subject"])}>
              <option value="userName">userName</option>
              <option value="externalId">externalId</option>
            </select>
          </label>
          <small className="v3-hint">{t("v3.sso.scim.subjectHint")}</small>
        </div>
        <dl className="v3-sso-counts" aria-label={t("v3.sso.scim.pushed")}>
          <div><dt>{t("v3.sso.scim.users")}</dt><dd>{scim.counts?.users ?? 0}</dd></div>
          <div><dt>{t("v3.sso.scim.groups")}</dt><dd>{scim.counts?.groups ?? 0}</dd></div>
        </dl>
      </div>
      {confirmRevoke && (
        <Confirm
          danger
          title={t("v3.sso.scim.revokeTitle")}
          confirmLabel={t("v3.sso.scim.revoke")}
          cancelLabel={t("common.cancel")}
          busy={busy === "revoke"}
          onCancel={() => setConfirmRevoke(false)}
          onConfirm={() => void revoke()}
        >
          {t("v3.sso.scim.revokeBody")}
        </Confirm>
      )}
    </Panel>
  );
}

/**
 * The console-wide sign-in switch (D-2), drawn as a miniature of the login card: the
 * IdP keys on top and the dashed "username and password" seam beneath, which folds
 * away when local login is hidden — the admin sees the page their users will.
 */
export function LoginPageSwitch({ settings, enabledCount, onChange }: {
  settings: LoginSettings;
  enabledCount: number;
  onChange: (next: LoginSettings) => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const [busy, setBusy] = useState(false);
  const hidden = settings.hide_local_login;
  const flip = async () => {
    setBusy(true);
    try {
      failed.clear();
      onChange(await ssoAdminApi.putSettings({ hide_local_login: !hidden }));
      toast("ok", t(hidden ? "v3.sso.loginPage.shownToast" : "v3.sso.loginPage.hiddenToast"));
    } catch (err) {
      failed.fail(err);
    } finally {
      setBusy(false);
    }
  };
  const inert = hidden && !settings.local_login_hidden;
  return (
    <section className="v3-sso-loginpage" data-hidden={hidden} aria-label={t("v3.sso.loginPage.title")}>
      <div className="mini" aria-hidden="true">
        <span className="key" />
        <span className="key" />
        <span className="seam"><i /></span>
        <span className="field" />
        <span className="field" />
      </div>
      <div className="txt">
        <div className="v3-sso-kick">{t("v3.sso.loginPage.kick")}</div>
        <b>{t(hidden ? "v3.sso.loginPage.hiddenTitle" : "v3.sso.loginPage.shownTitle")}</b>
        <p className="v3-sso-muted">{t(hidden ? "v3.sso.loginPage.hiddenBody" : "v3.sso.loginPage.shownBody")}</p>
        {inert && <Notice s="wait">{t("v3.sso.loginPage.inert")}</Notice>}
        <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-sso-loginpage-error" />
      </div>
      <label className="v3-sso-toggle big" data-on={hidden}>
        <Reasoned disabledReason={!hidden && enabledCount === 0 ? t("v3.sso.loginPage.needsProvider") : null}>
          <input type="checkbox" role="switch" checked={hidden} disabled={busy || (!hidden && enabledCount === 0)}
            onChange={() => void flip()} data-testid="sso-hide-local" />
        </Reasoned>
        <span className="track" aria-hidden="true" />
        <span className="txt">
          <b><EyeOff size={13} aria-hidden="true" /> {t("v3.sso.loginPage.toggle")}</b>
          <small>{enabledCount === 0 && !hidden ? t("v3.sso.loginPage.needsProvider") : t("v3.sso.loginPage.breakGlass")}</small>
        </span>
      </label>
    </section>
  );
}
