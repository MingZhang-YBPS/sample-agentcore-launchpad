"""T10 — the scenario-template catalogue.

A template is only a set of wizard defaults, so what is worth pinning is that the
catalogue stays *applicable*: every prompt/method/toolkit combination it offers must
survive `AgentSpec` validation, because the wizard posts an ordinary spec built from
it. A template that cannot validate would fail at deploy time, in the one flow whose
whole point is that a business member never sees a validation error.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas.agent import AgentSpec
from app.services import agent_templates


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


def test_the_catalogue_is_served_and_stable(client):
    body = client.get("/api/agent-templates").json()
    keys = [row["key"] for row in body["templates"]]
    assert keys == [template.key for template in agent_templates.TEMPLATES]
    assert "hr-policy-qa" in keys and "blank" in keys


def test_keys_are_unique_and_lookup_works():
    keys = [template.key for template in agent_templates.TEMPLATES]
    assert len(keys) == len(set(keys))
    assert agent_templates.get("hr-policy-qa") is not None
    assert agent_templates.get("no-such-template") is None


@pytest.mark.parametrize("template", agent_templates.TEMPLATES, ids=lambda t: t.key)
def test_every_template_builds_a_valid_spec(template):
    """The defaults a template hands the wizard must pass spec validation.

    `blank` ships an empty prompt on purpose (the member writes it), which the spec
    refuses — so it is checked with a placeholder, proving only that the *rest* of
    its defaults are valid.
    """
    spec = AgentSpec(
        name=f"tmpl-{template.key.replace('_', '-')}"[:48],
        method=template.method,
        system_prompt=template.system_prompt or "placeholder prompt",
        toolkits=list(template.toolkits),
        guardrail={"enabled": template.guardrail, "mode": "anonymize"},
        memory={"short_term": True, "long_term": template.memory_long_term},
    )
    assert spec.method == template.method
    assert spec.guardrail.enabled is template.guardrail


def test_toolkits_only_appear_on_a_method_that_supports_them():
    """`AgentSpec` refuses toolkits on a harness — a template must not offer that."""
    for template in agent_templates.TEMPLATES:
        if template.toolkits:
            assert template.method == "zip_runtime", template.key


def test_a_knowledge_first_template_declares_it():
    hr = agent_templates.get("hr-policy-qa")
    assert hr is not None
    assert hr.knowledge == "required"
    # a documents-only scenario must not also ship tools that bypass the documents
    assert hr.toolkits == ()
    assert hr.guardrail is True  # employee data ⇒ the PII preset is the default
    assert 3 <= len(hr.sample_questions) <= 5


def test_the_payload_carries_i18n_keys_not_prose(client):
    for row in client.get("/api/agent-templates").json()["templates"]:
        assert row["label_key"].startswith("templates.")
        assert row["description_key"].startswith("templates.")
