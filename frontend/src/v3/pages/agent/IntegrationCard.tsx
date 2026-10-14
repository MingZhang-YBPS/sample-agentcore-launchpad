import { Copy } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useIntegration } from "../../../lib/agent-admin/integration";
import { KEY_PLACEHOLDER, SNIPPET_LANGS, SNIPPET_MODES } from "../../../lib/snippets";
import { useToast } from "../../hooks";
import { Btn, Filters, Panel } from "../../ui";

/** Copy-ready curl / Python / JavaScript for the agent's public `/v1` API (no key is ever shown). */
export function IntegrationCard({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const toast = useToast();
  const { lang, setLang, mode, setMode, snippet, copy } = useIntegration(agentId);
  return (
    <Panel
      title={t("v3.agent.integration.title")}
      end={
        <Btn
          size="sm"
          onClick={() =>
            void copy().then((ok) =>
              toast(ok ? "ok" : "act", t(ok ? "v3.agent.integration.copied" : "v3.agent.integration.copyFailed")),
            )
          }
        >
          <Copy size={13} /> {t("v3.agent.integration.copy")}
        </Btn>
      }
    >
      <div style={{ display: "grid", gap: 12 }} data-testid="v3-agent-integration">
        <span className="v3-hint">{t("v3.agent.integration.sub", { key: KEY_PLACEHOLDER })}</span>
        <div className="v3-agent-snippet-bar">
          <span className="v3-hint">{t("v3.agent.integration.language")}</span>
          <Filters value={lang} onChange={setLang} options={SNIPPET_LANGS.map((l) => ({ value: l, label: t(`v3.agent.integration.lang.${l}`) }))} />
          <span className="v3-hint">{t("v3.agent.integration.mode")}</span>
          <Filters value={mode} onChange={setMode} options={SNIPPET_MODES.map((m) => ({ value: m, label: t(`v3.agent.integration.modes.${m}`) }))} />
        </div>
        <pre className="v3-pre" data-testid="v3-agent-integration-snippet">{snippet}</pre>
        <span className="v3-hint">{t("v3.agent.integration.keyHint")}</span>
      </div>
    </Panel>
  );
}
