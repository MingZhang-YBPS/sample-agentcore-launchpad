"""What a backend application needs to call an agent (design invoke-access §0).

One agent: the resources to call (Runtime ARN and its endpoint ARNs, or the Harness
ARN; account; region), how callers authenticate (``iam`` | ``jwt``), the
least-privilege IAM policy for an ``iam`` agent (a ``jwt`` agent gets the Bearer
configuration instead — IAM permissions do not apply to it), and AWS CLI / Python /
TypeScript samples with every value filled in. One project: the same for each of its
agents, as an inventory plus ONE merged policy a single backend role can carry.

Everything is computed from the ledger (the ARN a deploy recorded, the endpoint mode,
the workspace's release mode); nothing here calls AWS, so the console never assembles
an ARN itself and a test can pin every combination.

Endpoints, from the ledger (``dlc/releases.py``):

* ``DEFAULT`` always exists (it auto-follows the latest version);
* ``live`` once the agent is migrated (``endpoint_mode = "live"``) — production;
* ``candidate`` additionally while the workspace releases ``gated``.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from typing import Any

from app.core.errors import AppError
from app.core.regions import partition_for_region
from app.models.ledger import Agent, Workspace
from app.services import inbound_auth as inbound_auth_service
from app.services.runtime_discovery import DISCOVERED_METHOD, HARNESS_RESOURCE_TYPE

DEFAULT = "DEFAULT"
LIVE = "live"
CANDIDATE = "candidate"
QUALIFIERS = (DEFAULT, LIVE, CANDIDATE)

RUNTIME_METHODS = frozenset({"zip_runtime", "studio", "container", "byoc"})
RUNTIME_ACTION = "bedrock-agentcore:InvokeAgentRuntime"
# TO VERIFY: the IAM action name the managed Harness authorizes `InvokeHarness` as.
# services/invoke.py calls the `invoke_harness` data-plane operation; the service
# authorization reference has not been checked for its action / resource type yet.
HARNESS_ACTION = "bedrock-agentcore:InvokeHarness"
HARNESS_ACTION_VERIFIED = False
STOP_ACTION = "bedrock-agentcore:StopRuntimeSession"
MEMORY_READ_ACTIONS = (
    "bedrock-agentcore:GetEvent",
    "bedrock-agentcore:ListEvents",
    "bedrock-agentcore:ListSessions",
    "bedrock-agentcore:RetrieveMemoryRecords",
    "bedrock-agentcore:GetMemoryRecord",
    "bedrock-agentcore:ListMemoryRecords",
)
# `include=` options: session management, reading the agent's memory
INCLUDES = ("stop_session", "memory")

_ARN_RE = re.compile(
    r"^arn:(?P<partition>[^:]+):bedrock-agentcore:(?P<region>[^:]+):(?P<account>\d{12}):"
    r"(?P<kind>runtime|harness)/(?P<id>[^/]+)$"
)
_DISCOVERY_SUFFIX = "/.well-known/openid-configuration"
INVENTORY_FIELDS = ("agent_id", "name", "method", "kind", "mode", "account", "region",
                    "arn", "qualifier", "endpoint_arn", "production")


@dataclass(frozen=True)
class Target:
    """The parts of an ARN every output is assembled from."""

    kind: str  # runtime | harness
    arn: str
    partition: str
    account: str
    region: str


def resource_kind(agent: Agent) -> str | None:
    """runtime | harness | None — the AWS resource family behind the row."""
    if agent.method == "harness":
        return "harness"
    if agent.method in RUNTIME_METHODS:
        return "runtime"
    if agent.method == DISCOVERED_METHOD:
        discovery = (agent.spec or {}).get("discovery") or {}
        rtype = discovery.get("resource_type") or "runtime"
        return "harness" if rtype == HARNESS_RESOURCE_TYPE else "runtime"
    return None


def target_of(agent: Agent, workspace: Workspace | None) -> Target:
    """The agent's ARN split into parts, or 409 ``agent.no_resource``."""
    kind = resource_kind(agent)
    if agent.status == "deleted" or kind is None or not agent.arn:
        raise AppError(
            "agent.no_resource",
            "This agent has no deployed AgentCore resource to call yet.",
            {"agent_id": agent.id, "status": agent.status, "method": agent.method},
            status_code=409,
        )
    match = _ARN_RE.match(agent.arn)
    if match:
        return Target(kind=match["kind"], arn=agent.arn, partition=match["partition"],
                      account=match["account"], region=match["region"])
    # an ARN shape this module does not recognise: trust the ledger's ARN as is and
    # take account / region from the workspace it was deployed into
    region = workspace.region if workspace else ""
    return Target(kind=kind, arn=agent.arn, partition=partition_for_region(region),
                  account=workspace.account_id if workspace else "", region=region)


def _gated(workspace: Workspace | None) -> bool:
    policy = (workspace.release_policy if workspace else None) or {}
    return policy.get("release_mode") == "gated"


def endpoints(agent: Agent, workspace: Workspace | None) -> list[str]:
    """The qualifiers the agent can be called with; the one production traffic uses
    is ``production_qualifier``."""
    names = [DEFAULT]
    if getattr(agent, "endpoint_mode", None) == LIVE:
        names.append(LIVE)
        if _gated(workspace):
            names.append(CANDIDATE)
    return names


def production_qualifier(agent: Agent) -> str:
    return LIVE if getattr(agent, "endpoint_mode", None) == LIVE else DEFAULT


def select_qualifiers(agent: Agent, workspace: Workspace | None,
                      qualifier: str | None) -> list[str]:
    """Every endpoint (blank), or the one asked for — 400 when the agent has no such
    endpoint, so a policy never names an endpoint that does not exist."""
    available = endpoints(agent, workspace)
    if not qualifier:
        return available
    if qualifier not in available:
        raise AppError(
            "invoke_access.unknown_qualifier",
            f"agent '{agent.name}' has no '{qualifier}' endpoint",
            {"qualifier": qualifier, "available": available},
        )
    return [qualifier]


def parse_include(raw: str | None) -> frozenset[str]:
    parts = {p.strip() for p in (raw or "").split(",") if p.strip()}
    unknown = sorted(parts - set(INCLUDES))
    if unknown:
        raise AppError("invoke_access.unknown_include", f"unknown include {unknown}",
                       {"unknown": unknown, "allowed": list(INCLUDES)})
    return frozenset(parts)


def endpoint_arn(target: Target, qualifier: str) -> str | None:
    """The runtime-endpoint ARN of a qualifier. A Harness has none here: its endpoint
    sub-resource ARN format is not modelled (TO VERIFY), so its grant is the ARN."""
    if target.kind != "runtime":
        return None
    return f"{target.arn}/runtime-endpoint/{qualifier}"


def memory_arn(agent: Agent, target: Target, workspace: Workspace | None) -> str | None:
    """The memory the agent reads and writes, when it uses one."""
    memory = (agent.spec or {}).get("memory") or {}
    if not (memory.get("short_term") or memory.get("long_term")):
        return None
    memory_id = memory.get("memory_id") or ((workspace.resources if workspace else None)
                                            or {}).get("memory_id")
    if not memory_id:
        return None
    return (f"arn:{target.partition}:bedrock-agentcore:{target.region}:{target.account}:"
            f"memory/{memory_id}")


def mode_of(agent: Agent) -> str:
    return "jwt" if inbound_auth_service.display_mode(agent) == "jwt" else "iam"


def _sid(prefix: str, agent: Agent) -> str:
    return prefix + re.sub(r"[^A-Za-z0-9]", "", agent.name.title())[:40]


def statements(agent: Agent, workspace: Workspace | None, qualifiers: list[str],
               include: frozenset[str]) -> list[dict[str, Any]]:
    """The least-privilege statements of one ``iam`` agent."""
    target = target_of(agent, workspace)
    out: list[dict[str, Any]] = []
    if target.kind == "runtime":
        resources = [target.arn, *(f"{target.arn}/runtime-endpoint/{q}" for q in qualifiers)]
        out.append({"Sid": _sid("InvokeRuntime", agent), "Effect": "Allow",
                    "Action": [RUNTIME_ACTION], "Resource": resources})
        if "stop_session" in include:
            out.append({"Sid": _sid("StopSession", agent), "Effect": "Allow",
                        "Action": [STOP_ACTION], "Resource": resources})
    else:
        out.append({"Sid": _sid("InvokeHarness", agent), "Effect": "Allow",
                    "Action": [HARNESS_ACTION], "Resource": [target.arn]})
    if "memory" in include:
        memory = memory_arn(agent, target, workspace)
        if memory:
            out.append({"Sid": _sid("ReadMemory", agent), "Effect": "Allow",
                        "Action": list(MEMORY_READ_ACTIONS), "Resource": [memory]})
    return out


def policy_document(stmts: list[dict[str, Any]]) -> dict[str, Any]:
    return {"Version": "2012-10-17", "Statement": stmts}


def merge_statements(stmts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One statement per distinct action set, resources de-duplicated in order — the
    merged project policy stays short however many agents it covers."""
    merged: dict[tuple[str, ...], list[str]] = {}
    for stmt in stmts:
        key = tuple(stmt["Action"])
        bucket = merged.setdefault(key, [])
        for arn in stmt["Resource"]:
            if arn not in bucket:
                bucket.append(arn)
    names = {(RUNTIME_ACTION,): "InvokeRuntimes", (HARNESS_ACTION,): "InvokeHarnesses",
             (STOP_ACTION,): "StopRuntimeSessions", MEMORY_READ_ACTIONS: "ReadMemories"}
    return [{"Sid": names.get(key, f"Grant{i}"), "Effect": "Allow", "Action": list(key),
             "Resource": arns} for i, (key, arns) in enumerate(merged.items())]


def resources(agent: Agent, workspace: Workspace | None, qualifiers: list[str],
              include: frozenset[str] = frozenset()) -> list[dict[str, Any]]:
    target = target_of(agent, workspace)
    production = production_qualifier(agent)
    rows: list[dict[str, Any]] = [{"kind": target.kind, "arn": target.arn}]
    for q in qualifiers:
        rows.append({"kind": f"{target.kind}_endpoint", "qualifier": q,
                     "arn": endpoint_arn(target, q), "production": q == production})
    memory = memory_arn(agent, target, workspace)
    if memory:
        rows.append({"kind": "memory", "arn": memory})
    return rows


def jwt_info(agent: Agent) -> dict[str, Any]:
    """The Bearer configuration a ``jwt`` agent accepts (no secrets live here)."""
    config = agent.inbound_auth_config or {}
    jwt = config.get("jwt") if isinstance(config.get("jwt"), dict) else config
    discovery = str((jwt or {}).get("discovery_url") or "")
    discovered = (agent.spec or {}).get("discovery") or {}
    return {
        "discovery_url": discovery or None,
        "issuer": discovery.removesuffix(_DISCOVERY_SUFFIX) if discovery else None,
        "allowed_clients": list((jwt or {}).get("allowed_clients") or []),
        "allowed_audience": list((jwt or {}).get("allowed_audience") or []),
        "allowed_scopes": list((jwt or {}).get("allowed_scopes") or []),
        "external": not discovery and bool(discovered),
    }


def notes(agent: Agent, workspace: Workspace | None, target: Target, mode: str,
          include: frozenset[str]) -> list[str]:
    """Machine-readable caveats the console renders as translated hints."""
    out = ["cross_account"]
    if mode == "jwt":
        out.insert(0, "jwt_no_iam")
    if target.kind == "harness":
        if not HARNESS_ACTION_VERIFIED:
            out.append("harness_action_unverified")
        if "stop_session" in include:
            out.append("harness_no_stop_session")
    if "memory" in include and memory_arn(agent, target, workspace) is None:
        out.append("memory_unresolved")  # the agent uses no memory, or none is recorded
    return out


# ---- samples ----


def _session_hint() -> str:
    return "runtimeSessionId must be at least 33 characters, e.g. a UUID4 plus a suffix"


def _runtime_samples(target: Target, qualifier: str) -> dict[str, str]:
    cli = f"""# {_session_hint()}
aws bedrock-agentcore invoke-agent-runtime \\
  --region {target.region} \\
  --agent-runtime-arn '{target.arn}' \\
  --qualifier {qualifier} \\
  --runtime-session-id "$(uuidgen)-$(date +%s)" \\
  --content-type application/json \\
  --cli-binary-format raw-in-base64-out \\
  --payload '{{"prompt": "Hello"}}' \\
  response.json   # a streaming agent writes its SSE events into this file
cat response.json"""
    python = f"""import json
import uuid

import boto3

client = boto3.client("bedrock-agentcore", region_name="{target.region}")
AGENT_ARN = "{target.arn}"
session_id = str(uuid.uuid4()) + "-session"  # {_session_hint()}

response = client.invoke_agent_runtime(
    agentRuntimeArn=AGENT_ARN,
    qualifier="{qualifier}",
    runtimeSessionId=session_id,
    contentType="application/json",
    payload=json.dumps({{"prompt": "Hello"}}).encode(),
)

if "text/event-stream" in response.get("contentType", ""):
    # streaming: one SSE line per event
    for line in response["response"].iter_lines(chunk_size=1):
        if line.startswith(b"data: "):
            print(line[6:].decode())
else:
    # non-streaming: one JSON body
    print(json.loads(response["response"].read()))"""
    ts = f"""import {{
  BedrockAgentCoreClient,
  InvokeAgentRuntimeCommand,
}} from "@aws-sdk/client-bedrock-agentcore";
import {{ randomUUID }} from "node:crypto";

const client = new BedrockAgentCoreClient({{ region: "{target.region}" }});

const response = await client.send(
  new InvokeAgentRuntimeCommand({{
    agentRuntimeArn: "{target.arn}",
    qualifier: "{qualifier}",
    runtimeSessionId: `${{randomUUID()}}-session`, // {_session_hint()}
    contentType: "application/json",
    payload: new TextEncoder().encode(JSON.stringify({{ prompt: "Hello" }})),
  }}),
);

if (response.contentType?.includes("text/event-stream")) {{
  // streaming: read the SSE body chunk by chunk
  for await (const chunk of response.response as AsyncIterable<Uint8Array>) {{
    process.stdout.write(new TextDecoder().decode(chunk));
  }}
}} else {{
  // non-streaming: one JSON body
  console.log(JSON.parse(await response.response!.transformToString()));
}}"""
    return {"cli": cli, "python": python, "ts": ts}


def _harness_samples(target: Target, qualifier: str) -> dict[str, str]:
    qualifier_cli = "" if qualifier == DEFAULT else f"  --qualifier {qualifier} \\\n"
    qualifier_py = "" if qualifier == DEFAULT else f'    qualifier="{qualifier}",\n'
    qualifier_ts = "" if qualifier == DEFAULT else f'    qualifier: "{qualifier}",\n'
    cli = f"""# TO VERIFY: the CLI command name of the InvokeHarness operation.
# {_session_hint()}
aws bedrock-agentcore invoke-harness \\
  --region {target.region} \\
  --harness-arn '{target.arn}' \\
{qualifier_cli}  --runtime-session-id "$(uuidgen)-$(date +%s)" \\
  --actor-id my-backend \\
  --messages '[{{"role": "user", "content": [{{"text": "Hello"}}]}}]' \\
  response.json   # the Harness always answers an event stream"""
    python = f"""import uuid

import boto3

client = boto3.client("bedrock-agentcore", region_name="{target.region}")

response = client.invoke_harness(
    harnessArn="{target.arn}",
{qualifier_py}    runtimeSessionId=str(uuid.uuid4()) + "-session",  # {_session_hint()}
    actorId="my-backend",
    messages=[{{"role": "user", "content": [{{"text": "Hello"}}]}}],
)

# streaming: the Harness answers an event stream
text = []
for event in response["stream"]:
    delta = event.get("contentBlockDelta", {{}}).get("delta", {{}})
    if "text" in delta:
        print(delta["text"], end="", flush=True)
        text.append(delta["text"])
# non-streaming callers simply join the deltas
answer = "".join(text)"""
    ts = f"""// TO VERIFY: the SDK v3 command name of the InvokeHarness operation.
import {{
  BedrockAgentCoreClient,
  InvokeHarnessCommand,
}} from "@aws-sdk/client-bedrock-agentcore";
import {{ randomUUID }} from "node:crypto";

const client = new BedrockAgentCoreClient({{ region: "{target.region}" }});

const response = await client.send(
  new InvokeHarnessCommand({{
    harnessArn: "{target.arn}",
{qualifier_ts}    runtimeSessionId: `${{randomUUID()}}-session`, // {_session_hint()}
    actorId: "my-backend",
    messages: [{{ role: "user", content: [{{ text: "Hello" }}] }}],
  }}),
);

let answer = "";
for await (const event of response.stream ?? []) {{
  const text = event.contentBlockDelta?.delta?.text;
  if (text) {{
    process.stdout.write(text); // streaming
    answer += text; // non-streaming: the joined answer
  }}
}}"""
    return {"cli": cli, "python": python, "ts": ts}


def _jwt_samples(target: Target, qualifier: str, jwt: dict[str, Any]) -> dict[str, str]:
    from urllib.parse import quote

    url = (f"https://bedrock-agentcore.{target.region}.amazonaws.com/runtimes/"
           f"{quote(target.arn, safe='')}/invocations?qualifier={qualifier}")
    scope = " ".join(jwt.get("allowed_scopes") or []) or "<scope>"
    client_id = (jwt.get("allowed_clients") or ["<client-id>"])[0]
    token_cmd = (f"# token: client-credentials grant at the token_endpoint of\n"
                 f"#   {jwt.get('discovery_url') or '<issuer>/.well-known/openid-configuration'}\n"
                 f"TOKEN=$(curl -s -X POST \"$TOKEN_ENDPOINT\" \\\n"
                 f"  -u '{client_id}:'\"$CLIENT_SECRET\" \\\n"
                 f"  -d grant_type=client_credentials -d scope='{scope}' | jq -r .access_token)")
    cli = f"""{token_cmd}

curl -N -X POST '{url}' \\
  -H "Authorization: Bearer $TOKEN" \\
  -H "Content-Type: application/json" \\
  -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: $(uuidgen)-$(date +%s)" \\
  -d '{{"prompt": "Hello"}}'   # -N prints a streaming answer as it arrives"""
    python = f"""import uuid

import requests

URL = "{url}"
token = "..."  # an access token from the agent's identity provider (client credentials)

response = requests.post(
    URL,
    headers={{
        "Authorization": f"Bearer {{token}}",
        "Content-Type": "application/json",
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": str(uuid.uuid4()) + "-session",
    }},
    json={{"prompt": "Hello"}},
    stream=True,
    timeout=300,
)
response.raise_for_status()
if "text/event-stream" in response.headers.get("content-type", ""):
    for line in response.iter_lines():  # streaming
        if line.startswith(b"data: "):
            print(line[6:].decode())
else:
    print(response.json())  # non-streaming"""
    ts = f"""import {{ randomUUID }} from "node:crypto";

const token = "..."; // an access token from the agent's identity provider

const response = await fetch(
  "{url}",
  {{
    method: "POST",
    headers: {{
      Authorization: `Bearer ${{token}}`,
      "Content-Type": "application/json",
      "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": `${{randomUUID()}}-session`,
    }},
    body: JSON.stringify({{ prompt: "Hello" }}),
  }},
);

if (response.headers.get("content-type")?.includes("text/event-stream")) {{
  const reader = response.body!.getReader(); // streaming
  for (;;) {{
    const {{ done, value }} = await reader.read();
    if (done) break;
    process.stdout.write(new TextDecoder().decode(value));
  }}
}} else {{
  console.log(await response.json()); // non-streaming
}}"""
    return {"cli": cli, "python": python, "ts": ts}


def samples(target: Target, mode: str, qualifier: str,
            jwt: dict[str, Any] | None = None) -> dict[str, str]:
    if mode == "jwt":
        return _jwt_samples(target, qualifier, jwt or {})
    if target.kind == "harness":
        return _harness_samples(target, qualifier)
    return _runtime_samples(target, qualifier)


# ---- the two answers ----


def agent_access(agent: Agent, workspace: Workspace | None, *, qualifier: str | None = None,
                 include: frozenset[str] = frozenset()) -> dict[str, Any]:
    """``GET /api/agents/{id}/invoke-access``."""
    target = target_of(agent, workspace)
    qualifiers = select_qualifiers(agent, workspace, qualifier)
    mode = mode_of(agent)
    sample_q = qualifier or production_qualifier(agent)
    jwt = jwt_info(agent) if mode == "jwt" else None
    policy = (None if mode == "jwt"
              else policy_document(statements(agent, workspace, qualifiers, include)))
    return {
        "agent_id": agent.id,
        "name": agent.name,
        "method": agent.method,
        "kind": target.kind,
        "mode": mode,
        "account": target.account,
        "region": target.region,
        "partition": target.partition,
        "arn": target.arn,
        "qualifiers": qualifiers,
        "available_qualifiers": endpoints(agent, workspace),
        "production_qualifier": production_qualifier(agent),
        "sample_qualifier": sample_q,
        "include": sorted(include),
        "resources": resources(agent, workspace, qualifiers, include),
        "policy": policy,
        "jwt": jwt,
        "samples": samples(target, mode, sample_q, jwt),
        "notes": notes(agent, workspace, target, mode, include),
        "actions": {"runtime": RUNTIME_ACTION, "harness": HARNESS_ACTION,
                    "harness_verified": HARNESS_ACTION_VERIFIED},
    }


def inventory_row(agent: Agent, workspace: Workspace | None) -> list[dict[str, Any]]:
    """One CSV / JSON row per (agent, endpoint); an agent without a resource is
    skipped by the caller."""
    target = target_of(agent, workspace)
    mode = mode_of(agent)
    production = production_qualifier(agent)
    return [{
        "agent_id": agent.id, "name": agent.name, "method": agent.method,
        "kind": target.kind, "mode": mode, "account": target.account,
        "region": target.region, "arn": target.arn, "qualifier": q,
        "endpoint_arn": endpoint_arn(target, q), "production": q == production,
    } for q in endpoints(agent, workspace)]


def project_access(agents: list[tuple[Agent, Workspace | None]], *,
                   qualifier: str | None = None,
                   include: frozenset[str] = frozenset()) -> dict[str, Any]:
    """``GET /api/projects/{id}/invoke-access``: the inventory of every callable agent
    and one merged policy over the ``iam`` ones. ``qualifier`` narrows each agent to
    that endpoint where it has one (``live``: production only); an agent without it
    keeps its production endpoint, so the merged policy still reaches every agent."""
    rows: list[dict[str, Any]] = []
    stmts: list[dict[str, Any]] = []
    jwt_agents: list[str] = []
    skipped: list[dict[str, str]] = []
    for agent, workspace in agents:
        try:
            rows.extend(inventory_row(agent, workspace))
        except AppError:
            skipped.append({"agent_id": agent.id, "name": agent.name, "reason": "no_resource"})
            continue
        if mode_of(agent) == "jwt":
            jwt_agents.append(agent.id)
            continue
        available = endpoints(agent, workspace)
        chosen = ([qualifier] if qualifier in available
                  else [production_qualifier(agent)] if qualifier else available)
        stmts.extend(statements(agent, workspace, chosen, include))
    merged = merge_statements(stmts)
    return {
        "inventory": rows,
        "policy": policy_document(merged) if merged else None,
        "jwt_agents": jwt_agents,
        "skipped": skipped,
        "qualifier": qualifier or None,
        "include": sorted(include),
    }


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def inventory_csv(rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(INVENTORY_FIELDS)
    for row in rows:
        writer.writerow([_cell(row.get(f)) for f in INVENTORY_FIELDS])
    return buffer.getvalue()


def dumps(document: Any) -> str:
    return json.dumps(document, indent=2, ensure_ascii=False)
