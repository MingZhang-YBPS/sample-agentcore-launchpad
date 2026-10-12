import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, errorMessage, type OnlineEvalConfigRow } from "../../../lib/api";
import { configName } from "../../../v2/online";
import { useToast } from "../../hooks";
import { Confirm } from "../../ui";

export type Pending = { row: OnlineEvalConfigRow; action: "pause" | "resume" | "delete" };

export const onlineHref = (params: Record<string, string>) => `/v3/online?${new URLSearchParams(params).toString()}`;

/** Pause / resume / delete with V2's confirm copy and guards; shared by the list and a config. */
export function useOnlineAction(onDone: (action: Pending["action"]) => void) {
  const { t } = useTranslation();
  const toast = useToast();
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    if (!pending) return;
    const { row, action } = pending;
    setBusy(true);
    try {
      if (action === "delete") await api.v2DeleteOnlineConfig(row.config_id);
      else await api.v2OnlineAction(row.config_id, action);
      toast("ok", action === "delete" ? t("v2.online.deleted", { logGroup: row.results_log_group }) : t(`v2.tasks.done.${action}`));
      setPending(null);
      onDone(action);
    } catch (err) {
      toast("act", errorMessage(err));
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
      onCancel={() => setPending(null)}
      onConfirm={() => void run()}
    >
      {t(`v2.online.confirm.${pending.action}Body`, { name: configName(pending.row), logGroup: pending.row.results_log_group })}
    </Confirm>
  ) : null;
  return { ask: setPending, busy, dialog };
}
