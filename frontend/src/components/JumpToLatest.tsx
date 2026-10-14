import "./jump-to-latest.css";

import { ArrowDown } from "lucide-react";
import { useTranslation } from "react-i18next";

/**
 * "Back to the latest" for a thread the reader scrolled up in (`useStickToBottom`).
 * Rendered as the scroll container's LAST child: `position: sticky` keeps it at the
 * container's bottom edge without the container having to be positioned.
 */
export function JumpToLatest({ show, onClick }: { show: boolean; onClick: () => void }) {
  const { t } = useTranslation();
  if (!show) return null;
  return (
    <div className="jump-to-latest">
      <button type="button" onClick={onClick} data-testid="jump-to-latest">
        <ArrowDown size={13} aria-hidden="true" />
        {t("common.jumpToLatest")}
      </button>
    </div>
  );
}
