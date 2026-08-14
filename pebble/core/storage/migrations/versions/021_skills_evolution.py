"""Add skills evolution columns, skill_resources table, security scan fields,
session config columns (from WS templates), skill_versions table, and migrate
workstream_templates data into prompt_templates.

Extends prompt_templates with description, tags, source_url, version,
author, activation mode, token_estimate, allowed_tools, security scan
fields (scan_status, scan_report, installed_at, installed_by), and
session config fields (model, auto_approve, temperature, reasoning_effort,
max_tokens, token_budget, agent_max_turns, notify_on_complete, enabled).
Creates skill_resources and skill_versions tables.
Migrates data from workstream_templates and workstream_template_versions
into the unified skills model, then drops the old tables.
Removes ws_template column from scheduled_tasks.

Revision ID: 021
Revises: 020
Create Date: 2026-03-16
"""

import sqlalchemy as sa
from alembic import op

revision = "021"
down_revision = "020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Phase 1+2: Skills evolution columns + security scanning (batch for SQLite)
    with op.batch_alter_table("prompt_templates") as batch_op:
        batch_op.add_column(sa.Column("description", sa.Text, nullable=False, server_default=""))
        batch_op.add_column(sa.Column("tags", sa.Text, nullable=False, server_default="[]"))
        batch_op.add_column(sa.Column("source_url", sa.Text, nullable=False, server_default=""))
        batch_op.add_column(sa.Column("version", sa.Text, nullable=False, server_default="1.0.0"))
        batch_op.add_column(sa.Column("author", sa.Text, nullable=False, server_default=""))
        batch_op.add_column(
            sa.Column("activation", sa.Text, nullable=False, server_default="named"),
        )
        batch_op.add_column(
            sa.Column("token_estimate", sa.Integer, nullable=False, server_default="0"),
        )
        batch_op.add_column(
            sa.Column("allowed_tools", sa.Text, nullable=False, server_default="[]"),
        )
        batch_op.add_column(sa.Column("scan_status", sa.Text, nullable=False, server_default=""))
        batch_op.add_column(
            sa.Column("scan_report", sa.Text, nullable=False, server_default="{}"),
        )
        batch_op.add_column(
            sa.Column("installed_at", sa.Text, nullable=False, server_default=""),
        )
        batch_op.add_column(
            sa.Column("installed_by", sa.Text, nullable=False, server_default=""),
        )

    # Backfill activation from is_default
    op.execute("UPDATE prompt_templates SET activation = 'default' WHERE is_default = 1")
    op.execute("UPDATE prompt_templates SET activation = 'named' WHERE is_default = 0")

    # Skill resources — bundled files (scripts/, references/, assets/)
    op.create_table(
        "skill_resources",
        sa.Column("resource_id", sa.Text, primary_key=True),
        sa.Column("skill_id", sa.Text, nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("content_type", sa.Text, nullable=False, server_default="text/plain"),
        sa.Column("created", sa.Text, nullable=False),
    )
    op.create_index("idx_skill_resources_skill_id", "skill_resources", ["skill_id"])
    op.create_index(
        "idx_skill_resources_skill_path",
        "skill_resources",
        ["skill_id", "path"],
        unique=True,
    )

    # Phase 3: Session config columns (from workstream templates)
    with op.batch_alter_table("prompt_templates") as batch_op:
        batch_op.add_column(sa.Column("model", sa.Text, nullable=False, server_default=""))
        batch_op.add_column(
            sa.Column("auto_approve", sa.Integer, nullable=False, server_default="0"),
        )
        batch_op.add_column(sa.Column("temperature", sa.Float, nullable=True))
        batch_op.add_column(
            sa.Column("reasoning_effort", sa.Text, nullable=False, server_default=""),
        )
        batch_op.add_column(sa.Column("max_tokens", sa.Integer, nullable=True))
        batch_op.add_column(
            sa.Column("token_budget", sa.Integer, nullable=False, server_default="0"),
        )
        batch_op.add_column(sa.Column("agent_max_turns", sa.Integer, nullable=True))
        batch_op.add_column(
            sa.Column("notify_on_complete", sa.Text, nullable=False, server_default="{}"),
        )
        batch_op.add_column(
            sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
        )

    # Skill versions — version history for skills
    op.create_table(
        "skill_versions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("skill_id", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("snapshot", sa.Text, nullable=False),
        sa.Column("changed_by", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
    )
    op.create_index("idx_skill_versions_skill_id", "skill_versions", ["skill_id"])

    # Phase 4: Data migration — workstream_templates → prompt_templates
    op.execute("""
        INSERT INTO prompt_templates (
            template_id, name, category, content, variables, is_default,
            org_id, created_by, origin, mcp_server, readonly,
            description, tags, source_url, version, author,
            activation, token_estimate, allowed_tools,
            scan_status, scan_report, installed_at, installed_by,
            model, auto_approve, temperature, reasoning_effort,
            max_tokens, token_budget, agent_max_turns,
            notify_on_complete, enabled, created, updated
        )
        SELECT
            ws_template_id,
            CASE
                WHEN EXISTS (SELECT 1 FROM prompt_templates pt WHERE pt.name = wt.name)
                THEN 'skills-' || wt.name
                ELSE wt.name
            END,
            'profile',
            COALESCE(
                CASE WHEN wt.system_prompt != '' THEN wt.system_prompt ELSE NULL END,
                (SELECT pt2.content FROM prompt_templates pt2
                 WHERE pt2.name = wt.prompt_template),
                ''
            ),
            '[]', 0,
            wt.org_id, wt.created_by, 'manual', '', 0,
            wt.description, '[]', '', '1.0.0', '',
            'named', 0, COALESCE(wt.auto_approve_tools, ''),
            '', '{}', '', '',
            COALESCE(wt.model, ''),
            wt.auto_approve,
            wt.temperature,
            COALESCE(wt.reasoning_effort, ''),
            wt.max_tokens,
            wt.token_budget,
            wt.agent_max_turns,
            COALESCE(wt.notify_on_complete, '{}'),
            wt.enabled,
            wt.created,
            wt.updated
        FROM workstream_templates wt
    """)

    # Migrate version history
    op.execute("""
        INSERT INTO skill_versions (skill_id, version, snapshot, changed_by, created)
        SELECT ws_template_id, version, snapshot, changed_by, created
        FROM workstream_template_versions
    """)

    # Update scheduled_tasks: migrate ws_template references to skill (template column)
    op.execute("""
        UPDATE scheduled_tasks SET template = (
            SELECT CASE
                WHEN EXISTS (
                    SELECT 1 FROM prompt_templates pt
                    WHERE pt.name = scheduled_tasks.ws_template
                    AND pt.template_id NOT IN (
                        SELECT ws_template_id FROM workstream_templates
                    )
                )
                THEN 'skills-' || scheduled_tasks.ws_template
                ELSE scheduled_tasks.ws_template
            END
        )
        WHERE ws_template != '' AND template = ''
    """)

    # Clean orphaned admin.ws_templates permission from existing roles
    op.execute(
        "UPDATE roles SET permissions = REPLACE(permissions, ',admin.ws_templates', '') "
        "WHERE permissions LIKE '%,admin.ws_templates%'"
    )
    op.execute(
        "UPDATE roles SET permissions = REPLACE(permissions, 'admin.ws_templates,', '') "
        "WHERE permissions LIKE '%admin.ws_templates,%'"
    )
    op.execute(
        "UPDATE roles SET permissions = REPLACE(permissions, 'admin.ws_templates', '') "
        "WHERE permissions = 'admin.ws_templates'"
    )

    # Rename admin.templates permission → admin.skills
    op.execute(
        "UPDATE roles SET permissions = REPLACE(permissions, 'admin.templates', 'admin.skills') "
        "WHERE permissions LIKE '%admin.templates%'"
    )

    # Migrate workstream_config keys: ws_template_* → applied_skill_*
    op.execute("UPDATE workstream_config SET key = 'applied_skill_id' WHERE key = 'ws_template_id'")
    op.execute(
        "UPDATE workstream_config SET key = 'applied_skill_version' "
        "WHERE key = 'ws_template_version'"
    )
    op.execute(
        "UPDATE workstream_config SET key = 'applied_skill_content' "
        "WHERE key = 'ws_template_system_prompt'"
    )

    # Rename workstreams table columns: ws_template_id → skill_id,
    # ws_template_version → skill_version
    with op.batch_alter_table("workstreams") as batch_op:
        batch_op.alter_column("ws_template_id", new_column_name="skill_id")
        batch_op.alter_column("ws_template_version", new_column_name="skill_version")

    # Rename scheduled_tasks.template → skill, drop ws_template
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.alter_column("template", new_column_name="skill")
        batch_op.drop_column("ws_template")

    # Drop old tables
    op.drop_table("workstream_template_versions")
    op.drop_table("workstream_templates")


def downgrade() -> None:
    # Reverse workstreams column renames
    with op.batch_alter_table("workstreams") as batch_op:
        batch_op.alter_column("skill_id", new_column_name="ws_template_id")
        batch_op.alter_column("skill_version", new_column_name="ws_template_version")

    # Reverse workstream_config key renames
    op.execute("UPDATE workstream_config SET key = 'ws_template_id' WHERE key = 'applied_skill_id'")
    op.execute(
        "UPDATE workstream_config SET key = 'ws_template_version' "
        "WHERE key = 'applied_skill_version'"
    )
    op.execute(
        "UPDATE workstream_config SET key = 'ws_template_system_prompt' "
        "WHERE key = 'applied_skill_content'"
    )

    # Reverse scheduled_tasks column rename + re-add ws_template
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.alter_column("skill", new_column_name="template")
        batch_op.add_column(sa.Column("ws_template", sa.Text, nullable=False, server_default=""))

    # Recreate workstream_templates (empty — destructive migration)
    op.create_table(
        "workstream_templates",
        sa.Column("ws_template_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("system_prompt", sa.Text, nullable=False, server_default=""),
        sa.Column("prompt_template", sa.Text, nullable=False, server_default=""),
        sa.Column("prompt_template_hash", sa.Text, nullable=False, server_default=""),
        sa.Column("model", sa.Text, nullable=False, server_default=""),
        sa.Column("auto_approve", sa.Integer, nullable=False, server_default="0"),
        sa.Column("auto_approve_tools", sa.Text, nullable=False, server_default=""),
        sa.Column("temperature", sa.Float),
        sa.Column("reasoning_effort", sa.Text, nullable=False, server_default=""),
        sa.Column("max_tokens", sa.Integer),
        sa.Column("token_budget", sa.Integer, nullable=False, server_default="0"),
        sa.Column("agent_max_turns", sa.Integer),
        sa.Column("notify_on_complete", sa.Text, nullable=False, server_default="{}"),
        sa.Column("org_id", sa.Text, nullable=False, server_default=""),
        sa.Column("created_by", sa.Text, nullable=False, server_default=""),
        sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )

    # Recreate workstream_template_versions (empty)
    op.create_table(
        "workstream_template_versions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ws_template_id", sa.Text, nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("snapshot", sa.Text, nullable=False),
        sa.Column("changed_by", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
    )

    # Drop skill_versions
    op.drop_index("idx_skill_versions_skill_id", table_name="skill_versions")
    op.drop_table("skill_versions")

    # Drop session config + skills evolution columns from prompt_templates
    with op.batch_alter_table("prompt_templates") as batch_op:
        batch_op.drop_column("enabled")
        batch_op.drop_column("notify_on_complete")
        batch_op.drop_column("agent_max_turns")
        batch_op.drop_column("token_budget")
        batch_op.drop_column("max_tokens")
        batch_op.drop_column("reasoning_effort")
        batch_op.drop_column("temperature")
        batch_op.drop_column("auto_approve")
        batch_op.drop_column("model")
        batch_op.drop_column("installed_by")
        batch_op.drop_column("installed_at")
        batch_op.drop_column("scan_report")
        batch_op.drop_column("scan_status")
        batch_op.drop_column("allowed_tools")
        batch_op.drop_column("token_estimate")
        batch_op.drop_column("activation")
        batch_op.drop_column("author")
        batch_op.drop_column("version")
        batch_op.drop_column("source_url")
        batch_op.drop_column("tags")
        batch_op.drop_column("description")

    # Drop skill resources
    op.drop_index("idx_skill_resources_skill_path", table_name="skill_resources")
    op.drop_index("idx_skill_resources_skill_id", table_name="skill_resources")
    op.drop_table("skill_resources")
