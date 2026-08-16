"""Scope skills to a repo, so one flat namespace stops accumulating junk.

``prompt_templates.name`` was globally UNIQUE.  That reads like a clean key and
behaves like a landfill: every project's skills share one namespace, so two
repos cannot each have a differently-tuned skill under the same obvious name,
and nothing ever has a reason to be removed because nothing is scoped to
anything.  Uniqueness moves to ``(name, repo_id)``.

Existing rows become GLOBAL (``repo_id = ''``) rather than being guessed into a
repo.  A skill that predates scoping was authored when every session could see
it, so silently narrowing it would remove capability from sessions that rely on
it today; widening later is a decision someone can make deliberately.

Lookup becomes repo-first-then-global, with the repo winning — which is why the
composite constraint is ``(name, repo_id)`` and not ``(repo_id, name)`` in
spirit: a bare name must still resolve to exactly one global row.

Batch mode because SQLite cannot drop a constraint in place; it rebuilds the
table.  The Postgres constraint has a server-generated name
(``prompt_templates_name_key``), so it is dropped by that name there and by
reflection under batch mode for SQLite.

Revision ID: 078
Revises: 077
Create Date: 2026-08-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "078"
down_revision = "077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table("prompt_templates") as batch_op:
        batch_op.add_column(sa.Column("repo_id", sa.Text, nullable=False, server_default=""))

    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE prompt_templates DROP CONSTRAINT IF EXISTS prompt_templates_name_key"
        )
        op.create_unique_constraint(
            "uq_prompt_templates_name_repo", "prompt_templates", ["name", "repo_id"]
        )
    else:
        # SQLite: the unique lives in the CREATE TABLE, so the table is
        # rebuilt with the composite constraint instead.
        with op.batch_alter_table("prompt_templates") as batch_op:
            batch_op.create_unique_constraint("uq_prompt_templates_name_repo", ["name", "repo_id"])

    # Lookup is repo-then-global on every skill resolution, which happens on
    # the session hot path.
    op.create_index("idx_prompt_templates_repo", "prompt_templates", ["repo_id", "name"])


def downgrade() -> None:
    op.drop_index("idx_prompt_templates_repo", table_name="prompt_templates")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE prompt_templates DROP CONSTRAINT IF EXISTS uq_prompt_templates_name_repo"
        )
        # Only restorable when no two rows share a name; a deployment that
        # used scoping has to resolve that itself, and failing loudly here is
        # better than dropping one of them.
        op.create_unique_constraint("prompt_templates_name_key", "prompt_templates", ["name"])
        op.execute("ALTER TABLE prompt_templates DROP COLUMN repo_id")
    else:
        with op.batch_alter_table("prompt_templates") as batch_op:
            batch_op.drop_constraint("uq_prompt_templates_name_repo", type_="unique")
            batch_op.create_unique_constraint("prompt_templates_name_key", ["name"])
            batch_op.drop_column("repo_id")
