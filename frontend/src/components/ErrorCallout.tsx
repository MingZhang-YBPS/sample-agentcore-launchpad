import "../v3/story/story.css";

import { AlertTriangle, ArrowRight, KeyRound, X, XCircle } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import { ApiError, apiErrorFacts, errorMessage } from "../lib/api";
import { type ActionParams, errorNext } from "../v3/story/actions";
import { ApiErrorDetail } from "./ApiErrorDetail";

/** Who usually holds a permission a `needs` names (guided-console §3.3). */
const HOLDER: Record<string, string> = {
  "criteria.sign": "biz-owner",
  "golden.admit": "biz-owner",
  "judge.calibrate": "biz-owner",
  "release.sign": "release-ops",
  "waiver.approve": "release-ops",
  "promotion.approve": "release-ops",
  "mcp.approve": "release-ops",
  "criteria.manage": "developer",
  "eval.run": "developer",
  "agents.deploy": "developer",
};

interface ErrorCalloutProps {
  /** what the catch branch got; null / "" renders nothing */
  error: unknown;
  /** parameters the page already knows (the selected agent), under the backend's own */
  params?: ActionParams;
  onDismiss?: () => void;
  "data-testid"?: string;
}

/**
 * An error that stays where it happened and says how to get out (guided-console
 * §3.4): **what went wrong** (`apiErrors.<code>`), **why** (optional
 * `apiErrors.<code>_why`), and **where to fix it** — the envelope's `next` through
 * the action table — plus, when only a permission can fix it, who usually holds it
 * (the Users link only for administrators, who can grant it). Replaces the toast
 * at a page's main actions; it stays until the user acts again or dismisses it.
 */
export function ErrorCallout({ error, params, onDismiss, "data-testid": testId = "error-callout" }: ErrorCalloutProps) {
  const { t, i18n } = useTranslation();
  const { isAdmin } = useAuth();
  if (error == null || error === "") return null;
  const api = error instanceof ApiError ? error : null;
  const headline = api ? api.title : typeof error === "string" ? error : errorMessage(error);
  const whyKey = api ? `apiErrors.${api.code}_why` : null;
  const why = whyKey && i18n.exists(whyKey) ? t(whyKey) : null;
  const actions = api ? errorNext(api, params) : [];
  const perm = api?.needs?.replace(/^perm:/, "") ?? null;
  const holder = perm ? HOLDER[perm] : undefined;
  // the permission's absence is not the user's mistake: amber, not red
  const signal = perm && actions.length === 0 ? "wait" : "act";
  const Icon = signal === "wait" ? AlertTriangle : XCircle;

  return (
    <div className="v3-callout" data-s={signal} role="alert" data-testid={testId}
      data-code={api?.code} key={api ? `${api.code}:${api.at.getTime()}` : headline}>
      <Icon className="v3-callout-icon" size={16} aria-hidden="true" />
      <div className="v3-callout-body">
        <div className="v3-callout-what" data-testid={`${testId}-what`}>
          {api && apiErrorFacts(api) ? <ApiErrorDetail error={api} data-testid={`${testId}-detail`} /> : headline}
        </div>
        {why && <p className="v3-callout-why" data-testid={`${testId}-why`}>{why}</p>}
        {actions.length > 0 && (
          <div className="v3-callout-next" data-testid={`${testId}-next`}>
            <span className="v3-callout-tag">{t("story.callout.next")}</span>
            {actions.map((a, i) => (
              <Link key={`${a.key}:${i}`} to={a.to} data-action={a.key}
                className={["v3-btn", "sm", i === 0 ? "primary" : "ghost"].join(" ")}>
                {t(a.label)}
                {i === 0 && <ArrowRight size={13} aria-hidden="true" />}
              </Link>
            ))}
          </div>
        )}
        {perm && (
          <div className="v3-callout-needs" data-testid={`${testId}-needs`}>
            <KeyRound size={13} aria-hidden="true" />
            <span>
              {holder
                ? t("story.callout.needs", { perm, role: t(`story.role.${holder}`) })
                : t("story.callout.needsAnyone", { perm })}
              {" "}
              {t(isAdmin ? "story.callout.grantAdmin" : "story.callout.grantAsk")}
            </span>
            {isAdmin && (
              <Link to="/v3/users" className="v3-callout-link" data-testid={`${testId}-users`}>
                {t("story.callout.toUsers")} <ArrowRight size={12} aria-hidden="true" />
              </Link>
            )}
          </div>
        )}
      </div>
      {onDismiss && (
        <button type="button" className="v3-btn ghost sm v3-callout-close" aria-label={t("story.callout.dismiss")}
          title={t("story.callout.dismiss")} onClick={onDismiss}>
          <X size={13} aria-hidden="true" />
        </button>
      )}
    </div>
  );
}
