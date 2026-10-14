"""One-time move of installed presets off a superseded default turn budget.

``build_spec`` writes the resolved ``timeout_seconds`` into the stored spec and a repair
reads it back verbatim (``presets.options_from_spec``), so changing the catalogue
default alone never reaches an installed preset. At startup this module finds every
installed preset whose stored budget is still the legacy default
(``presets.timeout_migration_target``), rewrites it to the current default through the
ordinary repair path — the same compare-and-set claim and update job an administrator's
edit gets — journals the change and starts the in-place re-publish. Any other stored
value is an administrator's choice and is never touched.

Idempotent: a migrated spec carries the new default and never matches again. A preset
that cannot be repaired right now (a job in flight, an uninstall, a workspace that is
not ready) is skipped with a log line and picked up on the next start.
"""

import logging
from collections.abc import Callable

from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.core.project_scope import project_context
from app.models.ledger import Agent, Workspace
from app.services.audit import record_audit_event
from app.system_agents import presets as catalogue
from app.system_agents import service

logger = logging.getLogger("launchpad.system_agents")

AUDIT_ACTION = "system_agent.timeout_migrated"


def _candidates(db: Session) -> list[tuple[str, str]]:
    """``(agent id, preset key)`` of every installed preset still on a legacy budget."""
    out: list[tuple[str, str]] = []
    for agent in db.query(Agent).filter(Agent.system_key.isnot(None),
                                        Agent.status != "deleted").all():
        preset = catalogue.get_preset(agent.system_key or "")
        if preset is not None and catalogue.timeout_migration_target(preset, agent.spec):
            out.append((agent.id, preset.key))
    return out


def _migrate_one(db: Session, agent_id: str, preset_key: str) -> str | None:
    """Repair one preset onto the new budget; the started job's id, or None."""
    preset = catalogue.get_preset(preset_key)
    agent = db.get(Agent, agent_id)
    if preset is None or agent is None:
        return None
    target = catalogue.timeout_migration_target(preset, agent.spec)
    row = db.get(Workspace, agent.workspace_id) if agent.workspace_id else None
    if target is None or row is None:
        return None
    before = (agent.spec or {}).get("timeout_seconds")
    edit = catalogue.PresetEdit(values={"timeout_seconds": target})
    # the repair re-resolves against the row it claims; a concurrent edit that already
    # moved the budget wins (its value is an administrator's choice from then on)
    outcome = service.install_preset(db, row, preset, edit)
    if not outcome.changed or outcome.job is None:
        db.commit()
        return None
    record_audit_event(
        workspace_id=row.id, actor="system", db=db, target=agent.id,
        action=f"{AUDIT_ACTION} {preset.key} timeout_seconds {before}→{target}",
    )
    db.commit()
    logger.info("system preset %s: turn budget %s→%s s, re-publish job %s (agent %s)",
                preset.key, before, target, outcome.job.id, agent.id)
    return outcome.job.id


def migrate_preset_timeouts(start: Callable[[str], object] | None = None) -> list[str]:
    """Startup hook: migrate every matching preset, start each re-publish job with
    ``start`` (default ``deployer.pipeline.start_deploy_async``); the started job ids."""
    if start is None:
        from app.deployer.pipeline import start_deploy_async as start
    db = SessionLocal()
    started: list[str] = []
    try:
        for agent_id, preset_key in _candidates(db):
            project_id = db.get(Agent, agent_id).project_id
            with project_context(project_id):  # the preset row's own project
                try:
                    job_id = _migrate_one(db, agent_id, preset_key)
                except (AppError, ValueError) as exc:
                    db.rollback()
                    logger.warning("system preset %s: turn budget migration of agent %s "
                                   "skipped (%s); retried on the next start",
                                   preset_key, agent_id, exc)
                    continue
            if job_id:
                start(job_id)
                started.append(job_id)
        return started
    finally:
        db.close()
