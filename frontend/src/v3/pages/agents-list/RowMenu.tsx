import { Ellipsis } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import type { useAgentActions } from "../../../lib/agent-admin/actions";
import { EDIT_HINT_KEY, EDIT_LABEL_KEY, editPath } from "../../../lib/agent-admin/edit";
import type { AgentPermissions } from "../../../lib/agent-admin/permissions";
import type { AgentInfo } from "../../../lib/api";
import "../agent/agent.css";
import "./agents-list.css";
import { type RowEntry, rowEntries } from "./rows";

/**
 * A row's "⋯" menu. The list scrolls sideways (`v3-table-scroll`), which would
 * clip a dropdown hanging below the last rows, so it opens fixed under its
 * button and closes on scroll, Esc, an outside click or a pick.
 */
export function RowMenu({
  agent,
  perms,
  actions,
}: {
  agent: AgentInfo;
  perms: AgentPermissions;
  actions: ReturnType<typeof useAgentActions>;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [at, setAt] = useState<{ top: number; right: number } | null>(null);
  const button = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const id = useId();
  const entries = rowEntries(agent, perms);

  useEffect(() => {
    if (!at) return;
    const close = () => setAt(null);
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (!menu.current?.contains(target) && !button.current?.contains(target)) close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [at]);

  if (entries.length === 0) return null;
  const name = agent.display_name || agent.name;
  const run: Record<RowEntry["key"], () => void> = {
    edit: () => navigate(editPath(agent)),
    convert: () => actions.askConvert(agent),
    delete: () => actions.askDelete(agent),
    remove: () => actions.askDelete(agent),
  };
  const label = (entry: RowEntry) => t(entry.key === "edit" ? EDIT_LABEL_KEY : `v3.agent.${entry.key}`);
  // a disabled entry says why; an enabled "Edit configuration" says what it does
  const title = (entry: RowEntry) =>
    entry.why === "locked" ? perms.lockTitle : entry.why ? t(`v3.agent.editBlocked.${entry.why}`) : entry.key === "edit" ? t(EDIT_HINT_KEY) : undefined;
  const toggle = () => {
    if (at) return setAt(null);
    const box = button.current?.getBoundingClientRect();
    if (box) setAt({ top: box.bottom + 6, right: window.innerWidth - box.right });
  };

  return (
    <div className="v3-agent-menu">
      <button
        ref={button}
        type="button"
        className="v3-btn sm ghost"
        aria-haspopup="menu"
        aria-expanded={at !== null}
        aria-controls={at ? id : undefined}
        aria-label={t("v3.agents.rowMenu", { name })}
        title={t("v3.agents.rowMenu", { name })}
        onClick={toggle}
        data-testid={`v3-agents-row-menu-${agent.id}`}
      >
        <Ellipsis size={14} />
      </button>
      {at && (
        <div role="menu" id={id} ref={menu} className="v3-agents-row-menu" style={{ top: at.top, right: at.right }}>
          {entries.map((entry) => (
            <div key={entry.key} style={{ display: "contents" }}>
              {entry.danger && <div className="sep" role="separator" />}
              <button
                type="button"
                role="menuitem"
                disabled={entry.disabled}
                title={title(entry)}
                data-danger={entry.danger ? "true" : undefined}
                data-testid={`v3-agents-row-${entry.key}`}
                onClick={() => {
                  setAt(null);
                  run[entry.key]();
                }}
              >
                {label(entry)}
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
