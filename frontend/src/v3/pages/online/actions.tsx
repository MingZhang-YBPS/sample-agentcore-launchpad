import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { api, type OnlineEvalConfigRow } from "../../../lib/api";
import { configName } from "../../../v2/online";
import { useToast } from "../../hooks";
import { Confirm, Notice } from "../../ui";

export type Pending = { row: OnlineEvalConfigRow; action: "pause" | "resume" | "delete" };

export const onlineHref = (params: Record<string, string>) => `/v3/online?${new URLSearchParams(params).toString()}`;

/** Pause / resume / delete with V2's confirm copy and guards; shared by the list and a config. */
export function useOnlineAction(onDone: (action: Pending["action"]) => void) {
  const { t } = useTranslation();
  const toast = useToast();
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  // shown in the still-open dialog with its AWS details, cleared with the dialog
  const [failure, setFailure] = useState<unknown>(null);
  const ask = (next: Pending | null) => {
    setFailure(null);
    setPending(next);
  };
  const run = async () => {
    if (!pending) return;
    const { row, action } = pending;
    setBusy(true);
    setFailure(null);
    try {
      if (action === "delete") await api.v2DeleteOnlineConfig(row.config_id);
      else await api.v2OnlineAction(row.config_id, action);
      toast("ok", action === "delete" ? t("v2.online.deleted", { logGroup: row.results_log_group }) : t(`v2.tasks.done.${action}`));
      setPending(null);
      onDone(action);
    } catch (err) {
      setFailure(err);
    } finally {
      setBusy(false);
    }
  };
  const dialog = pending ? (
    <Confirm
      title={t(`v2.online.confirm.${pending.action}Title`)}
      confirmLabel={t(`v2.online.confirm.${pending.action}Ok`)}
      cancelLabel={t("v3.common.cancel")}
      danger={pending.action === "delete"}
      busy={busy}
      onCancel={() => ask(null)}
      onConfirm={() => void run()}
    >
      {t(`v2.online.confirm.${pending.action}Body`, { name: configName(pending.row), logGroup: pending.row.results_log_group })}
      {failure != null && <div style={{ marginTop: 12 }}><Notice s="act"><ApiErrorDetail error={failure} /></Notice></div>}
    </Confirm>
  ) : null;
  return { ask, busy, dialog };
}
