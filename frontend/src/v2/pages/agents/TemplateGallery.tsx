import { useTranslation } from "react-i18next";

import { api, type AgentTemplateInfo } from "../../../lib/api";
import { useLoad } from "../../hooks";
import { Card, OptionCard, Spin, Tag } from "../../ui";

/**
 * T10 — the scenario-template gallery on the wizard's first step.
 *
 * A template is a set of wizard defaults, not a resource: picking one fills the
 * form (method, prompt, whether documents are expected, demo toolkit, memory, the
 * T12 PII preset) and hands the member the ordinary configure step, where every
 * field is still editable. The catalogue itself is static backend data
 * (`GET /api/agent-templates`), so the copy lives in i18n and the shape in one place.
 */


export function TemplateGallery({
  onPick,
}: {
  onPick: (template: AgentTemplateInfo) => void;
}) {
  const { t } = useTranslation();
  // A failed read must not block the wizard: the member can still pick a method.
  const templates = useLoad(() => api.agentTemplates(), "agent-templates");
  const rows = templates.data?.templates ?? [];
  if (templates.loading) {
    return (
      <Card title={t("templates.title")} sub={t("templates.sub")}>
        <Spin />
      </Card>
    );
  }
  if (rows.length === 0) return null;
  return (
    <Card title={t("templates.title")} sub={t("templates.sub")} testId="v2-agent-templates">
      <div className="v2-options">
        {rows.map((template) => (
          <OptionCard
            key={template.key}
            title={t(template.label_key)}
            desc={t(template.description_key)}
            on={false}
            onClick={() => onPick(template)}
            badge={
              template.knowledge === "required" ? (
                <Tag tone="blue">{t("templates.needsDocs")}</Tag>
              ) : template.guardrail ? (
                <Tag>{t("templates.piiOn")}</Tag>
              ) : undefined
            }
            testId={`v2-agent-template-${template.key}`}
          />
        ))}
      </div>
    </Card>
  );
}
