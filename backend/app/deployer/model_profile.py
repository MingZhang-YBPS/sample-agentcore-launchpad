"""The project's application inference profile for code runtimes (projects design §10.8).

The harness deployer swaps the model id in its request (`harness._attribute_to_project`);
a code runtime's model id is baked into its generated `main.py`, so for those the
platform hands the profile ARN over in `LAUNCHPAD_MODEL_ID`, which the templates read
before their rendered id (`MODEL_ID = os.environ.get("LAUNCHPAD_MODEL_ID") or MODEL_ID`).
BYOC code receives the same variable and may use it; its `MODEL_ID` stays the spec's id.

Only behind `project_inference_profiles` and for an isolated project's agent whose model
is a Bedrock-hosted id (not Mantle, not an ARN the spec already pins); everything else —
every single-project workspace — gets no variable, no IAM change and no AWS call.
The execution role is granted the profile ARN next to the model's own resources
(`agent_iam.policy_document`, `RoleContext.inference_profile_arns`): invoking an
application profile authorizes against the profile and the foundation models it routes to.

UNVERIFIED live: the Claude Agent SDK (container method, `CLAUDE_CODE_USE_BEDROCK=1`)
taking an application inference profile ARN as its model; Strands' BedrockModel passes
the id to Converse, which does.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.schemas.agent import AgentSpec

ENV_MODEL_ID = "LAUNCHPAD_MODEL_ID"

# the methods whose code reads LAUNCHPAD_MODEL_ID (templates) or may (byoc); studio
# flows carry their models on the nodes and keep them
_CODE_METHODS = ("zip_runtime", "container", "byoc")


def profiled_model_ids(spec: AgentSpec) -> list[str]:
    """The model ids of `spec` that the project's profile stands in for (harness and
    the code methods); [] when the agent calls no Bedrock-hosted model."""
    if spec.protocol == "mcp" or spec.model_source != "bedrock":
        return []
    if spec.model_provider is not None:
        # SB-2: a direct model provider's id is the provider's, not a Bedrock id
        return []
    if spec.method not in (*_CODE_METHODS, "harness"):
        return []
    model_id = spec.model_id or ""
    if not model_id or model_id.startswith("arn:"):
        return []
    return [model_id]


def profile_arns(project_id: str | None, spec: AgentSpec, workspace: Any,
                 log: Callable[[str], None] | None = None) -> dict[str, str]:
    """`{model id: profile ARN}` for an isolated project's agent with the setting on —
    made (or adopted) on first use, recorded in `project_resources`; {} otherwise."""
    from app.services import inference_profiles
    from app.services.project_telemetry import attributed

    models = profiled_model_ids(spec)
    if not (models and project_id and inference_profiles.enabled()):
        return {}
    if not attributed(project_id):
        return {}
    from app.core.db import SessionLocal

    out: dict[str, str] = {}
    db = SessionLocal()
    try:
        for model_id in models:
            arn = inference_profiles.model_id_for(
                db, project_id=project_id, workspace=workspace, model_id=model_id, log=log,
            )
            if arn != model_id:
                out[model_id] = arn
    finally:
        db.close()
    return out


def model_environment(project_id: str | None, spec: AgentSpec, workspace: Any,
                      log: Callable[[str], None] | None = None) -> dict[str, str]:
    """`{LAUNCHPAD_MODEL_ID: <profile ARN>}` for a code runtime of an isolated
    project with the setting on; {} for every other agent."""
    if spec.method not in _CODE_METHODS:
        return {}
    arns = profile_arns(project_id, spec, workspace, log)
    arn = arns.get(spec.model_id)
    if not arn:
        return {}
    if log is not None:
        log(f"model {spec.model_id} via the project's inference profile {arn}")
    return {ENV_MODEL_ID: arn}
