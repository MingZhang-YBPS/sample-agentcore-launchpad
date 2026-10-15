import { ArrowLeftRight, Download, FileUp, Link2, RefreshCw, ShieldCheck } from "lucide-react";
import { type ChangeEvent, useId, useState } from "react";
import { useTranslation } from "react-i18next";

import { ErrorCallout } from "../../../components/ErrorCallout";
import { NAMEID_FORMATS, type Provider, ssoAdminApi } from "../../../lib/api/sso-saml";
import { useFailure } from "../../failure";
import { useToast } from "../../hooks";
import { Btn, Chip, Lamp, Notice, Panel } from "../../ui";
import { type ProviderDraft, daysLeft, type DraftProblem, samlTestRows } from "./model";
import { CopyPlate } from "./parts";

/** A certificate's expiry as a chip: ok, due within 30 days, or expired. */
export function CertExpiry({ notAfter, error }: { notAfter?: string; error?: string }) {
  const { t } = useTranslation();
  if (error) return <Chip s="act">{t("v3.sso.saml.certBad")}</Chip>;
  const days = daysLeft(notAfter);
  if (days == null) return null;
  const s = days < 0 ? "act" : days < 30 ? "wait" : "ok";
  return (
    <Chip s={s} title={notAfter}>
      {days < 0 ? t("v3.sso.saml.certExpired") : t("v3.sso.saml.certDays", { count: days })}
    </Chip>
  );
}

/**
 * The SAML handoff (sso §2.7 一键复制): what the console gives the IdP on the left,
 * what the IdP gives back — its metadata, by URL or file — on the right, a seam
 * between them. The right side is the one action; once metadata is loaded it shows
 * what was read, so the exchange reads as done.
 */
export function SamlExchange({ provider, onChanged }: { provider: Provider; onChanged: (next: Provider) => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const saml = provider.saml!;
  const [url, setUrl] = useState(saml.metadata_url);
  const [busy, setBusy] = useState<"url" | "xml" | "key" | null>(null);
  const fileId = useId();
  const received = Boolean(saml.idp_entity_id && saml.sso_url && saml.idp_certs.length);

  const load = async (kind: "url" | "xml", source: { url?: string; xml?: string }) => {
    setBusy(kind);
    try {
      failed.clear();
      onChanged(await ssoAdminApi.loadMetadata(provider.id, source));
      toast("ok", t("v3.sso.saml.loaded"));
    } catch (err) {
      failed.fail(err);
    } finally {
      setBusy(null);
    }
  };
  const pick = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    void file.text().then((xml) => load("xml", { xml }));
  };
  const rotate = async () => {
    setBusy("key");
    try {
      failed.clear();
      onChanged(await ssoAdminApi.rotateSpKey(provider.id));
      toast("ok", t("v3.sso.saml.rotated"));
    } catch (err) {
      failed.fail(err);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="v3-sso-exchange" aria-label={t("v3.sso.section.handoff")} data-received={received}>
      <div className="side give">
        <div className="v3-sso-kick">{t("v3.sso.saml.give")}</div>
        <p>{t(`v3.sso.handoffBody.${provider.preset}`, { defaultValue: t("v3.sso.handoffBody.saml") })}</p>
        <CopyPlate label={t("v3.sso.saml.spEntityId")} value={saml.sp_entity_id} testId="sso-sp-entity" />
        <CopyPlate label={t("v3.sso.saml.acsUrl")} value={saml.acs_url} testId="sso-acs-url" />
        <CopyPlate label={t("v3.sso.saml.slsUrl")} value={saml.sls_url} />
        <div className="v3-sso-sp-cert">
          <ShieldCheck size={14} aria-hidden="true" />
          <span>{t("v3.sso.saml.spCert")}</span>
          {saml.sp_cert_facts && <CertExpiry notAfter={saml.sp_cert_facts.not_after} error={saml.sp_cert_facts.error} />}
          <code title={saml.sp_cert_facts?.sha256}>{saml.sp_cert_facts?.sha256?.slice(0, 23) ?? "—"}</code>
          <span className="end">
            <a className="v3-btn ghost sm" href={saml.metadata_endpoint} target="_blank" rel="noreferrer" download>
              <Download size={13} aria-hidden="true" /> {t("v3.sso.saml.downloadMetadata")}
            </a>
            <Btn size="sm" kind="ghost" onClick={() => void rotate()} disabled={busy !== null} title={t("v3.sso.saml.rotateHint")}>
              <RefreshCw size={13} className={busy === "key" ? "spin" : undefined} aria-hidden="true" /> {t("v3.sso.saml.rotate")}
            </Btn>
          </span>
        </div>
      </div>

      <div className="seam" aria-hidden="true"><ArrowLeftRight size={14} /></div>

      <div className="side take">
        <div className="v3-sso-kick">{t("v3.sso.saml.take")}</div>
        <form
          className="v3-sso-intake"
          onSubmit={(e) => {
            e.preventDefault();
            if (url.trim()) void load("url", { url: url.trim() });
          }}
        >
          <label className="v3-field">
            <span>{t("v3.sso.saml.metadataUrl")}</span>
            <span className="row">
              <input className="v3-input mono" value={url} spellCheck={false} placeholder="https://…"
                onChange={(e) => setUrl(e.target.value)} data-testid="sso-metadata-url" />
              <Btn size="sm" type="submit" disabled={busy !== null || !url.trim()}>
                <Link2 size={13} aria-hidden="true" /> {t("v3.sso.saml.fetch")}
              </Btn>
            </span>
          </label>
          <div className="or"><span>{t("v3.sso.saml.or")}</span></div>
          <label className="v3-sso-drop" htmlFor={fileId} data-busy={busy === "xml"}>
            <FileUp size={16} aria-hidden="true" />
            <span><b>{t("v3.sso.saml.upload")}</b><small>{t("v3.sso.saml.uploadHint")}</small></span>
            <input id={fileId} type="file" accept=".xml,application/xml,text/xml,application/samlmetadata+xml"
              onChange={pick} disabled={busy !== null} />
          </label>
        </form>
        <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-sso-saml-error" />
        {received ? (
          <dl className="v3-sso-received" data-testid="sso-received">
            <div><dt>{t("v3.sso.saml.idpEntityId")}</dt><dd className="mono">{saml.idp_entity_id}</dd></div>
            <div><dt>{t("v3.sso.saml.ssoUrl")}</dt><dd className="mono">{saml.sso_url}</dd></div>
            <div>
              <dt>{t("v3.sso.saml.idpCerts", { count: saml.idp_certs.length })}</dt>
              <dd>{saml.idp_certs.map((c) => <CertExpiry key={c.body.slice(-16)} notAfter={c.not_after} error={c.error} />)}</dd>
            </div>
          </dl>
        ) : (
          <p className="v3-sso-muted">{t("v3.sso.saml.awaiting")}</p>
        )}
      </div>
    </section>
  );
}

/** The SAML connection fields: the IdP side by hand, and the request options. */
export function SamlConnection({ provider, draft, problems, set }: {
  provider: Provider;
  draft: ProviderDraft;
  problems: DraftProblem[];
  set: <K extends keyof ProviderDraft>(key: K, value: ProviderDraft[K]) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="v3-sso-form">
      <label className="v3-field">
        <span>{t("v3.sso.field.name")}</span>
        <input className="v3-input" value={draft.name} maxLength={64} onChange={(e) => set("name", e.target.value)} />
      </label>
      <label className="v3-field">
        <span>{t("v3.sso.saml.idpEntityId")}</span>
        <input className="v3-input mono" value={draft.saml_idp_entity_id} spellCheck={false}
          onChange={(e) => set("saml_idp_entity_id", e.target.value)} />
      </label>
      <label className="v3-field">
        <span>{t("v3.sso.saml.ssoUrl")}</span>
        <input className="v3-input mono" value={draft.saml_sso_url} spellCheck={false}
          aria-invalid={problems.includes("saml_sso_url")} onChange={(e) => set("saml_sso_url", e.target.value)} />
        {problems.includes("saml_sso_url") && <small className="v3-err">{t("v3.sso.problem.issuer")}</small>}
      </label>
      <label className="v3-field">
        <span>{t("v3.sso.saml.sloUrl")}</span>
        <input className="v3-input mono" value={draft.saml_slo_url} spellCheck={false} placeholder={t("v3.sso.saml.sloNone")}
          aria-invalid={problems.includes("saml_slo_url")} onChange={(e) => set("saml_slo_url", e.target.value)} />
        <small className="v3-hint">{t("v3.sso.saml.sloHint")}</small>
      </label>
      <label className="v3-field">
        <span>{t("v3.sso.saml.certsField")}</span>
        <textarea className="v3-input mono v3-sso-pem" rows={4} value={draft.saml_idp_certs} spellCheck={false}
          placeholder="-----BEGIN CERTIFICATE-----" onChange={(e) => set("saml_idp_certs", e.target.value)} />
        <small className="v3-hint">{t("v3.sso.saml.certsHint")}</small>
      </label>
      <label className="v3-field">
        <span>{t("v3.sso.saml.nameIdFormat")}</span>
        <select className="v3-select mono" value={draft.saml_name_id_format}
          onChange={(e) => set("saml_name_id_format", e.target.value)}>
          <option value="">{t("v3.sso.saml.nameIdAny")}</option>
          {NAMEID_FORMATS.map((f) => <option key={f} value={f}>{f.split(":").at(-1)}</option>)}
        </select>
      </label>
      <div className="v3-sso-toggles">
        {([
          ["saml_allow_idp_initiated", "idpInitiated"],
          ["saml_sign_requests", "signRequests"],
          ["saml_want_encrypted", "wantEncrypted"],
        ] as const).map(([key, label]) => (
          <label key={key} className="v3-sso-toggle" data-on={draft[key]}>
            <input type="checkbox" role="switch" checked={draft[key]} onChange={(e) => set(key, e.target.checked)} />
            <span className="track" aria-hidden="true" />
            <span className="txt"><b>{t(`v3.sso.saml.${label}`)}</b><small>{t(`v3.sso.saml.${label}Body`)}</small></span>
          </label>
        ))}
      </div>
      {provider.preset === "identity-center" && draft.saml_want_encrypted && (
        <Notice s="wait">{t("v3.sso.saml.encryptedIcNote")}</Notice>
      )}
    </div>
  );
}

/** The SAML connection test as a checklist. */
export function SamlTestPanel({ provider, busy, onTest }: { provider: Provider; busy: boolean; onTest: () => void }) {
  const { t } = useTranslation();
  const result = provider.last_test;
  return (
    <Panel
      title={t("v3.sso.section.test")}
      end={(
        <Btn size="sm" onClick={onTest} disabled={busy}>
          <RefreshCw size={13} className={busy ? "spin" : undefined} aria-hidden="true" /> {t("v3.sso.test")}
        </Btn>
      )}
    >
      {!result ? (
        <p className="v3-sso-muted">{t("v3.sso.testNever")}</p>
      ) : (
        <div style={{ display: "grid", gap: 12 }}>
          <ol className="v3-sso-checks" data-testid="sso-test-rows">
            {samlTestRows(result).map((row) => (
              <li key={row.key} data-s={row.s}>
                <Lamp s={row.s} />
                <span className="what">{t(`v3.sso.check.${row.key}.${row.s}`, { defaultValue: t(`v3.sso.check.${row.key}.label`) })}</span>
                {row.detail && <code>{row.detail}</code>}
              </li>
            ))}
          </ol>
          {result.error && <Notice s="act">{result.error.message} <code className="mono">{result.error.code}</code></Notice>}
        </div>
      )}
    </Panel>
  );
}
