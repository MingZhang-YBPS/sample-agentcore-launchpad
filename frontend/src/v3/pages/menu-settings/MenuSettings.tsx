import { EyeOff, ShieldAlert } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { ErrorCallout } from "../../../components/ErrorCallout";
import { navSettingsApi } from "../../../lib/api/navSettings";
import { useFailure } from "../../failure";
import { useLoad, useToast } from "../../hooks";
import { hideableGroups } from "../../nav";
import { Btn, Notice, PageHead, Panel, Skeleton } from "../../ui";

/**
 * Menu settings (admin-only): which module entries members see in the V3 rail and
 * ⌘K. A view filter, not authorization — a hidden page stays reachable by URL.
 */
export function V3MenuSettings() {
  const { isAdmin } = useAuth();
  const { t } = useTranslation();
  if (!isAdmin) {
    return (
      <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
        <PageHead eyebrow={t("v2.nav.groupAdmin")} title={t("v3.menuSettings.title")} sub={t("auth.adminRequired.meta")} />
        <Panel>
          <div className="v3-empty" data-testid="v3-menu-settings-forbidden">
            <ShieldAlert size={28} aria-hidden="true" />
            <strong>{t("auth.adminRequired.title")}</strong>
            {t("auth.adminRequired.body")}
          </div>
        </Panel>
      </div>
    );
  }
  return <Editor />;
}

function Editor() {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const load = useLoad(() => navSettingsApi.hiddenModules(), "v3-menu-settings");
  const groups = useMemo(hideableGroups, []);
  const [hidden, setHidden] = useState<Set<string> | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (load.data) setHidden(new Set(load.data.hidden));
  }, [load.data]);

  const saved = useMemo(() => new Set(load.data?.hidden ?? []), [load.data]);
  const dirty = hidden !== null && (hidden.size !== saved.size || [...hidden].some((k) => !saved.has(k)));
  const toggle = (key: string, show: boolean) =>
    setHidden((prev) => {
      const next = new Set(prev ?? []);
      if (show) next.delete(key);
      else next.add(key);
      return next;
    });
  const save = async () => {
    if (!hidden) return;
    setBusy(true);
    try {
      failed.clear();
      // keep keys the catalog no longer lists out of the write: only what this page shows
      const known = new Set(groups.flatMap((g) => g.items.map((i) => i.railKey)));
      await navSettingsApi.setHiddenModules([...hidden].filter((k) => known.has(k)).sort());
      toast("ok", t("v3.menuSettings.saved"));
      load.reload();
    } catch (err) {
      failed.fail(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead eyebrow={t("v2.nav.groupAdmin")} title={t("v3.menuSettings.title")} sub={t("v3.menuSettings.sub")} />
      <ErrorCallout error={failed.error ?? load.failure} onDismiss={failed.clear} data-testid="v3-menu-settings-error" />
      <Notice>{t("v3.menuSettings.viewOnly")}</Notice>
      {hidden === null ? (
        <Panel><Skeleton rows={6} /></Panel>
      ) : (
        <>
          {groups.map((group) => (
            <Panel key={group.key} title={t(group.labelKey)}
              end={t("v3.menuSettings.shownCount", {
                shown: group.items.filter((i) => !hidden.has(i.railKey)).length,
                total: group.items.length,
              })}>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: 8 }}>
                {group.items.map((item) => {
                  const Icon = item.icon;
                  const shown = !hidden.has(item.railKey);
                  return (
                    <label key={item.railKey} style={{ display: "flex", gap: 8, alignItems: "center", opacity: shown ? 1 : 0.55 }}>
                      <input type="checkbox" checked={shown} onChange={(e) => toggle(item.railKey, e.target.checked)}
                        data-testid={`v3-menu-item-${item.railKey}`} />
                      <Icon size={15} aria-hidden="true" />
                      {t(item.labelKey)}
                      {!shown && <EyeOff size={13} aria-label={t("v3.menuSettings.hidden")} />}
                    </label>
                  );
                })}
              </div>
            </Panel>
          ))}
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <Btn kind="primary" busy={busy} disabled={!dirty} disabledReason={t("story.reason.noChanges")}
              onClick={() => void save()} data-testid="v3-menu-settings-save">
              {t("v3.menuSettings.save")}
            </Btn>
            <Btn kind="ghost" disabled={!dirty} disabledReason={t("story.reason.noChanges")}
              onClick={() => setHidden(new Set(saved))}>
              {t("v3.menuSettings.reset")}
            </Btn>
            {load.data?.updated_by && (
              <span style={{ color: "var(--v3-text-3)", fontSize: 12 }}>
                {t("v3.menuSettings.updated", {
                  who: load.data.updated_by,
                  when: load.data.updated_at ? new Date(load.data.updated_at).toLocaleString() : "—",
                })}
              </span>
            )}
          </div>
        </>
      )}
    </div>
  );
}
