"""SQLAlchemy engine / session for the local ledger database."""

import json
from collections.abc import Generator
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import DATA_DIR, get_settings
from app.core.project_scope import ProjectSession


class Base(DeclarativeBase):
    pass


DEFAULT_WORKSPACE_ID = "default"

# Every table whose rows belong to one (account, region) environment. `users`,
# `workspaces`, `user_workspaces`, `announcements`, and video tables are hub-global.
WORKSPACE_SCOPED_TABLES = (
    "agents",
    "deployments",
    "chat_sessions",
    "chat_messages",
    "api_keys",
    "policy_decisions",
    "policy_changes",
    "jobs",
    "eval_datasets",
    "eval_runs",
    "eval_recommendations",
    "online_eval_configs",
    "experiments",
    "runtime_canaries",
    "skill_lab_tasksets",
    "skill_lab_jobs",
    "assistant_conversations",
    "assistant_messages",
    "assistant_proposals",
    "agent_name_claims",
    "system_skill_records",
    "managed_memories",
    "assistant_evaluation_plans",
    "evaluation_asset_operations",
    "eval_pipelines",
    "audit_events",
    "share_links",
    "chat_feedback",
    "api_key_usage",
    "spec_snapshots",
    "release_bundles",
    "promotions",
    "resource_mappings",
    "answer_rules",
    "answer_rule_sets",
    "issues",
    "alert_rules",
    "identity_providers",
    "user_token_revocations",
    "oauth_pending_sessions",
    "user_grants",
    "criteria_sets",
    "criteria",
    "criterion_results",
    "annotation_tasks",
    "annotations",
    "calibration_records",
    "release_records",
    "waivers",
    "admission_candidates",
    "watch_configs",
    "resource_creators",
)

# Project boundary (design §4). STRICT rows always belong to one project; SHARED rows
# may carry NULL (workspace / platform level, readable from every project). The
# parent map drives the one-time backfill, in dependency order: a derived table
# takes its parent's project, an orphan its workspace's default project.
PROJECT_STRICT_TABLES = (
    "api_keys", "resource_creators", "eval_datasets", "eval_pipelines",
    "skill_lab_tasksets", "assistant_conversations", "criteria_sets",
    "chat_sessions", "chat_messages", "chat_feedback",
    "issues", "answer_rules", "answer_rule_sets", "oauth_pending_sessions", "user_grants",
    "release_bundles", "eval_runs", "online_eval_configs", "experiments",
    "annotation_tasks", "calibration_records", "release_records", "waivers",
    "admission_candidates", "watch_configs", "runtime_canaries", "api_key_usage",
    "share_links", "promotions", "eval_recommendations", "assistant_messages",
    "assistant_proposals", "assistant_evaluation_plans", "evaluation_asset_operations",
    "criteria", "criterion_results", "annotations", "skill_lab_jobs",
)
PROJECT_SHARED_TABLES = (
    # a system preset's deployments and spec snapshots belong to no project, like it
    "agents", "jobs", "managed_memories", "deployments", "spec_snapshots",
    "policy_decisions", "policy_changes",
    "audit_events", "alert_rules", "resource_mappings", "identity_providers",
    "user_token_revocations",
)
# yes-tables stamped from the workspace default at the migration moment (shared
# ones only these three: governance/audit/alert/mapping/connection rows stay NULL)
_PROJECT_BACKFILL_DIRECT = (
    "agents", "jobs", "managed_memories", "api_keys", "resource_creators", "eval_datasets",
    "eval_pipelines", "skill_lab_tasksets", "assistant_conversations",
)
# (table, parent column, parent table) — parents always listed before children
_PROJECT_BACKFILL_DERIVED = (
    ("criteria_sets", "agent_id", "agents"),
    ("deployments", "agent_id", "agents"),
    ("spec_snapshots", "agent_id", "agents"),
    ("chat_sessions", "agent_id", "agents"),
    ("chat_messages", "agent_id", "agents"),
    ("chat_feedback", "agent_id", "agents"),
    ("issues", "agent_id", "agents"),
    ("answer_rules", "agent_id", "agents"),
    ("answer_rule_sets", "agent_id", "agents"),
    ("oauth_pending_sessions", "agent_id", "agents"),
    ("user_grants", "agent_id", "agents"),
    ("release_bundles", "agent_id", "agents"),
    ("eval_runs", "agent_id", "agents"),
    ("online_eval_configs", "agent_id", "agents"),
    ("experiments", "agent_id", "agents"),
    ("annotation_tasks", "agent_id", "agents"),
    ("calibration_records", "agent_id", "agents"),
    ("release_records", "agent_id", "agents"),
    ("waivers", "agent_id", "agents"),
    ("admission_candidates", "agent_id", "agents"),
    ("watch_configs", "agent_id", "agents"),
    ("runtime_canaries", "champion_agent_id", "agents"),
    ("api_key_usage", "key_id", "api_keys"),
    ("share_links", "target_id", "agents"),
    ("share_links", "target_id", "annotation_tasks"),
    ("promotions", "bundle_id", "release_bundles"),
    ("eval_recommendations", "run_id", "eval_runs"),
    ("assistant_messages", "conversation_id", "assistant_conversations"),
    ("assistant_proposals", "conversation_id", "assistant_conversations"),
    ("assistant_evaluation_plans", "conversation_id", "assistant_conversations"),
    ("evaluation_asset_operations", "conversation_id", "assistant_conversations"),
    ("criteria", "set_id", "criteria_sets"),
    ("criterion_results", "run_id", "eval_runs"),
    ("annotations", "task_id", "annotation_tasks"),
    ("skill_lab_jobs", "taskset_id", "skill_lab_tasksets"),
)
PROJECTS_MIGRATION_MARKER = "migration.projects_v1"
# The one-time username -> users.id backfill of the SoD columns (design §8.3-7).
SOD_USER_IDS_MARKER = "migration.sod_user_ids_v1"


def _make_engine():
    settings = get_settings()
    url = make_url(settings.database_url)
    is_sqlite = url.get_backend_name() == "sqlite"
    is_file_sqlite = is_sqlite and url.database not in (None, "", ":memory:")
    if is_sqlite:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    if is_file_sqlite:
        return create_engine(
            settings.database_url,
            connect_args={"check_same_thread": False},
            # SQLAlchemy 2 defaults file SQLite databases to QueuePool(5+10). A
            # burst of sync FastAPI requests can consume that fixed pool and
            # block every worker for 30 seconds. SQLite connections are cheap
            # and request sessions already close deterministically, so avoid
            # the artificial cap and close each DBAPI connection per session.
            poolclass=NullPool,
        )
    if is_sqlite:
        return create_engine(
            settings.database_url,
            connect_args={"check_same_thread": False},
        )
    return create_engine(settings.database_url)


engine = _make_engine()
SessionLocal = sessionmaker(
    bind=engine, class_=ProjectSession, autoflush=False, expire_on_commit=False,
)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(bind=None) -> None:
    """Bring the ledger up to the models and seed the default workspace.

    ``bind`` defaults to the process engine; tests pass an upgrade candidate so
    they exercise this exact sequence rather than a copy of it.
    """
    bind = bind if bind is not None else engine
    # every model module, so an out-of-process caller (scripts/) gets every table
    from app.evaluation import models as _eval_models  # noqa: F401
    from app.models import announcement as _announcement_models  # noqa: F401
    from app.models import assistant as _assistant_models  # noqa: F401
    from app.models import dlc as _dlc_models  # noqa: F401 — Agent-DLC tables
    from app.models import platform_settings as _platform_settings  # noqa: F401
    from app.models import projects as _project_models  # noqa: F401 — design §2.2
    from app.models import resource_mapping as _mapping_models  # noqa: F401
    from app.models import selfservice as _selfservice_models  # noqa: F401 — T35/T36
    from app.models import video as _video_models  # noqa: F401 — register tables before create_all
    from app.optimization import models as _optimization_models  # noqa: F401
    from app.skill_lab import models as _skill_lab_models  # noqa: F401

    Base.metadata.create_all(bind=bind)
    _migrate(bind)
    # Seeding after the drift check so a database that is behind the models is
    # never left half-migrated *and* half-seeded.
    assert_no_schema_drift(bind)
    _seed_default_workspace(bind)
    from app.services.videos import seed_legacy_catalog

    seed_legacy_catalog(bind)
    _adopt_unscoped_account_audit_rows(bind)
    assert_every_row_has_a_workspace(bind)
    _seed_default_projects(bind)
    _backfill_sod_user_ids(bind)
    assert_every_row_has_a_project(bind)


# Console-account journal actions (routers/users.py, routers/auth.py,
# scripts/reset_admin_password.py). They are hub-global and are stamped with the
# hub's own `default` workspace; build 26d627a6 wrote them with a NULL workspace,
# which `assert_every_row_has_a_workspace` then refused at the next startup.
ACCOUNT_AUDIT_ACTIONS = (
    "user.create",
    "user.update",
    "user.delete",
    "auth.password_change",
    "admin.password_change",
    "admin.password_reset_to_config",
)


def _adopt_unscoped_account_audit_rows(bind) -> None:
    """Give the account-journal rows that one build wrote without a workspace the
    hub's. Narrow on purpose: only these actions, so any OTHER unstamped row still
    fails startup the way the invariant intends."""
    from sqlalchemy import bindparam, inspect, text

    if "audit_events" not in inspect(bind).get_table_names():
        return
    statement = text(
        "UPDATE audit_events SET workspace_id = :ws "
        "WHERE workspace_id IS NULL AND action IN :actions"
    ).bindparams(bindparam("actions", expanding=True))
    with bind.begin() as conn:
        conn.execute(
            statement, {"ws": DEFAULT_WORKSPACE_ID, "actions": list(ACCOUNT_AUDIT_ACTIONS)}
        )


def schema_drift(bind) -> dict[str, list[str]]:
    """Model columns missing from the live database, per table.

    `create_all` only creates tables that do not exist yet, so a column added to a
    model is invisible to an **existing** ledger until `_migrate` adds it. Forgetting
    that entry is silent in the hermetic suite (every test DB is freshly created) and
    surfaces in production as a 500 on whichever request first selects the column.
    This turns that class of mistake into one legible failure.
    """
    from sqlalchemy import inspect

    inspector = inspect(bind)
    live_tables = set(inspector.get_table_names())
    drift: dict[str, list[str]] = {}
    for name, table in Base.metadata.tables.items():
        if name not in live_tables:
            continue  # create_all handles a table that does not exist at all
        live_columns = {c["name"] for c in inspector.get_columns(name)}
        missing = [c.name for c in table.columns if c.name not in live_columns]
        if missing:
            drift[name] = missing
    return drift


def assert_no_schema_drift(bind) -> None:
    drift = schema_drift(bind)
    if not drift:
        return
    lines = [
        f"  ALTER TABLE {table} ADD COLUMN {column} ...;"
        for table, columns in sorted(drift.items())
        for column in columns
    ]
    raise RuntimeError(
        "ledger schema is behind the models — add the column(s) to `_migrate()` in "
        "app/core/db.py so existing databases are upgraded on startup:\n"
        + "\n".join(lines)
    )


def _migrate(bind) -> None:
    """Additive column migrations for the local SQLite ledger (no Alembic).

    Every column added to a model after its table shipped needs an entry here, or
    `assert_no_schema_drift` fails startup. Keep the statements idempotent.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    if "agents" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("agents")}
        if "registry_record_id" not in existing:
            with bind.begin() as conn:
                conn.execute(
                    text("ALTER TABLE agents ADD COLUMN registry_record_id VARCHAR(64)")
                )
        if "system_key" not in existing:
            with bind.begin() as conn:
                conn.execute(text("ALTER TABLE agents ADD COLUMN system_key VARCHAR(64)"))
                conn.execute(
                    text("CREATE INDEX IF NOT EXISTS ix_agents_system_key ON agents (system_key)")
                )
        if "attachment_version" not in existing:
            with bind.begin() as conn:
                conn.execute(text("ALTER TABLE agents ADD COLUMN attachment_version VARCHAR(16)"))
        if "endpoint_mode" not in existing:
            with bind.begin() as conn:
                conn.execute(
                    text("ALTER TABLE agents ADD COLUMN endpoint_mode VARCHAR(16) "
                         "DEFAULT 'default'")
                )
    if "deployments" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("deployments")}
        if "image_digest" not in existing:
            with bind.begin() as conn:
                conn.execute(
                    text("ALTER TABLE deployments ADD COLUMN image_digest VARCHAR(80)")
                )
    if "users" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("users")}
        if "permissions" not in existing:
            with bind.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN permissions JSON"))
        if "first_login_at" not in existing:
            # no backfill: a prior login's time is unknown, so TTFA falls back to
            # created_at for accounts that already logged in
            with bind.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN first_login_at DATETIME"))
    if "eval_datasets" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("eval_datasets")}
        additions = {
            "description": "ALTER TABLE eval_datasets ADD COLUMN description TEXT DEFAULT ''",
            "cloud": "ALTER TABLE eval_datasets ADD COLUMN cloud JSON",
            "role": "ALTER TABLE eval_datasets ADD COLUMN role VARCHAR(16) DEFAULT 'scratch'",
            "criteria_set_id": "ALTER TABLE eval_datasets ADD COLUMN criteria_set_id VARCHAR(32)",
            "split_of": "ALTER TABLE eval_datasets ADD COLUMN split_of VARCHAR(16)",
            "split": "ALTER TABLE eval_datasets ADD COLUMN split VARCHAR(16)",
        }
        for column, ddl in additions.items():
            if column not in existing:
                with bind.begin() as conn:
                    conn.execute(text(ddl))
    if "eval_runs" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("eval_runs")}
        if "dataset_version" not in existing:
            with bind.begin() as conn:
                conn.execute(
                    text("ALTER TABLE eval_runs ADD COLUMN dataset_version VARCHAR(16)")
                )
        for column, ddl in (
            ("name", "ALTER TABLE eval_runs ADD COLUMN name VARCHAR(64)"),
            ("description", "ALTER TABLE eval_runs ADD COLUMN description TEXT"),
            ("log_source", "ALTER TABLE eval_runs ADD COLUMN log_source JSON"),
            ("budget_stops", "ALTER TABLE eval_runs ADD COLUMN budget_stops JSON"),
            ("criteria_set_id", "ALTER TABLE eval_runs ADD COLUMN criteria_set_id VARCHAR(32)"),
            ("criteria_set_version",
             "ALTER TABLE eval_runs ADD COLUMN criteria_set_version INTEGER"),
            ("agent_version", "ALTER TABLE eval_runs ADD COLUMN agent_version VARCHAR(32)"),
            ("endpoint_qualifier",
             "ALTER TABLE eval_runs ADD COLUMN endpoint_qualifier VARCHAR(64)"),
            ("evaluator_set_hash",
             "ALTER TABLE eval_runs ADD COLUMN evaluator_set_hash VARCHAR(64)"),
            ("split", "ALTER TABLE eval_runs ADD COLUMN split VARCHAR(16)"),
            ("repeats", "ALTER TABLE eval_runs ADD COLUMN repeats INTEGER DEFAULT 1"),
            ("repeat_mode",
             "ALTER TABLE eval_runs ADD COLUMN repeat_mode VARCHAR(16) DEFAULT 'all'"),
            ("cost_estimate", "ALTER TABLE eval_runs ADD COLUMN cost_estimate JSON"),
            ("cost_actual", "ALTER TABLE eval_runs ADD COLUMN cost_actual JSON"),
            ("denominator", "ALTER TABLE eval_runs ADD COLUMN denominator JSON"),
            ("criteria_summary", "ALTER TABLE eval_runs ADD COLUMN criteria_summary JSON"),
            ("attempts", "ALTER TABLE eval_runs ADD COLUMN attempts JSON"),
            ("session_failures", "ALTER TABLE eval_runs ADD COLUMN session_failures JSON"),
            ("scenario_failures", "ALTER TABLE eval_runs ADD COLUMN scenario_failures JSON"),
        ):
            if column not in existing:
                with bind.begin() as conn:
                    conn.execute(text(ddl))
        # The former multi-actor/multi-session procedure ledger column (`execution`,
        # SE-046) is no longer mapped; an existing column is simply left in place and
        # ignored — SQLite needs no drop for the model to load.
    if "evaluation_asset_operations" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("evaluation_asset_operations")}
        if "pinned" not in existing:
            # operations approved before identity pinning existed get an EMPTY pin: the
            # worker/cleanup refuse them (review required) instead of inventing bindings
            with bind.begin() as conn:
                conn.execute(text("ALTER TABLE evaluation_asset_operations ADD COLUMN pinned JSON"))
    if "experiments" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("experiments")}
        additions = {
            "running_action": "ALTER TABLE experiments ADD COLUMN running_action VARCHAR(24)",
            "progress": "ALTER TABLE experiments ADD COLUMN progress TEXT",
        }
        for column, ddl in additions.items():
            if column not in existing:
                with bind.begin() as conn:
                    conn.execute(text(ddl))
    if "chat_sessions" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("chat_sessions")}
        if "ended_at" not in existing:
            with bind.begin() as conn:
                conn.execute(text("ALTER TABLE chat_sessions ADD COLUMN ended_at DATETIME"))
    if "skill_lab_tasksets" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("skill_lab_tasksets")}
        if "sample" not in existing:
            with bind.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE skill_lab_tasksets "
                        "ADD COLUMN sample BOOLEAN DEFAULT 0 NOT NULL"
                    )
                )
    for table, column, ddl in (
        ("chat_messages", "attachments", "ALTER TABLE chat_messages ADD COLUMN attachments JSON"),
        ("chat_sessions", "runtime_version",
         "ALTER TABLE chat_sessions ADD COLUMN runtime_version VARCHAR(16)"),
        ("eval_recommendations", "accepted",
         "ALTER TABLE eval_recommendations ADD COLUMN accepted JSON"),
        ("eval_recommendations", "edit",
         "ALTER TABLE eval_recommendations ADD COLUMN edit JSON"),
    ):
        if table in inspector.get_table_names():
            existing = {c["name"] for c in inspector.get_columns(table)}
            if column not in existing:
                with bind.begin() as conn:
                    conn.execute(text(ddl))
    _migrate_inbound_auth_columns(bind)
    _migrate_identity_provider_columns(bind)
    _migrate_oauth_session_columns(bind)
    _migrate_assistant_columns(bind)
    _migrate_workspace_tier(bind)
    _migrate_api_key_scope(bind)
    _migrate_promotion_execution(bind)
    _migrate_share_link_channels(bind)
    _migrate_workspace_columns(bind)
    _migrate_selfservice_columns(bind)
    _migrate_system_key_index(bind)
    _migrate_system_skill_records_columns(bind)
    _migrate_system_skill_records_index(bind)
    _migrate_identity_indexes(bind)
    _migrate_managed_memories_index(bind)
    _migrate_api_key_lifecycle_columns(bind)
    _migrate_creator_columns(bind)
    _migrate_resource_creators_index(bind)
    _migrate_project_columns(bind)
    _migrate_template_project_column(bind)
    _migrate_actor_user_id_columns(bind)


def _migrate_actor_user_id_columns(bind) -> None:
    """The immutable `users.id` beside the usernames that identify a person (design §8.2-4).

    `users.session_epoch` (forced logout, §7.4: 0 = the cookie stamp of today);
    `audit_events.actor_user_id` and `share_links.created_by_user_id` (§5.8, §5.10);
    and the separation-of-duties columns (§3.6) — a username can be re-registered after
    the account is deleted, an id cannot. Every new column is nullable (NULL = compare
    by name, as before); `_backfill_sod_user_ids` fills the SoD ones once.
    """
    from sqlalchemy import inspect, text

    additions = {
        "users": {
            "session_epoch": "ALTER TABLE users ADD COLUMN session_epoch INTEGER DEFAULT 0",
        },
        "chat_sessions": {
            "input_tokens": "ALTER TABLE chat_sessions ADD COLUMN input_tokens INTEGER DEFAULT 0",
            "output_tokens": "ALTER TABLE chat_sessions ADD COLUMN output_tokens INTEGER DEFAULT 0",
            "total_tokens": "ALTER TABLE chat_sessions ADD COLUMN total_tokens INTEGER DEFAULT 0",
        },
        "audit_events": {
            "actor_user_id": "ALTER TABLE audit_events ADD COLUMN actor_user_id VARCHAR(32)",
        },
        "share_links": {
            "created_by_user_id": (
                "ALTER TABLE share_links ADD COLUMN created_by_user_id VARCHAR(32)"
            ),
        },
        "promotions": {
            "requested_by_user_id": (
                "ALTER TABLE promotions ADD COLUMN requested_by_user_id VARCHAR(32)"
            ),
            "reviewed_by_user_id": (
                "ALTER TABLE promotions ADD COLUMN reviewed_by_user_id VARCHAR(32)"
            ),
        },
        "release_records": {
            "requested_by_user_id": (
                "ALTER TABLE release_records ADD COLUMN requested_by_user_id VARCHAR(32)"
            ),
            "decided_by_user_id": (
                "ALTER TABLE release_records ADD COLUMN decided_by_user_id VARCHAR(32)"
            ),
        },
        "waivers": {
            "requested_by_user_id": (
                "ALTER TABLE waivers ADD COLUMN requested_by_user_id VARCHAR(32)"
            ),
            "approved_by_user_id": (
                "ALTER TABLE waivers ADD COLUMN approved_by_user_id VARCHAR(32)"
            ),
        },
        "criteria_sets": {
            "created_by_user_id": (
                "ALTER TABLE criteria_sets ADD COLUMN created_by_user_id VARCHAR(32)"
            ),
            "updated_by_user_id": (
                "ALTER TABLE criteria_sets ADD COLUMN updated_by_user_id VARCHAR(32)"
            ),
            "signed_by_user_id": (
                "ALTER TABLE criteria_sets ADD COLUMN signed_by_user_id VARCHAR(32)"
            ),
        },
    }
    inspector = inspect(bind)
    live_tables = set(inspector.get_table_names())
    for table, columns in additions.items():
        if table not in live_tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for column, ddl in columns.items():
            if column not in existing:
                with bind.begin() as conn:
                    conn.execute(text(ddl))
    if "users" in live_tables:
        with bind.begin() as conn:
            # `ADD COLUMN ... DEFAULT 0` fills existing rows on SQLite; a NULL can still
            # come from a ledger whose column predates the default
            conn.execute(text("UPDATE users SET session_epoch = 0 WHERE session_epoch IS NULL"))


# (table, username column, its *_user_id column, when the person acted). The time
# bounds the match: only an account created no later than the action can be its actor.
_SOD_USER_ID_BACKFILL = (
    ("promotions", "requested_by", "requested_by_user_id", "created_at"),
    ("promotions", "reviewed_by", "reviewed_by_user_id", "reviewed_at"),
    ("release_records", "requested_by", "requested_by_user_id", "created_at"),
    ("release_records", "decided_by", "decided_by_user_id", "decided_at"),
    ("waivers", "requested_by", "requested_by_user_id", "created_at"),
    ("waivers", "approved_by", "approved_by_user_id", "approved_at"),
    ("criteria_sets", "created_by", "created_by_user_id", "created_at"),
    ("criteria_sets", "updated_by", "updated_by_user_id", "updated_at"),
    ("criteria_sets", "signed_by", "signed_by_user_id", "signed_at"),
)


def _backfill_sod_user_ids(bind) -> None:
    """Once: give each historical SoD username the id of the account that held it then.

    The account currently holding the username (case-insensitively, `username_key`)
    is taken only when it was created no later than the action — a username
    re-registered after the original account was deleted is a different person, and
    that row keeps NULL (compared by name, exactly as before). The built-in admin has
    no `users` row, so its rows keep NULL too. Guarded by a marker rather than re-run
    every startup: a row a new write path left NULL must not be filled by a guess.
    """
    from sqlalchemy import inspect, text

    live_tables = set(inspect(bind).get_table_names())
    if "users" not in live_tables or "platform_settings" not in live_tables:
        return
    with bind.begin() as conn:
        done = conn.execute(
            text("SELECT 1 FROM platform_settings WHERE key = :k"),
            {"k": SOD_USER_IDS_MARKER},
        ).first()
        if done is not None:
            return
        for table, name_col, id_col, at_col in _SOD_USER_ID_BACKFILL:
            if table not in live_tables:
                continue
            conn.execute(
                text(
                    f"UPDATE {table} SET {id_col} = (SELECT u.id FROM users u"  # noqa: S608
                    f" WHERE u.username_key = lower({table}.{name_col})"
                    f" AND u.created_at <= COALESCE({table}.{at_col}, {table}.created_at))"
                    f" WHERE {id_col} IS NULL AND {name_col} IS NOT NULL AND {name_col} <> ''"
                )
            )
        conn.execute(
            text(
                "INSERT INTO platform_settings (key, value, updated_by, updated_at)"
                " VALUES (:k, 'done', 'migration', :now)"
            ),
            {"k": SOD_USER_IDS_MARKER, "now": _now_sql()},
        )


def _migrate_template_project_column(bind) -> None:
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    if "shared_templates" not in inspector.get_table_names():
        return
    if "source_project_id" not in {c["name"] for c in inspector.get_columns("shared_templates")}:
        with bind.begin() as conn:
            # NULL = published before projects; unpublishing reads the source agent anyway
            conn.execute(
                text("ALTER TABLE shared_templates ADD COLUMN source_project_id VARCHAR(32)")
            )


def _migrate_creator_columns(bind) -> None:
    """The creator of every user-managed resource (`app.services.creators`):
    `created_by` (`users.id`, indexed — visibility filters on it) and
    `created_by_name` (display). Existing rows keep NULL — a legacy row has an
    unknown creator and is never backfilled.

    `managed_memories` already carried `created_by` holding a **username**; it is
    reinterpreted exactly once (guarded on `created_by_name` being absent): the
    username moves to `created_by_name`, and `created_by` becomes the matching
    `users.id` for console-created rows (NULL when no `users` row has that name —
    the config admin / auth-disabled operator) and NULL for adopted rows, which
    pre-existed in AWS and so have no owner.
    """
    from sqlalchemy import inspect, text

    additions = {
        "agents": {
            "created_by": "ALTER TABLE agents ADD COLUMN created_by VARCHAR(32)",
            "created_by_name": "ALTER TABLE agents ADD COLUMN created_by_name VARCHAR(64)",
        },
        "eval_datasets": {
            "created_by": "ALTER TABLE eval_datasets ADD COLUMN created_by VARCHAR(32)",
            "created_by_name": "ALTER TABLE eval_datasets ADD COLUMN created_by_name VARCHAR(64)",
        },
        "eval_pipelines": {
            "created_by": "ALTER TABLE eval_pipelines ADD COLUMN created_by VARCHAR(32)",
            "created_by_name": (
                "ALTER TABLE eval_pipelines ADD COLUMN created_by_name VARCHAR(64)"
            ),
        },
        "eval_runs": {
            "created_by": "ALTER TABLE eval_runs ADD COLUMN created_by VARCHAR(32)",
            "created_by_name": "ALTER TABLE eval_runs ADD COLUMN created_by_name VARCHAR(64)",
        },
        "online_eval_configs": {
            "created_by": "ALTER TABLE online_eval_configs ADD COLUMN created_by VARCHAR(32)",
            "created_by_name": (
                "ALTER TABLE online_eval_configs ADD COLUMN created_by_name VARCHAR(64)"
            ),
        },
        "experiments": {
            "created_by": "ALTER TABLE experiments ADD COLUMN created_by VARCHAR(32)",
            "created_by_name": "ALTER TABLE experiments ADD COLUMN created_by_name VARCHAR(64)",
        },
        "runtime_canaries": {
            "created_by": "ALTER TABLE runtime_canaries ADD COLUMN created_by VARCHAR(32)",
            "created_by_name": (
                "ALTER TABLE runtime_canaries ADD COLUMN created_by_name VARCHAR(64)"
            ),
        },
        "skill_lab_jobs": {
            "created_by": "ALTER TABLE skill_lab_jobs ADD COLUMN created_by VARCHAR(32)",
            "created_by_name": (
                "ALTER TABLE skill_lab_jobs ADD COLUMN created_by_name VARCHAR(64)"
            ),
        },
        "skill_lab_tasksets": {
            "created_by": "ALTER TABLE skill_lab_tasksets ADD COLUMN created_by VARCHAR(32)",
            "created_by_name": (
                "ALTER TABLE skill_lab_tasksets ADD COLUMN created_by_name VARCHAR(64)"
            ),
        },
    }
    inspector = inspect(bind)
    live_tables = set(inspector.get_table_names())
    for table, columns in additions.items():
        if table not in live_tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for column, ddl in columns.items():
            if column not in existing:
                with bind.begin() as conn:
                    conn.execute(text(ddl))
        with bind.begin() as conn:
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_{table}_created_by "
                    f"ON {table} (created_by)"
                )
            )

    if "managed_memories" not in live_tables:
        return
    existing = {c["name"] for c in inspector.get_columns("managed_memories")}
    if "created_by_name" not in existing:
        with bind.begin() as conn:
            conn.execute(
                text("ALTER TABLE managed_memories ADD COLUMN created_by_name VARCHAR(64)")
            )
            conn.execute(text("UPDATE managed_memories SET created_by_name = created_by"))
            if "users" in live_tables:
                conn.execute(
                    text(
                        "UPDATE managed_memories SET created_by = ("
                        "SELECT u.id FROM users u "
                        "WHERE u.username = managed_memories.created_by_name"
                        ") WHERE origin = 'created'"
                    )
                )
            else:
                conn.execute(
                    text("UPDATE managed_memories SET created_by = NULL WHERE origin = 'created'")
                )
            conn.execute(
                text("UPDATE managed_memories SET created_by = NULL WHERE origin = 'adopted'")
            )
    with bind.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_managed_memories_created_by "
                "ON managed_memories (created_by)"
            )
        )


def _migrate_resource_creators_index(bind) -> None:
    """The unique (workspace, kind, resource) index of `resource_creators`: a
    retried record of the same resource leaves one row. `create_all` builds it
    with the table; this keeps a ledger whose table lost it honest."""
    from sqlalchemy import inspect, text

    if "resource_creators" not in inspect(bind).get_table_names():
        return
    with bind.begin() as conn:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_resource_creators_workspace_kind_resource "
                "ON resource_creators (workspace_id, kind, resource_id)"
            )
        )


def _migrate_api_key_lifecycle_columns(bind) -> None:
    """SE-054 credential lifecycle on `api_keys`: creator + last use. Existing rows
    keep NULL — a legacy key has an unknown creator and is never offboarded."""
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    if "api_keys" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("api_keys")}
    if "created_by" in existing and "created_by_name" not in existing:
        # A ledger written by the T16 line (`_migrate_api_key_scope` before it
        # stopped adding `created_by`) stored the minting **username** in
        # `created_by`. Reinterpret it exactly once, like `managed_memories` in
        # `_migrate_creator_columns`: the username moves to `created_by_name` and
        # `created_by` becomes the matching `users.id` — NULL when no `users` row
        # has that name (the config admin / auth-disabled operator), so such a key
        # is never treated as offboarded.
        with bind.begin() as conn:
            conn.execute(text("ALTER TABLE api_keys ADD COLUMN created_by_name VARCHAR(64)"))
            conn.execute(text("UPDATE api_keys SET created_by_name = created_by"))
            if "users" in inspector.get_table_names():
                conn.execute(
                    text(
                        "UPDATE api_keys SET created_by = ("
                        "SELECT u.id FROM users u "
                        "WHERE u.username = api_keys.created_by_name)"
                    )
                )
            else:
                conn.execute(text("UPDATE api_keys SET created_by = NULL"))
        existing.add("created_by_name")
    additions = {
        "created_by": "ALTER TABLE api_keys ADD COLUMN created_by VARCHAR(32)",
        "created_by_name": "ALTER TABLE api_keys ADD COLUMN created_by_name VARCHAR(64)",
        "last_used_at": "ALTER TABLE api_keys ADD COLUMN last_used_at DATETIME",
    }
    for column, ddl in additions.items():
        if column not in existing:
            with bind.begin() as conn:
                conn.execute(text(ddl))
    with bind.begin() as conn:
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_api_keys_created_by ON api_keys (created_by)"
            )
        )


def _migrate_inbound_auth_columns(bind) -> None:
    """Identity columns the earlier identity fork added (workspace policy blob,
    per-agent inbound-auth snapshot). A ledger upgraded by that fork already has
    them — each ALTER is guarded — and a ledger that never ran it gets them here.
    All NULL on upgrade: NULL mode reads back as IAM."""
    from sqlalchemy import inspect, text

    additions = {
        "workspaces": {
            "settings": "ALTER TABLE workspaces ADD COLUMN settings JSON",
        },
        "agents": {
            "inbound_auth_mode": "ALTER TABLE agents ADD COLUMN inbound_auth_mode VARCHAR(8)",
            "inbound_auth_config": "ALTER TABLE agents ADD COLUMN inbound_auth_config JSON",
        },
    }
    inspector = inspect(bind)
    live_tables = set(inspector.get_table_names())
    for table, columns in additions.items():
        if table not in live_tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for column, ddl in columns.items():
            if column not in existing:
                with bind.begin() as conn:
                    conn.execute(text(ddl))


def _migrate_identity_provider_columns(bind) -> None:
    """P1 Connection columns on the `identity_providers` table the earlier
    identity fork created (its rows keep NULL: display-only fields)."""
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    if "identity_providers" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("identity_providers")}
    additions = {
        "client_id": "ALTER TABLE identity_providers ADD COLUMN client_id VARCHAR(256)",
        "scopes": "ALTER TABLE identity_providers ADD COLUMN scopes JSON",
        "template": "ALTER TABLE identity_providers ADD COLUMN template VARCHAR(32)",
        "description": "ALTER TABLE identity_providers ADD COLUMN description VARCHAR(200)",
    }
    for column, ddl in additions.items():
        if column not in existing:
            with bind.begin() as conn:
                conn.execute(text(ddl))


def _migrate_oauth_session_columns(bind) -> None:
    """P3 caller kind on in-flight 3LO sessions. Upgraded rows read "iam": every
    session recorded before inbound JWT was asked over SigV4."""
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    if "oauth_pending_sessions" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("oauth_pending_sessions")}
    if "caller_kind" not in existing:
        with bind.begin() as conn:
            conn.execute(text(
                "ALTER TABLE oauth_pending_sessions "
                "ADD COLUMN caller_kind VARCHAR(16) DEFAULT 'iam' NOT NULL"
            ))


def _migrate_identity_indexes(bind) -> None:
    """The unique indexes of the identity tables. `create_all` builds them
    with a fresh table; this restores them on a table that predates them. Runs
    after `_migrate_workspace_columns` because each spans `workspace_id`."""
    from sqlalchemy import inspect, text

    indexes = {
        "identity_providers": (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_providers_ws_kind_name "
            "ON identity_providers (workspace_id, kind, name)"
        ),
        "user_token_revocations": (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_token_revocations_ws_provider_user "
            "ON user_token_revocations (workspace_id, provider, user_id)"
        ),
        "oauth_pending_sessions": (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_oauth_pending_sessions_session_uri "
            "ON oauth_pending_sessions (workspace_id, session_uri)"
        ),
        "user_grants": (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_grants_ws_user_provider_agent "
            "ON user_grants (workspace_id, user_id, provider, agent_id)"
        ),
    }
    live_tables = set(inspect(bind).get_table_names())
    for table, ddl in indexes.items():
        if table in live_tables:
            with bind.begin() as conn:
                conn.execute(text(ddl))


def _migrate_api_key_scope(bind) -> None:
    """T16: per-key agent scope, expiry, rate limit and usage stamps.

    Every existing key keeps NULL scope/expiry/limit, i.e. today's behaviour.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    if "api_keys" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("api_keys")}
    additions = {
        "agent_ids": "ALTER TABLE api_keys ADD COLUMN agent_ids JSON",
        "expires_at": "ALTER TABLE api_keys ADD COLUMN expires_at DATETIME",
        "rate_per_minute": "ALTER TABLE api_keys ADD COLUMN rate_per_minute INTEGER",
        "last_used_at": "ALTER TABLE api_keys ADD COLUMN last_used_at DATETIME",
        "use_count": "ALTER TABLE api_keys ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0",
        # `created_by` / `created_by_name` belong to `_migrate_api_key_lifecycle_columns`
    }
    for column, ddl in additions.items():
        if column not in existing:
            with bind.begin() as conn:
                conn.execute(text(ddl))


def _migrate_share_link_channels(bind) -> None:
    """T30: channel (Slack / Feishu) settings and secrets on a share link. Additive
    and nullable: every existing web link keeps NULL, i.e. today's behaviour."""
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    if "share_links" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("share_links")}
    additions = {
        "channel_config": "ALTER TABLE share_links ADD COLUMN channel_config JSON",
        "channel_secrets": "ALTER TABLE share_links ADD COLUMN channel_secrets JSON",
    }
    for column, ddl in additions.items():
        if column not in existing:
            with bind.begin() as conn:
                conn.execute(text(ddl))


def _migrate_selfservice_columns(bind) -> None:
    """T34/T35: the reviewer's correction text and the "answered by a rule" marker.

    Additive and nullable: every existing answer was produced by the model and every
    existing verdict has no correction."""
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    additions = {
        "chat_feedback": {
            "correction": "ALTER TABLE chat_feedback ADD COLUMN correction TEXT",
        },
        "chat_messages": {
            "answered_by": "ALTER TABLE chat_messages ADD COLUMN answered_by VARCHAR(48)",
        },
    }
    for table, columns in additions.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for column, ddl in columns.items():
            if column not in existing:
                with bind.begin() as conn:
                    conn.execute(text(ddl))


def _migrate_promotion_execution(bind) -> None:
    """T26/T27: promotion execution state and the per-workspace release policy.

    Additive and nullable: a promotion approved before execution existed simply has no
    stages yet, and a workspace with no policy uses the tier defaults."""
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    tables = set(inspector.get_table_names())
    additions: dict[str, dict[str, str]] = {
        "promotions": {
            "stages": "ALTER TABLE promotions ADD COLUMN stages JSON",
            "execution": "ALTER TABLE promotions ADD COLUMN execution JSON",
            "previous_bundle_id": (
                "ALTER TABLE promotions ADD COLUMN previous_bundle_id VARCHAR(32)"
            ),
            "started_at": "ALTER TABLE promotions ADD COLUMN started_at DATETIME",
            "finished_at": "ALTER TABLE promotions ADD COLUMN finished_at DATETIME",
        },
        "workspaces": {
            "release_policy": "ALTER TABLE workspaces ADD COLUMN release_policy JSON",
        },
    }
    for table, columns in additions.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for column, ddl in columns.items():
            if column not in existing:
                with bind.begin() as conn:
                    conn.execute(text(ddl))


def _migrate_workspace_tier(bind) -> None:
    """`workspaces.tier` (T05): every pre-existing row — `default` included — becomes
    `dev`, the tier that changes nothing about what members may do."""
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    if "workspaces" not in inspector.get_table_names():
        return
    if "tier" not in {c["name"] for c in inspector.get_columns("workspaces")}:
        with bind.begin() as conn:
            conn.execute(
                text("ALTER TABLE workspaces ADD COLUMN tier VARCHAR(16) NOT NULL DEFAULT 'dev'")
            )


def _migrate_system_skill_records_columns(bind) -> None:
    """SE-043 correction: the accepted-but-unverified id, the persisted create request
    and the update-intent high-water mark on a ledger created by the first candidate."""
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    if "system_skill_records" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("system_skill_records")}
    additions = {
        "pending_record_id": (
            "ALTER TABLE system_skill_records ADD COLUMN pending_record_id VARCHAR(64)"
        ),
        "create_request": "ALTER TABLE system_skill_records ADD COLUMN create_request JSON",
        "intent_version": (
            "ALTER TABLE system_skill_records ADD COLUMN intent_version VARCHAR(32)"
        ),
        "intent_digest": "ALTER TABLE system_skill_records ADD COLUMN intent_digest VARCHAR(64)",
        "intent_content_digest": (
            "ALTER TABLE system_skill_records ADD COLUMN intent_content_digest VARCHAR(64)"
        ),
    }
    for column, ddl in additions.items():
        if column not in existing:
            with bind.begin() as conn:
                conn.execute(text(ddl))


def _migrate_system_skill_records_index(bind) -> None:
    """The unique (workspace, preset) index that arbitrates concurrent system-skill
    registrations (SE-043). `create_all` builds it with the table on a fresh ledger;
    this keeps an upgraded ledger whose table predates the index honest."""
    from sqlalchemy import inspect, text

    if "system_skill_records" not in inspect(bind).get_table_names():
        return
    with bind.begin() as conn:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_system_skill_records_workspace_preset "
                "ON system_skill_records (workspace_id, preset_key)"
            )
        )


def _migrate_managed_memories_index(bind) -> None:
    """The unique (workspace, memory) index behind memory ownership (issue #55):
    an adopt racing a create of the same id leaves one row. `create_all` builds it
    with the table; this keeps a ledger whose table lost it honest."""
    from sqlalchemy import inspect, text

    if "managed_memories" not in inspect(bind).get_table_names():
        return
    with bind.begin() as conn:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_managed_memories_workspace_memory "
                "ON managed_memories (workspace_id, memory_id)"
            )
        )


def _migrate_system_key_index(bind) -> None:
    """The partial unique index that arbitrates concurrent preset installs.

    Runs after the column migrations because it spans `workspace_id` (added by
    `_migrate_workspace_columns`) and `system_key`. A table created by an older
    release has neither the column nor the index; `create_all` on a fresh ledger
    builds both from the model.
    """
    from sqlalchemy import inspect, text

    if "agents" not in inspect(bind).get_table_names():
        return
    with bind.begin() as conn:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_agents_workspace_system_key "
                "ON agents (workspace_id, system_key) "
                "WHERE system_key IS NOT NULL AND status != 'deleted'"
            )
        )


def _migrate_assistant_columns(bind) -> None:
    """Additive columns of the assistant tables (SE-039 correction): the immutable
    owner principal (NULL = visible to nobody, never adopted by username), the
    turn claim and the revision allocator, the proposal bindings, the
    admin-published share and the author of each user message."""
    from sqlalchemy import inspect, text

    additions = {
        "assistant_conversations": {
            "owner_principal": (
                "ALTER TABLE assistant_conversations ADD COLUMN owner_principal VARCHAR(96)"
            ),
            "active_turn": "ALTER TABLE assistant_conversations ADD COLUMN active_turn INTEGER",
            "active_turn_started_at": (
                "ALTER TABLE assistant_conversations ADD COLUMN active_turn_started_at DATETIME"
            ),
            "revision_seq": (
                "ALTER TABLE assistant_conversations ADD COLUMN revision_seq INTEGER DEFAULT 0"
            ),
            "active_turn_token": (
                "ALTER TABLE assistant_conversations ADD COLUMN active_turn_token VARCHAR(32)"
            ),
            "preparation": (
                "ALTER TABLE assistant_conversations ADD COLUMN preparation JSON DEFAULT '{}'"
            ),
            "preparation_sources": (
                "ALTER TABLE assistant_conversations ADD COLUMN preparation_sources "
                "JSON DEFAULT '[]'"
            ),
            "preparation_token": (
                "ALTER TABLE assistant_conversations ADD COLUMN preparation_token VARCHAR(32)"
            ),
            "shared": (
                "ALTER TABLE assistant_conversations ADD COLUMN shared BOOLEAN NOT NULL DEFAULT 0"
            ),
            "shared_by": "ALTER TABLE assistant_conversations ADD COLUMN shared_by VARCHAR(64)",
            "shared_at": "ALTER TABLE assistant_conversations ADD COLUMN shared_at DATETIME",
            "purge_job_id": (
                "ALTER TABLE assistant_conversations ADD COLUMN purge_job_id VARCHAR(32)"
            ),
        },
        "assistant_proposals": {
            "bindings": "ALTER TABLE assistant_proposals ADD COLUMN bindings JSON",
        },
        "assistant_messages": {
            "author": "ALTER TABLE assistant_messages ADD COLUMN author VARCHAR(64)",
        },
    }
    inspector = inspect(bind)
    live_tables = set(inspector.get_table_names())
    for table, columns in additions.items():
        if table not in live_tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for column, ddl in columns.items():
            if column not in existing:
                with bind.begin() as conn:
                    conn.execute(text(ddl))


def _migrate_workspace_columns(bind) -> None:
    """Add `workspace_id` (+ its index) to every per-environment table.

    The DDL is spelled out per table rather than generated, because
    `tests/test_ledger_migration.py` reads the (table, column) pairs a migration
    handles off the source of every `_migrate*` function.
    """
    from sqlalchemy import inspect, text

    additions = {
        "agents": "ALTER TABLE agents ADD COLUMN workspace_id VARCHAR(32)",
        "deployments": "ALTER TABLE deployments ADD COLUMN workspace_id VARCHAR(32)",
        "chat_sessions": "ALTER TABLE chat_sessions ADD COLUMN workspace_id VARCHAR(32)",
        "chat_messages": "ALTER TABLE chat_messages ADD COLUMN workspace_id VARCHAR(32)",
        "api_keys": "ALTER TABLE api_keys ADD COLUMN workspace_id VARCHAR(32)",
        "policy_decisions": "ALTER TABLE policy_decisions ADD COLUMN workspace_id VARCHAR(32)",
        "policy_changes": "ALTER TABLE policy_changes ADD COLUMN workspace_id VARCHAR(32)",
        "jobs": "ALTER TABLE jobs ADD COLUMN workspace_id VARCHAR(32)",
        "eval_datasets": "ALTER TABLE eval_datasets ADD COLUMN workspace_id VARCHAR(32)",
        "eval_runs": "ALTER TABLE eval_runs ADD COLUMN workspace_id VARCHAR(32)",
        "eval_recommendations": (
            "ALTER TABLE eval_recommendations ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "online_eval_configs": (
            "ALTER TABLE online_eval_configs ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "experiments": "ALTER TABLE experiments ADD COLUMN workspace_id VARCHAR(32)",
        "runtime_canaries": "ALTER TABLE runtime_canaries ADD COLUMN workspace_id VARCHAR(32)",
        "skill_lab_tasksets": "ALTER TABLE skill_lab_tasksets ADD COLUMN workspace_id VARCHAR(32)",
        "skill_lab_jobs": "ALTER TABLE skill_lab_jobs ADD COLUMN workspace_id VARCHAR(32)",
        "assistant_conversations": (
            "ALTER TABLE assistant_conversations ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "assistant_messages": (
            "ALTER TABLE assistant_messages ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "assistant_proposals": (
            "ALTER TABLE assistant_proposals ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "agent_name_claims": "ALTER TABLE agent_name_claims ADD COLUMN workspace_id VARCHAR(32)",
        "system_skill_records": (
            "ALTER TABLE system_skill_records ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "managed_memories": "ALTER TABLE managed_memories ADD COLUMN workspace_id VARCHAR(32)",
        "assistant_evaluation_plans": (
            "ALTER TABLE assistant_evaluation_plans ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "evaluation_asset_operations": (
            "ALTER TABLE evaluation_asset_operations ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "eval_pipelines": "ALTER TABLE eval_pipelines ADD COLUMN workspace_id VARCHAR(32)",
        # born with the column (T16/T18); listed so the scoped-tables drift test holds
        "api_key_usage": "ALTER TABLE api_key_usage ADD COLUMN workspace_id VARCHAR(32)",
        "spec_snapshots": "ALTER TABLE spec_snapshots ADD COLUMN workspace_id VARCHAR(32)",
        # born with the column (T05); listed so the scoped-tables drift test holds
        "audit_events": "ALTER TABLE audit_events ADD COLUMN workspace_id VARCHAR(32)",
        # born with the column (T13/T15); listed so the scoped-tables drift test holds
        "share_links": "ALTER TABLE share_links ADD COLUMN workspace_id VARCHAR(32)",
        "chat_feedback": "ALTER TABLE chat_feedback ADD COLUMN workspace_id VARCHAR(32)",
        # born with the column (T20/T21); listed so the scoped-tables drift test holds
        "release_bundles": "ALTER TABLE release_bundles ADD COLUMN workspace_id VARCHAR(32)",
        "promotions": "ALTER TABLE promotions ADD COLUMN workspace_id VARCHAR(32)",
        # born with the column (T29); listed so the scoped-tables drift test holds
        "alert_rules": "ALTER TABLE alert_rules ADD COLUMN workspace_id VARCHAR(32)",
        # born with the column (T23); listed so the scoped-tables drift test holds
        "resource_mappings": "ALTER TABLE resource_mappings ADD COLUMN workspace_id VARCHAR(32)",
        # born with the column (T35/T36); listed so the scoped-tables drift test holds
        "answer_rules": "ALTER TABLE answer_rules ADD COLUMN workspace_id VARCHAR(32)",
        "answer_rule_sets": "ALTER TABLE answer_rule_sets ADD COLUMN workspace_id VARCHAR(32)",
        "issues": "ALTER TABLE issues ADD COLUMN workspace_id VARCHAR(32)",
        "identity_providers": (
            "ALTER TABLE identity_providers ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "user_token_revocations": (
            "ALTER TABLE user_token_revocations ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "oauth_pending_sessions": (
            "ALTER TABLE oauth_pending_sessions ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "user_grants": "ALTER TABLE user_grants ADD COLUMN workspace_id VARCHAR(32)",
        # born with the column (Agent-DLC); listed so the scoped-tables drift test holds
        "criteria_sets": "ALTER TABLE criteria_sets ADD COLUMN workspace_id VARCHAR(32)",
        "criteria": "ALTER TABLE criteria ADD COLUMN workspace_id VARCHAR(32)",
        "criterion_results": (
            "ALTER TABLE criterion_results ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "annotation_tasks": "ALTER TABLE annotation_tasks ADD COLUMN workspace_id VARCHAR(32)",
        "annotations": "ALTER TABLE annotations ADD COLUMN workspace_id VARCHAR(32)",
        "calibration_records": (
            "ALTER TABLE calibration_records ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "release_records": "ALTER TABLE release_records ADD COLUMN workspace_id VARCHAR(32)",
        "waivers": "ALTER TABLE waivers ADD COLUMN workspace_id VARCHAR(32)",
        "admission_candidates": (
            "ALTER TABLE admission_candidates ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "watch_configs": "ALTER TABLE watch_configs ADD COLUMN workspace_id VARCHAR(32)",
        "resource_creators": (
            "ALTER TABLE resource_creators ADD COLUMN workspace_id VARCHAR(32)"
        ),
    }
    inspector = inspect(bind)
    live_tables = set(inspector.get_table_names())
    for table, ddl in additions.items():
        if table not in live_tables:
            continue
        if "workspace_id" not in {c["name"] for c in inspector.get_columns(table)}:
            with bind.begin() as conn:
                conn.execute(text(ddl))
        # `create_all` only builds indexes for tables it creates, so an upgraded
        # table needs its index here; IF NOT EXISTS keeps the fresh-database case
        # (where the model's index=True already applied) a no-op. Note that
        # `schema_drift` compares column names only and cannot catch a missing
        # index — `tests/test_workspaces.py` does.
        with bind.begin() as conn:
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_{table}_workspace_id "
                    f"ON {table} (workspace_id)"
                )
            )


def unscoped_row_counts(bind) -> dict[str, int]:
    """Rows in per-environment tables that name no workspace, per table."""
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    live_tables = set(inspector.get_table_names())
    counts: dict[str, int] = {}
    for table in WORKSPACE_SCOPED_TABLES:
        if table not in live_tables:
            continue
        with bind.begin() as conn:
            found = conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE workspace_id IS NULL")  # noqa: S608
            ).scalar_one()
        if found:
            counts[table] = int(found)
    return counts


def assert_every_row_has_a_workspace(bind) -> None:
    """Fail startup on any row that belongs to no environment.

    The upgrade adopts pre-workspace rows once (see `_seed_default_workspace`);
    after that a NULL means a write path forgot to stamp `workspace_id`, and such a
    row is invisible to every scoped query — it would silently disappear from the
    console instead of erroring. Repeating the adopting UPDATE on every startup
    would paper over exactly that bug, so this refuses to boot instead.
    """
    counts = unscoped_row_counts(bind)
    if not counts:
        return
    listed = ", ".join(f"{table}={count}" for table, count in sorted(counts.items()))
    raise RuntimeError(
        "ledger rows with no workspace_id: "
        f"{listed}. A write path is not stamping the workspace — find the insert "
        "for that table and give it the request's (or the parent row's) workspace."
    )


def _seed_default_workspace(bind) -> None:
    """Mirror settings onto the `default` workspace and adopt pre-P2 rows + users.

    The row is refreshed on every startup rather than seeded once: `make
    bootstrap` and kb_gateway rewrite `launchpad.yaml` *after* the first seed, so
    a frozen snapshot would keep serving a stale identity — a fresh clone would
    pin `account_id=""` forever and the real account would later arrive as a
    *second* workspace beside the bogus default. Settings therefore stays
    authoritative for `default` (and only `default`) until the console owns the
    row; non-default workspaces are row-authoritative from birth.

    Raw SQL on purpose, for two reasons: this module cannot import the models
    (they import `Base` from here), and `PolicyChange`'s `before_update` listener
    rejects ORM updates of frozen audit rows. Idempotent — the mirror converges on
    settings, and the row adoption runs only on the insert (see below).
    """
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    live_tables = set(inspector.get_table_names())
    if "workspaces" not in live_tables:
        # `create_all` built nothing, which means `Base.metadata` was empty —
        # i.e. the model modules were never imported (the trap that makes an
        # out-of-process migration rehearsal silently skip the whole upgrade).
        raise RuntimeError(
            "`workspaces` is missing after create_all — import app.models.ledger "
            "(as app.main does) before calling init_db"
        )

    settings = get_settings()
    resources = settings.resources or {}
    # An empty resource map means bootstrap has not run (or its output was lost),
    # so the environment is registered but not usable — never claim "ready" for it.
    values = {
        "id": DEFAULT_WORKSPACE_ID,
        "name": "Default",
        "account_id": settings.account_id,
        "region": settings.region,
        "resources": json.dumps(resources),
        "bootstrap_status": "ready" if resources else "registered",
        # SQLAlchemy's SQLite DATETIME format, written directly because there is
        # no type coercion on a raw statement.
        "now": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f"),
    }
    with bind.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM workspaces WHERE id = :id"), {"id": DEFAULT_WORKSPACE_ID}
        ).first()
        if exists is None:
            conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, account_id, region, role_arn,"
                    " external_id, bootstrap_status, resources, created_at, updated_at)"
                    " VALUES (:id, :name, :account_id, :region, NULL, NULL,"
                    " :bootstrap_status, :resources, :now, :now)"
                ),
                values,
            )
            # Accounts that predate workspaces already reach this environment, so
            # the upgrade grants it to them rather than locking the console.
            # Deliberately only on the insert (the migration moment): repeating it
            # every startup would make a revoked grant come back.
            conn.execute(
                text(
                    "INSERT INTO user_workspaces (user_id, workspace_id, created_at)"
                    " SELECT id, :id, :now FROM users"
                ),
                values,
            )
            # Adopt every pre-workspace row, once. Only on the insert branch: this
            # is the migration moment, and repeating it on every startup would
            # silently absorb rows a *new* write path failed to stamp — the exact
            # bug `assert_every_row_has_a_workspace` exists to surface.
            for table in WORKSPACE_SCOPED_TABLES:
                if table not in live_tables:
                    continue
                conn.execute(
                    text(
                        f"UPDATE {table} SET workspace_id = :id "  # noqa: S608
                        "WHERE workspace_id IS NULL"
                    ),
                    {"id": DEFAULT_WORKSPACE_ID},
                )
        else:
            # name/role_arn/external_id/tier are operator-owned, so the mirror
            # leaves them alone (an admin's `prod` must survive a restart). If a
            # second workspace already claimed this (account, region) while
            # default held a bogus identity, UNIQUE(account_id,
            # region) raises here and startup fails loudly — that conflict needs
            # an operator decision, not a silent winner.
            conn.execute(
                text(
                    "UPDATE workspaces SET account_id = :account_id, region = :region,"
                    " resources = :resources, bootstrap_status = :bootstrap_status,"
                    " updated_at = :now WHERE id = :id"
                ),
                values,
            )


# Tables whose (workspace_id, project_id) pair is the hot list filter (design §8.2).
_PROJECT_COMPOSITE_INDEXED = (
    "agents", "chat_sessions", "chat_messages", "chat_feedback", "eval_runs", "eval_datasets",
    "jobs", "issues", "audit_events", "api_keys", "share_links", "release_records",
    "admission_candidates",
)


def _migrate_project_columns(bind) -> None:
    """Add `project_id` (+ indexes) to every project-owned and project-shared table.

    Spelled out per table, like `_migrate_workspace_columns`, because
    `tests/test_ledger_migration.py` reads the (table, column) pairs off this source.
    """
    from sqlalchemy import inspect, text

    additions = {
        "api_keys": "ALTER TABLE api_keys ADD COLUMN project_id VARCHAR(32)",
        "resource_creators": "ALTER TABLE resource_creators ADD COLUMN project_id VARCHAR(32)",
        "eval_datasets": "ALTER TABLE eval_datasets ADD COLUMN project_id VARCHAR(32)",
        "eval_pipelines": "ALTER TABLE eval_pipelines ADD COLUMN project_id VARCHAR(32)",
        "skill_lab_tasksets": "ALTER TABLE skill_lab_tasksets ADD COLUMN project_id VARCHAR(32)",
        "assistant_conversations": (
            "ALTER TABLE assistant_conversations ADD COLUMN project_id VARCHAR(32)"
        ),
        "criteria_sets": "ALTER TABLE criteria_sets ADD COLUMN project_id VARCHAR(32)",
        "deployments": "ALTER TABLE deployments ADD COLUMN project_id VARCHAR(32)",
        "spec_snapshots": "ALTER TABLE spec_snapshots ADD COLUMN project_id VARCHAR(32)",
        "chat_sessions": "ALTER TABLE chat_sessions ADD COLUMN project_id VARCHAR(32)",
        "chat_messages": "ALTER TABLE chat_messages ADD COLUMN project_id VARCHAR(32)",
        "chat_feedback": "ALTER TABLE chat_feedback ADD COLUMN project_id VARCHAR(32)",
        "issues": "ALTER TABLE issues ADD COLUMN project_id VARCHAR(32)",
        "answer_rules": "ALTER TABLE answer_rules ADD COLUMN project_id VARCHAR(32)",
        "answer_rule_sets": "ALTER TABLE answer_rule_sets ADD COLUMN project_id VARCHAR(32)",
        "oauth_pending_sessions": (
            "ALTER TABLE oauth_pending_sessions ADD COLUMN project_id VARCHAR(32)"
        ),
        "user_grants": "ALTER TABLE user_grants ADD COLUMN project_id VARCHAR(32)",
        "release_bundles": "ALTER TABLE release_bundles ADD COLUMN project_id VARCHAR(32)",
        "eval_runs": "ALTER TABLE eval_runs ADD COLUMN project_id VARCHAR(32)",
        "online_eval_configs": "ALTER TABLE online_eval_configs ADD COLUMN project_id VARCHAR(32)",
        "experiments": "ALTER TABLE experiments ADD COLUMN project_id VARCHAR(32)",
        "annotation_tasks": "ALTER TABLE annotation_tasks ADD COLUMN project_id VARCHAR(32)",
        "calibration_records": "ALTER TABLE calibration_records ADD COLUMN project_id VARCHAR(32)",
        "release_records": "ALTER TABLE release_records ADD COLUMN project_id VARCHAR(32)",
        "waivers": "ALTER TABLE waivers ADD COLUMN project_id VARCHAR(32)",
        "admission_candidates": (
            "ALTER TABLE admission_candidates ADD COLUMN project_id VARCHAR(32)"
        ),
        "watch_configs": "ALTER TABLE watch_configs ADD COLUMN project_id VARCHAR(32)",
        "runtime_canaries": "ALTER TABLE runtime_canaries ADD COLUMN project_id VARCHAR(32)",
        "api_key_usage": "ALTER TABLE api_key_usage ADD COLUMN project_id VARCHAR(32)",
        "share_links": "ALTER TABLE share_links ADD COLUMN project_id VARCHAR(32)",
        "promotions": "ALTER TABLE promotions ADD COLUMN project_id VARCHAR(32)",
        "eval_recommendations": (
            "ALTER TABLE eval_recommendations ADD COLUMN project_id VARCHAR(32)"
        ),
        "assistant_messages": "ALTER TABLE assistant_messages ADD COLUMN project_id VARCHAR(32)",
        "assistant_proposals": "ALTER TABLE assistant_proposals ADD COLUMN project_id VARCHAR(32)",
        "assistant_evaluation_plans": (
            "ALTER TABLE assistant_evaluation_plans ADD COLUMN project_id VARCHAR(32)"
        ),
        "evaluation_asset_operations": (
            "ALTER TABLE evaluation_asset_operations ADD COLUMN project_id VARCHAR(32)"
        ),
        "criteria": "ALTER TABLE criteria ADD COLUMN project_id VARCHAR(32)",
        "criterion_results": "ALTER TABLE criterion_results ADD COLUMN project_id VARCHAR(32)",
        "annotations": "ALTER TABLE annotations ADD COLUMN project_id VARCHAR(32)",
        "skill_lab_jobs": "ALTER TABLE skill_lab_jobs ADD COLUMN project_id VARCHAR(32)",
        "agents": "ALTER TABLE agents ADD COLUMN project_id VARCHAR(32)",
        "jobs": "ALTER TABLE jobs ADD COLUMN project_id VARCHAR(32)",
        "managed_memories": "ALTER TABLE managed_memories ADD COLUMN project_id VARCHAR(32)",
        "policy_decisions": "ALTER TABLE policy_decisions ADD COLUMN project_id VARCHAR(32)",
        "policy_changes": "ALTER TABLE policy_changes ADD COLUMN project_id VARCHAR(32)",
        "audit_events": "ALTER TABLE audit_events ADD COLUMN project_id VARCHAR(32)",
        "alert_rules": "ALTER TABLE alert_rules ADD COLUMN project_id VARCHAR(32)",
        "resource_mappings": "ALTER TABLE resource_mappings ADD COLUMN project_id VARCHAR(32)",
        "identity_providers": "ALTER TABLE identity_providers ADD COLUMN project_id VARCHAR(32)",
        "user_token_revocations": (
            "ALTER TABLE user_token_revocations ADD COLUMN project_id VARCHAR(32)"
        ),
    }
    inspector = inspect(bind)
    live_tables = set(inspector.get_table_names())
    # one transaction for the whole pass: on a file ledger every separate commit is
    # its own fsync, and this runs at every startup (and for every test app)
    with bind.begin() as conn:
        for table, statement in additions.items():
            if table not in live_tables:
                continue
            if "project_id" not in {c["name"] for c in inspector.get_columns(table)}:
                conn.execute(text(statement))
            conn.execute(
                text(f"CREATE INDEX IF NOT EXISTS ix_{table}_project_id ON {table} (project_id)")
            )
            if table in _PROJECT_COMPOSITE_INDEXED:
                conn.execute(
                    text(
                        f"CREATE INDEX IF NOT EXISTS ix_{table}_ws_project "
                        f"ON {table} (workspace_id, project_id)"
                    )
                )


def _now_sql() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def bind_default_project_everywhere(conn) -> None:
    """Every workspace with no project yet gets `default`, as its workspace default.

    Idempotent and run on every startup, so a workspace registered by an older
    build (or restored from a backup) is never left with no project to resolve."""
    from sqlalchemy import text

    conn.execute(
        text(
            "INSERT INTO project_workspaces (project_id, workspace_id, is_workspace_default,"
            " enabled_by, enabled_at)"
            " SELECT 'default', w.id, 1, NULL, :now FROM workspaces w"
            " WHERE NOT EXISTS (SELECT 1 FROM project_workspaces pw WHERE pw.workspace_id = w.id)"
        ),
        {"now": _now_sql()},
    )


_WORKSPACE_DEFAULT_PROJECT = (
    "(SELECT pw.project_id FROM project_workspaces pw"
    " WHERE pw.workspace_id = {table}.workspace_id AND pw.is_workspace_default = 1)"
)


def _seed_default_projects(bind) -> None:
    """Seed the `default` project and, once, file every existing row into it (design §8.3).

    The one-time part (backfill + `inherit` memberships) runs while the marker
    `migration.projects_v1` is absent: repeating it would re-add a membership an
    administrator removed and silently absorb rows a new write path forgot to stamp.
    Raw SQL for the same reasons as `_seed_default_workspace`.
    """
    from sqlalchemy import inspect, text

    live_tables = set(inspect(bind).get_table_names())
    now = _now_sql()
    with bind.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO projects (id, name, description, status, is_default, settings,"
                " created_by, created_by_name, created_at, updated_at)"
                " SELECT 'default', 'Default', '', 'active', 1, NULL, NULL, NULL, :now, :now"
                " WHERE NOT EXISTS (SELECT 1 FROM projects WHERE id = 'default')"
            ),
            {"now": now},
        )
        bind_default_project_everywhere(conn)
        conn.execute(
            text(
                "INSERT INTO access_epoch (id, value) SELECT 1, 0"
                " WHERE NOT EXISTS (SELECT 1 FROM access_epoch WHERE id = 1)"
            )
        )
        done = conn.execute(
            text("SELECT 1 FROM platform_settings WHERE key = :k"),
            {"k": PROJECTS_MIGRATION_MARKER},
        ).first()
        if done is not None:
            return
        for table in _PROJECT_BACKFILL_DIRECT:
            if table not in live_tables:
                continue
            platform = " AND system_key IS NULL" if table == "agents" else ""
            conn.execute(
                text(
                    f"UPDATE {table} SET project_id = "  # noqa: S608 — fixed names
                    + _WORKSPACE_DEFAULT_PROJECT.format(table=table)
                    + f" WHERE project_id IS NULL{platform}"
                )
            )
        for table, column, parent in _PROJECT_BACKFILL_DERIVED:
            if table not in live_tables or parent not in live_tables:
                continue
            conn.execute(
                text(
                    f"UPDATE {table} SET project_id = (SELECT p.project_id FROM {parent} p"  # noqa: S608
                    f" WHERE p.id = {table}.{column}) WHERE project_id IS NULL"
                )
            )
        for table in PROJECT_STRICT_TABLES:
            if table not in live_tables:
                continue
            # orphans, and children of a platform (system preset) agent
            conn.execute(
                text(
                    f"UPDATE {table} SET project_id = "  # noqa: S608
                    + _WORKSPACE_DEFAULT_PROJECT.format(table=table)
                    + " WHERE project_id IS NULL"
                )
            )
        # every non-admin account already granted a workspace keeps reaching its
        # data: an `inherit` member of that workspace's default project (design §3.5)
        conn.execute(
            text(
                "INSERT INTO project_members (project_id, user_id, role_key, overrides,"
                " added_by, added_at)"
                " SELECT DISTINCT pw.project_id, uw.user_id, 'inherit', NULL, NULL, :now"
                " FROM user_workspaces uw"
                " JOIN users u ON u.id = uw.user_id AND u.role <> 'admin'"
                " JOIN project_workspaces pw ON pw.workspace_id = uw.workspace_id"
                "  AND pw.is_workspace_default = 1"
                " WHERE NOT EXISTS (SELECT 1 FROM project_members pm"
                "  WHERE pm.project_id = pw.project_id AND pm.user_id = uw.user_id)"
            ),
            {"now": now},
        )
        conn.execute(text("UPDATE access_epoch SET value = value + 1 WHERE id = 1"))
        conn.execute(
            text(
                "INSERT INTO platform_settings (key, value, updated_by, updated_at)"
                " VALUES (:k, 'done', 'migration', :now)"
            ),
            {"k": PROJECTS_MIGRATION_MARKER, "now": now},
        )


def unprojected_row_counts(bind) -> dict[str, int]:
    """Strict rows that name no project (agents: non-preset ones), per table."""
    from sqlalchemy import inspect, text

    live_tables = set(inspect(bind).get_table_names())
    counts: dict[str, int] = {}
    with bind.connect() as conn:
        for table in (*PROJECT_STRICT_TABLES, "agents"):
            if table not in live_tables:
                continue
            extra = " AND system_key IS NULL" if table == "agents" else ""
            found = conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE project_id IS NULL{extra}")  # noqa: S608
            ).scalar_one()
            if found:
                counts[table] = int(found)
    return counts


def claim_sole_project_rows(bind) -> dict[str, int]:
    """Give each unprojected strict row its workspace's only project; claimed rows per table.

    Unambiguous by construction: a workspace bound to exactly one project, or to
    none (then `default`, the same rule as project_scope._sole_workspace_project).
    Rows in a workspace holding several projects are left NULL. Shared by startup
    (`assert_every_row_has_a_project`) and scripts/backfill_projects.py.
    """
    from sqlalchemy import inspect, text

    counts = unprojected_row_counts(bind)
    if not counts:
        return {}
    live_tables = set(inspect(bind).get_table_names())
    sole = (
        "COALESCE((SELECT MIN(pw.project_id) FROM project_workspaces pw"
        " WHERE pw.workspace_id = {table}.workspace_id HAVING COUNT(*) = 1),"
        " (SELECT p.id FROM projects p WHERE p.id = 'default' AND NOT EXISTS"
        "  (SELECT 1 FROM project_workspaces pw2"
        "   WHERE pw2.workspace_id = {table}.workspace_id)))"
    )
    claimed_per_table: dict[str, int] = {}
    with bind.begin() as conn:
        for table in counts:
            if table not in live_tables:
                continue
            extra = " AND system_key IS NULL" if table == "agents" else ""
            owner = sole.format(table=table)
            # `owner IS NOT NULL` keeps the rowcount honest: an ambiguous row would
            # otherwise be "updated" to NULL and counted as adopted
            claimed = conn.execute(
                text(
                    f"UPDATE {table} SET project_id = {owner}"  # noqa: S608
                    f" WHERE project_id IS NULL{extra} AND {owner} IS NOT NULL"
                )
            ).rowcount
            if claimed:
                claimed_per_table[table] = int(claimed)
    return claimed_per_table


def derive_projects_from_parents(bind) -> dict[str, int]:
    """Unprojected derived rows take their parent's project, where it has one —
    unambiguous however many projects the workspace holds."""
    from sqlalchemy import inspect, text

    live = set(inspect(bind).get_table_names())
    stamped: dict[str, int] = {}
    with bind.begin() as conn:
        for table, column, parent in _PROJECT_BACKFILL_DERIVED:
            if table not in live or parent not in live:
                continue
            parent_project = (
                f"(SELECT p.project_id FROM {parent} p WHERE p.id = {table}.{column})"
            )
            changed = conn.execute(
                text(
                    f"UPDATE {table} SET project_id = {parent_project}"  # noqa: S608
                    f" WHERE project_id IS NULL AND {parent_project} IS NOT NULL"
                )
            ).rowcount
            if changed:
                stamped[table] = stamped.get(table, 0) + int(changed)
    return stamped


def assert_every_row_has_a_project(bind) -> None:
    """Fail startup on a strict row with no project — unless its workspace is bound
    to a single project, which then adopts it unambiguously (a WARNING): the case of
    rows an older build wrote after a rollback (design §8.3/§8.4)."""
    import logging

    log = logging.getLogger(__name__)
    for table, derived in derive_projects_from_parents(bind).items():
        log.warning("filed %d %s rows with no project under their parent's project",
                    derived, table)
    for table, claimed in claim_sole_project_rows(bind).items():
        log.warning("adopted %d %s rows with no project into their workspace's "
                    "only project", claimed, table)
    left = unprojected_row_counts(bind)
    if not left:
        return
    listed = ", ".join(f"{table}={count}" for table, count in sorted(left.items()))
    raise RuntimeError(
        f"ledger rows with no project_id: {listed}. Their workspaces hold several "
        "projects, so the owner is ambiguous — run scripts/backfill_projects.py "
        "--workspace <id> --project <id>, or find the write path that did not stamp it."
    )
