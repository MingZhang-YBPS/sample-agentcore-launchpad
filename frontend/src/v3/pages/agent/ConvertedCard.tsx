import { useTranslation } from "react-i18next";

import type { AgentInfo } from "../../../lib/api";
import { Panel } from "../../ui";

/** A code agent converted from a Harness agent: the source and the per-capability notes. */
export function ConvertedCard({ agent }: { agent: AgentInfo }) {
  const { t } = useTranslation();
  const spec = (agent.spec ?? {}) as Record<string, unknown>;
  const source = spec.source_harness as { agent_name?: string } | undefined;
  const notes = (spec.conversion_notes as Record<string, string> | undefined) ?? {};
  if (!source?.agent_name) return null;
  return (
    <Panel title={t("v3.agent.converted.title")} end={<span className="v3-hint">{t("v3.agent.converted.from", { name: source.agent_name })}</span>}>
      {Object.keys(notes).length ? (
        <dl className="v3-kv">
          {Object.entries(notes).map(([cap, note]) => (
            <div key={cap} style={{ display: "contents" }}>
              <dt>{cap}</dt>
              <dd>{note}</dd>
            </div>
          ))}
        </dl>
      ) : (
        <span className="v3-hint">—</span>
      )}
    </Panel>
  );
}
