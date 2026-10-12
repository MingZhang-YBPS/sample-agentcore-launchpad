"""Curated answers (T35): the console API for the per-agent rule list. The enforcement
lives in the invoke chain (`services/answer_rules.match_for_agent`); this router only
manages the rules. Writes are journaled in `audit_events` because they change what
customers are told without a redeploy."""

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.routers.auth import enabled as auth_enabled
from app.routers.auth import require_identity
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.services import agent_access, answer_rules, issues
from app.services.audit import record_audit_event

router = APIRouter(
    prefix="/api",
    tags=["answer-rules"],
    # workspace isolation: a hidden `{agent_id}` / `{run_id}` reads as missing
    dependencies=[Depends(agent_access.require_path_ids_visible)],
)


class RuleBody(BaseModel):
    name: str = Field(default="", max_length=answer_rules.MAX_NAME)
    match: Literal["exact", "contains"] = "exact"
    pattern: str = Field(max_length=answer_rules.MAX_PATTERN)
    answer: str = Field(max_length=answer_rules.MAX_ANSWER)
    enabled: bool = True
    issue_id: str | None = Field(default=None, max_length=32)  # the issue this rule fixes


class RulePatch(BaseModel):
    name: str | None = Field(default=None, max_length=answer_rules.MAX_NAME)
    match: Literal["exact", "contains"] | None = None
    pattern: str | None = Field(default=None, max_length=answer_rules.MAX_PATTERN)
    answer: str | None = Field(default=None, max_length=answer_rules.MAX_ANSWER)
    enabled: bool | None = None


class RuleOrder(BaseModel):
    ids: list[str] = Field(max_length=answer_rules.MAX_RULES_PER_AGENT)


class RuleSwitch(BaseModel):
    enabled: bool


class RuleTest(BaseModel):
    question: str = Field(min_length=1, max_length=8000)


def _actor(request: Request) -> str:
    return require_identity(request).username if auth_enabled() else "river"


def _audit(ws: WorkspaceScope, actor: str, action: str, agent_id: str, rule_id: str = "") -> None:
    record_audit_event(workspace_id=ws.id, actor=actor, action=action,
                       target=f"agent:{agent_id}" + (f" rule:{rule_id}" if rule_id else ""))


@router.get("/agents/{agent_id}/rules")
def list_rules(
    agent_id: str, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    answer_rules.manageable_agent(db, ws.id, agent_id)
    return answer_rules.list_rules(db, ws.id, agent_id)


@router.post("/agents/{agent_id}/rules", status_code=201)
def create_rule(
    agent_id: str, req: RuleBody, request: Request, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    answer_rules.manageable_agent(db, ws.id, agent_id)
    actor = _actor(request)
    issue = issues.get_issue(db, ws.id, req.issue_id) if req.issue_id else None
    rule = answer_rules.create_rule(
        db, workspace_id=ws.id, agent_id=agent_id, name=req.name, match=req.match,
        pattern=req.pattern, answer=req.answer, enabled=req.enabled, actor=actor,
        source_issue_id=issue.id if issue else None,
    )
    if issue is not None and issue.agent_id == agent_id and issue.status == "open":
        issues.add_fix(db, issue, action="rule", ref=rule.id, actor=actor)
    _audit(ws, actor, "rule.create", agent_id, rule.id)
    return answer_rules.rule_out(rule)


@router.patch("/agents/{agent_id}/rules/{rule_id}")
def update_rule(
    agent_id: str, rule_id: str, req: RulePatch, request: Request,
    db: Session = Depends(get_db), ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    answer_rules.manageable_agent(db, ws.id, agent_id)
    actor = _actor(request)
    rule = answer_rules.update_rule(
        db, answer_rules.get_rule(db, ws.id, agent_id, rule_id), actor=actor,
        **req.model_dump(exclude_unset=True, exclude={"issue_id"}),
    )
    _audit(ws, actor, "rule.update", agent_id, rule.id)
    return answer_rules.rule_out(rule)


@router.delete("/agents/{agent_id}/rules/{rule_id}")
def delete_rule(
    agent_id: str, rule_id: str, request: Request, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, bool]:
    answer_rules.manageable_agent(db, ws.id, agent_id)
    rule = answer_rules.get_rule(db, ws.id, agent_id, rule_id)
    db.delete(rule)
    db.commit()
    _audit(ws, _actor(request), "rule.delete", agent_id, rule_id)
    return {"deleted": True}


@router.put("/agents/{agent_id}/rules-order")
def reorder_rules(
    agent_id: str, req: RuleOrder, request: Request, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    answer_rules.manageable_agent(db, ws.id, agent_id)
    answer_rules.reorder(db, ws.id, agent_id, req.ids)
    _audit(ws, _actor(request), "rule.reorder", agent_id)
    return answer_rules.list_rules(db, ws.id, agent_id)


@router.put("/agents/{agent_id}/rules-enabled")
def switch_rules(
    agent_id: str, req: RuleSwitch, request: Request, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    answer_rules.manageable_agent(db, ws.id, agent_id)
    actor = _actor(request)
    answer_rules.set_agent_enabled(db, ws.id, agent_id, req.enabled, actor)
    _audit(ws, actor, "rule.enable" if req.enabled else "rule.disable", agent_id)
    return answer_rules.list_rules(db, ws.id, agent_id)


@router.post("/agents/{agent_id}/rules/test")
def test_rules(
    agent_id: str, req: RuleTest, db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    answer_rules.manageable_agent(db, ws.id, agent_id)
    return answer_rules.dry_run(db, ws.id, agent_id, req.question)
