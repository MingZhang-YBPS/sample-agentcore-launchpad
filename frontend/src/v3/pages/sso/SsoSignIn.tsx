import "./sso.css";

import { ArrowRight, ChevronDown, KeyRound } from "lucide-react";
import { type ReactNode, useEffect, useId, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  clearSsoError,
  readSsoError,
  ssoApi,
  type SsoPublicProvider,
  ssoStartUrl,
  wantsLocalLogin,
} from "../../../lib/api/sso";
import { ssoErrorKey } from "./model";

/** Where to land after the IdP: the page the browser asked for, minus our own params. */
function currentReturnTo(): string {
  const url = new URL(window.location.href);
  url.searchParams.delete("sso_error");
  url.searchParams.delete("sso_provider");
  url.searchParams.delete("local");
  return `${url.pathname}${url.search}`;
}

/** The provider's mark: its preset's initial in a keyed plate. */
function Mark({ preset }: { preset: string }) {
  const { t } = useTranslation();
  return (
    <span className="v3-sso-mark" data-preset={preset} aria-hidden="true">
      {t(`auth.sso.presetShort.${preset}`, { defaultValue: "ID" })}
    </span>
  );
}

/**
 * The sign-in card's single sign-on half (sso §2.7): one button per enabled IdP on
 * top, the username / password form (`children`) folded under a disclosure. With no
 * provider it renders the form alone, exactly as before; `?local=1` opens it.
 */
export function SsoSignIn({ skin, children }: {
  skin: "v2" | "v3";
  /** the local form; `nested` — it sits under the IdP buttons, so it drops its heading */
  children: (nested: boolean) => ReactNode;
}) {
  const { t } = useTranslation();
  const [providers, setProviders] = useState<SsoPublicProvider[] | null>(null);
  const [error] = useState(() => readSsoError());
  const [localOpen, setLocalOpen] = useState(() => wantsLocalLogin());
  const [leaving, setLeaving] = useState<string | null>(null);
  const panelId = useId();

  useEffect(clearSsoError, []);
  useEffect(() => {
    let live = true;
    ssoApi
      .providers()
      .then((answer) => live && setProviders(answer.providers))
      .catch(() => live && setProviders([]));
    return () => {
      live = false;
    };
  }, []);

  // nothing to choose from (still loading, or no IdP configured): the form, as before
  if (!providers?.length) {
    return (
      <>
        {error && <SsoError code={error.code} skin={skin} />}
        {children(false)}
      </>
    );
  }

  const go = (provider: SsoPublicProvider) => {
    setLeaving(provider.id);
    window.location.assign(ssoStartUrl(provider.id, currentReturnTo()));
  };

  return (
    <div className="v3-sso-signin" data-testid="sso-signin">
      <h2>{t("auth.title")}</h2>
      <p className="sub">{t("auth.sso.subtitle")}</p>
      {error && <SsoError code={error.code} skin={skin} />}
      <ul className="v3-sso-idps" aria-label={t("auth.sso.providersLabel")}>
        {providers.map((provider, index) => (
          <li key={provider.id} style={{ ["--i" as string]: index }}>
            <button
              type="button"
              className={`v3-sso-idp ${skin}-sso-idp`}
              onClick={() => go(provider)}
              disabled={leaving !== null}
              data-testid={`sso-idp-${provider.id}`}
              aria-busy={leaving === provider.id}
            >
              <Mark preset={provider.preset} />
              <span className="txt">
                <span className="kick">{t(`auth.sso.preset.${provider.preset}`, { defaultValue: "OIDC" })}</span>
                <span className="name">{t("auth.sso.continueWith", { name: provider.name })}</span>
              </span>
              <ArrowRight className="go" size={16} aria-hidden="true" />
            </button>
          </li>
        ))}
      </ul>

      <div className="v3-sso-local" data-open={localOpen}>
        <button
          type="button"
          className="v3-sso-local-toggle"
          aria-expanded={localOpen}
          aria-controls={panelId}
          onClick={() => setLocalOpen((open) => !open)}
          data-testid="sso-local-toggle"
        >
          <KeyRound size={13} aria-hidden="true" />
          <span>{t("auth.sso.useLocal")}</span>
          <ChevronDown className="chev" size={14} aria-hidden="true" />
        </button>
        <div id={panelId} className="v3-sso-local-body" hidden={!localOpen}>
          {localOpen && children(true)}
        </div>
      </div>
    </div>
  );
}

function SsoError({ code, skin }: { code: string; skin: "v2" | "v3" }) {
  const { t } = useTranslation();
  return (
    <div className={`v3-sso-error ${skin}-auth-error`} role="alert" data-testid="sso-error">
      <b>{t(ssoErrorKey(code))}</b>
      <span>{t("auth.sso.errors.contactAdmin")}</span>
      <code>{code}</code>
    </div>
  );
}
