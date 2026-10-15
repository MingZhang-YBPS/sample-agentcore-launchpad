import "./sso.css";

import { Plus, Power, RefreshCw, Trash2 } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { errorMessage } from "../../../lib/api";
import { projectGroupsApi } from "../../../lib/api/project-groups";
import { projectsApi } from "../../../lib/api/projects";
import { PRIMARY_PRESETS, ssoApi, type SsoLogin, type SsoPreset, SSO_ROLES } from "../../../lib/api/sso";
import {
  type AnyPreset,
  CHANNEL_PRESETS,
  type LoginSettings,
  type PresetTemplate,
  type Provider,
  ssoAdminApi,
  TEMPLATE_PRESETS,
} from "../../../lib/api/sso-saml";
import { ago } from "../../format";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Confirm, Dialog, Empty, Lamp, Notice, PageHead, Panel, Skeleton } from "../../ui";
import { MappingsEditor, type MappingTargets } from "./MappingsEditor";
import {
  canEnable,
  createBody,
  draftOf,
  draftProblems,
  isDirty,
  patchOf,
  type ProviderDraft,
  providerSignal,
  testRows,
} from "./model";
import { CopyPlate, PresetMark } from "./parts";
import { LoginPageSwitch, ScimPanel } from "./Provisioning";
import { SamlConnection, SamlExchange, SamlTestPanel } from "./SamlParts";

export { CopyPlate, PresetMark } from "./parts";

type Presets = Partial<Record<AnyPreset, PresetTemplate>>;

/**
 * The preset chooser: one lane per channel — SAML 2.0 (Identity Center, Entra, any
 * SAML IdP) and OpenID Connect (Keycloak, Authentik, any OIDC IdP) — as large tiles
 * with configuration guides, the attribute templates in a row beneath.
 */
export function PresetGrid({ value, onPick }: { value: AnyPreset | null; onPick: (preset: AnyPreset) => void }) {
  const { t } = useTranslation();
  let index = 0;
  const tile = (preset: AnyPreset) => (
    <button
      key={preset}
      type="button"
      className={`v3-sso-preset${value === preset ? " on" : ""}`}
      aria-pressed={value === preset}
      onClick={() => onPick(preset)}
      style={{ ["--i" as string]: index++ }}
      data-testid={`sso-preset-${preset}`}
    >
      <PresetMark preset={preset} size="lg" />
      <b>{t(`auth.sso.preset.${preset}`)}</b>
      <small>{t(`v3.sso.presetBody.${preset}`)}</small>
    </button>
  );
  return (
    <div className="v3-sso-presets">
      {(["saml", "oidc"] as const).map((kind) => (
        <div key={kind} className="v3-sso-channel" data-kind={kind}>
          <div className="v3-sso-channel-label">
            <span className="v3-sso-kick">{t(`v3.sso.channel.${kind}`)}</span>
            <small>{t(`v3.sso.channelBody.${kind}`)}</small>
          </div>
          <div className="v3-sso-preset-row primary">{CHANNEL_PRESETS[kind].map(tile)}</div>
        </div>
      ))}
      <div className="v3-sso-preset-sep"><span>{t("v3.sso.templatesLabel")}</span></div>
      <div className="v3-sso-preset-row">{TEMPLATE_PRESETS.map(tile)}</div>
    </div>
  );
}

function CreateDialog({ presets, initial, onClose, onCreated }: {
  presets: Presets | undefined;
  initial: AnyPreset | null;
  onClose: () => void;
  onCreated: (provider: Provider) => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const [preset, setPreset] = useState<AnyPreset | null>(initial);
  const [name, setName] = useState(initial ? t(`v3.sso.defaultName.${initial}`) : "");
  const [busy, setBusy] = useState(false);
  const pick = (next: AnyPreset) => {
    setPreset(next);
    setName((current) => current && preset && current !== t(`v3.sso.defaultName.${preset}`) ? current : t(`v3.sso.defaultName.${next}`));
  };
  const create = async () => {
    if (!preset || !name.trim()) return;
    setBusy(true);
    try {
      const created = await ssoAdminApi.create(createBody(preset, name, presets?.[preset]));
      toast("ok", t("v3.sso.created", { name: created.name }));
      onCreated(created);
    } catch (err) {
      toast("act", errorMessage(err), err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog
      wide
      title={t("v3.sso.createTitle")}
      onClose={onClose}
      foot={(
        <>
          <Btn kind="ghost" onClick={onClose} disabled={busy}>{t("common.cancel")}</Btn>
          <Btn kind="primary" onClick={() => void create()} disabled={busy || !preset || !name.trim()}>
            {t("v3.sso.create")}
          </Btn>
        </>
      )}
    >
      <div style={{ display: "grid", gap: 16 }}>
        <PresetGrid value={preset} onPick={pick} />
        <label className="v3-field">
          <span>{t("v3.sso.field.name")}</span>
          <input className="v3-input" value={name} maxLength={64} onChange={(e) => setName(e.target.value)} />
          <small className="v3-hint">{t("v3.sso.hint.name")}</small>
        </label>
      </div>
    </Dialog>
  );
}

function ProviderRail({ items, selected, onSelect }: {
  items: Provider[];
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <nav className="v3-sso-rail" aria-label={t("v3.sso.listLabel")}>
      {items.map((item) => (
        <button
          key={item.id}
          type="button"
          className={`v3-sso-rail-item${item.id === selected ? " on" : ""}`}
          aria-current={item.id === selected ? "true" : undefined}
          onClick={() => onSelect(item.id)}
          data-testid={`sso-rail-${item.id}`}
        >
          <PresetMark preset={item.preset} />
          <span className="txt">
            <b>{item.name}</b>
            <small className="mono">{item.kind.toUpperCase()} · {item.id}</small>
          </span>
          <span className="state">
            <Lamp s={providerSignal(item)} />
            <small>{t(`v3.sso.state.${providerSignal(item)}`)}</small>
          </span>
        </button>
      ))}
    </nav>
  );
}

function TestPanel({ provider, busy, onTest }: { provider: Provider; busy: boolean; onTest: () => void }) {
  const { t } = useTranslation();
  const result = provider.last_test;
  return (
    <Panel
      title={t("v3.sso.section.test")}
      end={(
        <Btn size="sm" onClick={onTest} disabled={busy || !provider.issuer}>
          <RefreshCw size={13} className={busy ? "spin" : undefined} aria-hidden="true" /> {t("v3.sso.test")}
        </Btn>
      )}
    >
      {!result ? (
        <p className="v3-sso-muted">{t(provider.issuer ? "v3.sso.testNever" : "v3.sso.testNeedsIssuer")}</p>
      ) : (
        <div style={{ display: "grid", gap: 12 }}>
          <ol className="v3-sso-checks" data-testid="sso-test-rows">
            {testRows(result).map((row) => (
              <li key={row.key} data-s={row.s}>
                <Lamp s={row.s} />
                <span className="what">{t(`v3.sso.check.${row.key}.${row.s}`, { defaultValue: t(`v3.sso.check.${row.key}.label`) })}</span>
                {row.detail && <code>{row.detail}</code>}
              </li>
            ))}
          </ol>
          {result.error && <Notice s="act">{result.error.message} <code className="mono">{result.error.code}</code></Notice>}
          <small className="v3-sso-muted">{t("v3.sso.testedAt", { when: ago(result.checked_at) })}</small>
        </div>
      )}
    </Panel>
  );
}

export function LoginsTable({ items }: { items: SsoLogin[] }) {
  const { t } = useTranslation();
  if (!items.length) return <p className="v3-sso-muted">{t("v3.sso.loginsEmpty")}</p>;
  return (
    <div className="v3-table-scroll">
      <table className="v3-table" data-testid="sso-logins">
        <thead>
          <tr>
            <th>{t("v3.sso.logins.when")}</th>
            <th>{t("v3.sso.logins.outcome")}</th>
            <th>{t("v3.sso.logins.account")}</th>
            <th>{t("v3.sso.logins.detail")}</th>
          </tr>
        </thead>
        <tbody>
          {items.map((row, index) => (
            <tr key={`${row.at}-${index}`}>
              <td className="mono">{ago(row.at)}</td>
              <td>
                <Chip s={row.outcome === "success" ? "ok" : row.outcome === "refused" ? "wait" : "act"}>
                  {t(`v3.sso.logins.${row.outcome}`)}
                </Chip>
              </td>
              <td>{row.username ?? "—"}{row.jit && <> <Chip s="info">{t("v3.sso.logins.jit")}</Chip></>}</td>
              <td className="mono">{row.reason ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ProviderDetail({ provider, presets, targets, onChanged, onDeleted }: {
  provider: Provider;
  presets: Presets | undefined;
  targets: MappingTargets;
  onChanged: (next: Provider) => void;
  onDeleted: () => void;
}) {
  const { t } = useTranslation();
  const toast = useToast();
  const [draft, setDraft] = useState<ProviderDraft>(() => draftOf(provider));
  const [busy, setBusy] = useState<"save" | "test" | "toggle" | "delete" | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const logins = useLoad(() => ssoApi.logins(provider.id), `sso-logins-${provider.id}-${provider.updated_at}`);
  useEffect(() => setDraft(draftOf(provider)), [provider]);

  const dirty = isDirty(provider, draft);
  const problems = draftProblems(draft, provider.kind);
  const saml = provider.kind === "saml";
  const set = <K extends keyof ProviderDraft>(key: K, value: ProviderDraft[K]) => setDraft((d) => ({ ...d, [key]: value }));
  const hint = presets?.[provider.preset];

  const run = async (kind: NonNullable<typeof busy>, work: () => Promise<Provider | void>, okKey?: string) => {
    setBusy(kind);
    try {
      const next = await work();
      if (next) onChanged(next);
      if (okKey) toast("ok", t(okKey, { name: provider.name }));
    } catch (err) {
      toast("act", errorMessage(err), err);
    } finally {
      setBusy(null);
    }
  };
  const save = () => run("save", () => ssoAdminApi.update(provider.id, patchOf(provider, draft)), "v3.sso.saved");
  const toggle = () => run("toggle", () => ssoAdminApi.update(provider.id, { enabled: !provider.enabled }),
    provider.enabled ? "v3.sso.disabled" : "v3.sso.enabled");
  const test = () => run("test", async () => {
    await ssoAdminApi.test(provider.id);
    return ssoAdminApi.get(provider.id);
  });

  const secretPlaceholder = provider.client_secret_source === "secrets_manager"
    ? provider.client_secret_ref ?? ""
    : provider.client_secret_set ? t("v3.sso.secretStored") : t("v3.sso.secretNone");

  return (
    <div className="v3-sso-detail" data-testid="sso-detail">
      <header className="v3-sso-detail-head">
        <PresetMark preset={provider.preset} size="lg" />
        <div className="txt">
          <span className="v3-sso-kick">{t(`auth.sso.preset.${provider.preset}`)} · {saml ? "SAML 2.0" : "OIDC"} · <span className="mono">{provider.id}</span></span>
          <h2>{provider.name}</h2>
          <span className="meta">
            <Chip s={provider.enabled ? "ok" : "off"}>{t(provider.enabled ? "v3.sso.onLogin" : "v3.sso.offLogin")}</Chip>
            <span>{t("v3.sso.linked", { count: provider.linked_accounts })}</span>
          </span>
        </div>
        <div className="end">
          <Btn kind={provider.enabled ? "ghost" : "primary"} onClick={() => void toggle()} disabled={busy !== null || (!provider.enabled && !canEnable(provider))}>
            <Power size={14} aria-hidden="true" /> {t(provider.enabled ? "v3.sso.disable" : "v3.sso.enable")}
          </Btn>
          <Btn kind="ghost" onClick={() => setConfirmDelete(true)} disabled={busy !== null} title={t("v3.sso.delete")}>
            <Trash2 size={14} aria-hidden="true" />
            <span className="sr-only">{t("v3.sso.delete")}</span>
          </Btn>
        </div>
      </header>

      {saml ? <SamlExchange provider={provider} onChanged={onChanged} /> : (
    <section className="v3-sso-handoff" aria-label={t("v3.sso.section.handoff")}>
          <div className="v3-sso-kick">{t("v3.sso.section.handoff")}</div>
          <p>{t(`v3.sso.handoffBody.${PRIMARY_PRESETS.has(provider.preset as SsoPreset) ? provider.preset : "template"}`)}</p>
          <CopyPlate label={t("v3.sso.callbackUrl")} value={provider.callback_url} testId="sso-callback-url" />
          <div className="v3-sso-facts">
            <span><b>{t("v3.sso.fact.flow")}</b> {t("v3.sso.fact.flowValue")}</span>
            <span><b>{t("v3.sso.fact.scopes")}</b> <code>{provider.scopes}</code></span>
            {provider.claims.groups && <span><b>{t("v3.sso.fact.groups")}</b> <code>{provider.claims.groups}</code></span>}
          </div>
        </section>
      )}

      <div className="v3-sso-grid">
        {saml ? (
          <Panel title={t("v3.sso.section.connection")}>
            <SamlConnection provider={provider} draft={draft} problems={problems} set={set} />
          </Panel>
        ) : (
        <Panel title={t("v3.sso.section.connection")}>
          <div className="v3-sso-form">
            <label className="v3-field">
              <span>{t("v3.sso.field.name")}</span>
              <input className="v3-input" value={draft.name} maxLength={64} onChange={(e) => set("name", e.target.value)} />
            </label>
            <label className="v3-field">
              <span>{t("v3.sso.field.issuer")}</span>
              <input className="v3-input mono" value={draft.issuer} placeholder={hint?.issuer_hint} spellCheck={false}
                aria-invalid={problems.includes("issuer")} onChange={(e) => set("issuer", e.target.value)} />
              {problems.includes("issuer")
                ? <small className="v3-err">{t("v3.sso.problem.issuer")}</small>
                : <small className="v3-hint">{t("v3.sso.hint.issuer")}</small>}
            </label>
            <label className="v3-field">
              <span>{t("v3.sso.field.clientId")}</span>
              <input className="v3-input mono" value={draft.client_id} spellCheck={false}
                aria-invalid={problems.includes("client_id")} onChange={(e) => set("client_id", e.target.value)} />
            </label>
            <label className="v3-field">
              <span>{t("v3.sso.field.clientSecret")}</span>
              <input className="v3-input mono" type="password" autoComplete="new-password" value={draft.client_secret}
                placeholder={secretPlaceholder} disabled={draft.clear_secret}
                onChange={(e) => set("client_secret", e.target.value)} data-testid="sso-secret" />
              <small className="v3-hint">{t("v3.sso.hint.clientSecret")}</small>
              {provider.client_secret_set && (
                <label className="v3-sso-check">
                  <input type="checkbox" checked={draft.clear_secret} onChange={(e) => set("clear_secret", e.target.checked)} />
                  {t("v3.sso.clearSecret")}
                </label>
              )}
            </label>
            <label className="v3-field">
              <span>{t("v3.sso.field.scopes")}</span>
              <input className="v3-input mono" value={draft.scopes} spellCheck={false}
                aria-invalid={problems.includes("scopes")} onChange={(e) => set("scopes", e.target.value)} />
              {problems.includes("scopes") && <small className="v3-err">{t("v3.sso.problem.scopes")}</small>}
            </label>
          </div>
        </Panel>
        )}

        {saml
          ? <SamlTestPanel provider={provider} busy={busy === "test"} onTest={() => void test()} />
          : <TestPanel provider={provider} busy={busy === "test"} onTest={() => void test()} />}

        <Panel title={t("v3.sso.section.accounts")}>
          <div className="v3-sso-form">
            <fieldset className="v3-sso-policy">
              <legend>{t("v3.sso.field.policy")}</legend>
              {(["jit", "existing_only"] as const).map((policy) => (
                <label key={policy} className={draft.account_policy === policy ? "on" : ""}>
                  <input type="radio" name={`sso-policy-${provider.id}`} checked={draft.account_policy === policy}
                    onChange={() => set("account_policy", policy)} />
                  <span><b>{t(`v3.sso.policy.${policy}`)}</b><small>{t(`v3.sso.policyBody.${policy}`)}</small></span>
                </label>
              ))}
            </fieldset>
            <label className="v3-field">
              <span>{t("v3.sso.field.domains")}</span>
              <textarea className="v3-input mono" rows={3} value={draft.allowed_email_domains}
                placeholder="example.com" onChange={(e) => set("allowed_email_domains", e.target.value)} />
              <small className="v3-hint">{t("v3.sso.hint.domains")}</small>
            </label>
            <div className="v3-sso-pair">
              <label className="v3-field">
                <span>{t("v3.sso.field.defaultRole")}</span>
                <select className="v3-select" value={draft.default_role}
                  onChange={(e) => set("default_role", e.target.value as ProviderDraft["default_role"])}>
                  {SSO_ROLES.map((role) => <option key={role} value={role}>{t(`v3.sso.role.${role}`)}</option>)}
                </select>
              </label>
              <label className="v3-field">
                <span>{t("v3.sso.field.validDays")}</span>
                <input className="v3-input mono" inputMode="numeric" value={draft.valid_days}
                  aria-invalid={problems.includes("valid_days")} onChange={(e) => set("valid_days", e.target.value)} />
              </label>
            </div>
            <small className="v3-hint">{t("v3.sso.hint.validDays")}</small>
          </div>
        </Panel>

        <Panel title={t("v3.sso.section.claims")}>
          <div className="v3-sso-form">
            <p className="v3-sso-muted">{t(saml ? "v3.sso.claimsBodySaml" : "v3.sso.claimsBody")}</p>
            {(["username", "email", "name", "groups"] as const).map((key) => (
              <label key={key} className="v3-field v3-sso-claim">
                <span>{t(`v3.sso.claim.${key}`)}</span>
                <input className="v3-input mono" value={draft.claims[key]} spellCheck={false}
                  placeholder={hint?.claims[key] || "—"}
                  onChange={(e) => set("claims", { ...draft.claims, [key]: e.target.value })} />
              </label>
            ))}
          </div>
        </Panel>
      </div>

      <MappingsEditor provider={provider} targets={targets} onSaved={onChanged} />

      <ScimPanel
        provider={provider}
        onScim={(scim) => onChanged({ ...provider, scim })}
        subject={draft.scim_subject}
        onSubject={(value) => set("scim_subject", value)}
      />

      <Panel title={t("v3.sso.section.logins")} end={<Btn size="sm" kind="ghost" onClick={logins.reload} title={t("v3.sso.refresh")}><RefreshCw size={13} aria-hidden="true" /><span className="sr-only">{t("v3.sso.refresh")}</span></Btn>}>
        {logins.loading && !logins.data ? <Skeleton rows={3} /> : logins.error ? <Notice s="act">{logins.error}</Notice> : (
          <LoginsTable items={logins.data?.items ?? []} />
        )}
      </Panel>

      <div className="v3-sso-savebar" data-on={dirty} aria-hidden={!dirty}>
        <span><Lamp s="wait" live /> {t("v3.sso.unsaved")}</span>
        <Btn kind="ghost" onClick={() => setDraft(draftOf(provider))} disabled={busy !== null || !dirty}>{t("v3.sso.discard")}</Btn>
        <Btn kind="primary" onClick={() => void save()} disabled={busy !== null || !dirty || problems.length > 0}>
          {t("v3.sso.save")}
        </Btn>
      </div>

      {confirmDelete && (
        <Confirm
          danger
          title={t("v3.sso.deleteTitle", { name: provider.name })}
          confirmLabel={t("v3.sso.delete")}
          cancelLabel={t("common.cancel")}
          busy={busy === "delete"}
          onCancel={() => setConfirmDelete(false)}
          onConfirm={() => void run("delete", async () => {
            await ssoApi.remove(provider.id);
            setConfirmDelete(false);
            onDeleted();
          }, "v3.sso.deleted")}
        >
          {provider.linked_accounts > 0
            ? t("v3.sso.deleteBlocked", { count: provider.linked_accounts })
            : t("v3.sso.deleteBody")}
        </Confirm>
      )}
    </div>
  );
}

/** V3 "Configure → Single sign-on" (sso §2.7): administrators only, hub-global. */
export function V3Sso() {
  const { t } = useTranslation();
  const auth = useAuth();
  const [nonce, setNonce] = useState(0);
  const loaded = useLoad(() => ssoAdminApi.list(), `sso-${nonce}`);
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState<AnyPreset | null | false>(false);
  const [items, setItems] = useState<Provider[]>([]);
  const [settings, setSettings] = useState<LoginSettings | null>(null);
  useEffect(() => {
    if (loaded.data) {
      setItems(loaded.data.items);
      setSettings(loaded.data.settings);
    }
  }, [loaded.data]);
  const enabledCount = items.filter((item) => item.enabled).length;
  const projects = useLoad(() => projectsApi.list({ status: "active" }), "sso-projects");
  const groups = useLoad(() => projectGroupsApi.list({ status: "active" }), "sso-groups");
  const templates = useLoad(() => projectsApi.roleTemplates(), "sso-role-templates");
  const targets: MappingTargets = useMemo(() => ({
    projects: (projects.data?.projects ?? []).map((p) => ({ id: p.id, name: p.name })),
    groups: (groups.data?.groups ?? []).map((g) => ({ id: g.id, name: g.name })),
    roleTemplates: (templates.data?.templates ?? []).map((r) => r.key),
  }), [projects.data, groups.data, templates.data]);

  const current = items.find((item) => item.id === selected) ?? items[0] ?? null;
  const replace = (next: Provider) => {
    setItems((list) => list.map((item) => (item.id === next.id ? next : item)));
    // the switch is in effect only while a provider is enabled
    if (settings) {
      const enabled = items.some((item) => (item.id === next.id ? next.enabled : item.enabled));
      setSettings({ ...settings, local_login_hidden: settings.hide_local_login && enabled });
    }
  };

  if (!auth.isAdmin) {
    return <Notice s="wait">{t("v3.sso.adminOnly")}</Notice>;
  }
  return (
    <div className="v3-reveal v3-sso" style={{ display: "grid", gap: 18 }}>
      <PageHead
        eyebrow={t("v3.sso.eyebrow")}
        title={t("v3.sso.title")}
        sub={t("v3.sso.sub")}
        end={items.length > 0 && (
          <Btn kind="primary" onClick={() => setCreating(null)}><Plus size={14} aria-hidden="true" /> {t("v3.sso.new")}</Btn>
        )}
      />
      {loaded.loading && !loaded.data ? <Skeleton rows={5} /> : loaded.error ? (
        <Notice s="act">{loaded.error}</Notice>
      ) : items.length === 0 ? (
        <Panel className="v3-sso-start">
          <div className="v3-sso-start-head">
            <Empty title={t("v3.sso.emptyTitle")}>
              <p className="v3-sso-muted">{t("v3.sso.emptyBody")}</p>
            </Empty>
          </div>
          <PresetGrid value={null} onPick={(preset) => setCreating(preset)} />
        </Panel>
      ) : (
        <>
        {settings && <LoginPageSwitch settings={settings} enabledCount={enabledCount} onChange={setSettings} />}
        <div className="v3-sso-layout">
          <ProviderRail items={items} selected={current?.id ?? null} onSelect={setSelected} />
          {current && (
            <ProviderDetail
              key={current.id}
              provider={current}
              presets={loaded.data?.presets}
              targets={targets}
              onChanged={replace}
              onDeleted={() => {
                setSelected(null);
                setNonce((n) => n + 1);
              }}
            />
          )}
        </div>
        </>
      )}
      {creating !== false && (
        <CreateDialog
          presets={loaded.data?.presets}
          initial={creating}
          onClose={() => setCreating(false)}
          onCreated={(created) => {
            setCreating(false);
            setItems((list) => [...list, created]);
            setSelected(created.id);
          }}
        />
      )}
    </div>
  );
}
