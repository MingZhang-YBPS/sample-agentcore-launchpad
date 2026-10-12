import { useTranslation } from "react-i18next";

import type { TokenPaged } from "../../../v2/pages/memory/common";
import { Btn, Notice, Skeleton } from "../../ui";

/** Token-driven "load more" — renders nothing once AWS stops paginating. */
export function LoadMore({ list }: { list: Pick<TokenPaged<unknown>, "token" | "loading" | "loadMore"> }) {
  const { t } = useTranslation();
  if (!list.token) return null;
  return (
    <div style={{ display: "flex", justifyContent: "center", padding: 12 }}>
      <Btn size="sm" disabled={list.loading} onClick={list.loadMore}>
        {list.loading ? t("v3.memory.loading") : t("v3.memory.loadMore")}
      </Btn>
    </div>
  );
}

/** Loading / error / empty for one token-paged pane; null once it has rows. */
export function PaneState({ list, empty }: { list: TokenPaged<unknown>; empty: string }) {
  const { t } = useTranslation();
  if (list.loading && list.items.length === 0) return <Skeleton rows={3} />;
  if (list.error)
    return (
      <Notice s="act">
        {list.error}{" "}
        <button type="button" className="v3-btn ghost sm" onClick={list.reload}>{t("v3.memory.retry")}</button>
      </Notice>
    );
  if (list.items.length === 0) return <div className="v3-mem-empty">{empty}</div>;
  return null;
}
