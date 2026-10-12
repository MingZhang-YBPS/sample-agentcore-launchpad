"""Scenario templates for the creation wizard (roadmap T10).

A template is **not** a new creation method or a stored resource: it is a named set
of wizard defaults — the prompt, the method, whether the scenario wants a knowledge
base, which demo toolkit fits — that the console pre-fills so a business member can
go from "I need an HR policy bot" to a deployed agent without choosing a build
method or writing a system prompt. The agent it produces is an ordinary agent; once
created, nothing records which template it came from beyond the display name the
member kept.

Pure data and pure functions on purpose (the shape `system_agents/presets.py` uses):
no ledger, no AWS. The catalogue is served read-only by `GET /api/agent-templates`
and applied client-side, so a template can never smuggle a field past `AgentSpec`
validation — the wizard still posts an ordinary spec the member can see and edit.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# Which zip-runtime demo toolkits exist (`AgentSpec.toolkits`). A template naming one
# implies the zip_runtime method, because the managed Harness cannot carry toolkits.
Toolkit = Literal["hr_assistant"]


@dataclass(frozen=True)
class AgentTemplate:
    key: str
    # i18n keys, not prose: the console owns en + zh-CN copy for the gallery card.
    label_key: str
    description_key: str
    method: Literal["harness", "zip_runtime"]
    system_prompt: str
    # Whether the scenario is about documents. `required` blocks deploy without one,
    # `suggested` shows the upload but lets an empty agent through, `none` hides it.
    knowledge: Literal["required", "suggested", "none"] = "none"
    toolkits: tuple[Toolkit, ...] = ()
    memory_long_term: bool = False
    # A PII-shaped scenario switches the T12 preset on by default; the member can
    # still turn it off in the wizard's advanced section.
    guardrail: bool = False
    # Seeded into the try-chat panel (T09 generates its own when a template is absent).
    sample_questions: tuple[str, ...] = ()
    tags: tuple[str, ...] = field(default_factory=tuple)


TEMPLATES: tuple[AgentTemplate, ...] = (
    AgentTemplate(
        key="hr-policy-qa",
        label_key="templates.hrPolicyQa.label",
        description_key="templates.hrPolicyQa.desc",
        method="harness",
        system_prompt=(
            "You are an HR policy assistant for employees of this company. Answer "
            "questions about leave, benefits, expenses and working arrangements using "
            "ONLY the company documents available to you. Quote the specific policy or "
            "section you relied on. If the documents do not cover the question, say so "
            "and suggest contacting the HR team rather than guessing. Never reveal or "
            "repeat another employee's personal data. Answer in the language the "
            "employee used, in at most three short paragraphs."
        ),
        knowledge="required",
        memory_long_term=True,
        guardrail=True,
        sample_questions=(
            "How many days of annual leave do I get?",
            "What is the policy for working from home?",
            "How do I claim a travel expense?",
        ),
        tags=("hr", "rag"),
    ),
    AgentTemplate(
        key="it-service-desk",
        label_key="templates.itServiceDesk.label",
        description_key="templates.itServiceDesk.desc",
        method="harness",
        system_prompt=(
            "You are an IT service desk assistant. Help colleagues with account access, "
            "device setup, VPN, software requests and common troubleshooting, using the "
            "internal documentation available to you. Give numbered steps the person can "
            "follow themselves. Ask one clarifying question when the symptom is "
            "ambiguous. Never ask for a password or any other credential, and never "
            "repeat one back. When the issue needs a human — hardware failure, anything "
            "touching payroll or security — say which team to contact and why."
        ),
        knowledge="suggested",
        memory_long_term=True,
        guardrail=True,
        sample_questions=(
            "I cannot connect to the VPN from home.",
            "How do I request a new laptop?",
            "My account is locked — what should I do?",
        ),
        tags=("it", "support"),
    ),
    AgentTemplate(
        key="hr-tools-demo",
        label_key="templates.hrToolsDemo.label",
        description_key="templates.hrToolsDemo.desc",
        method="zip_runtime",
        system_prompt=(
            "You are an HR assistant with access to employee-record tools. Use the tools "
            "to look up records, leave balances and PTO requests rather than guessing, "
            "and state the figure the tool returned. Answer in one or two sentences."
        ),
        toolkits=("hr_assistant",),
        sample_questions=(
            "How much PTO does employee E001 have left?",
            "Submit a PTO request for E001 for three days.",
            "Who reports to the engineering manager?",
        ),
        tags=("hr", "tools", "demo"),
    ),
    AgentTemplate(
        key="blank",
        label_key="templates.blank.label",
        description_key="templates.blank.desc",
        method="harness",
        system_prompt="",
        knowledge="suggested",
        tags=("blank",),
    ),
)

BY_KEY: dict[str, AgentTemplate] = {template.key: template for template in TEMPLATES}


def catalogue() -> list[dict[str, Any]]:
    """The gallery payload — every template, in display order."""
    return [asdict(template) for template in TEMPLATES]


def get(key: str) -> AgentTemplate | None:
    return BY_KEY.get(key)
