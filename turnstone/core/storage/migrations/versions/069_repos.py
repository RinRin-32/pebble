"""Git repositories a workstream can be bound to (coding-agent dispatch).

Turnstone's shell tools ran with no working directory of their own, so there was
nowhere to *do* coding work: no checkout, no isolation between concurrent
workstreams, and no way to get a diff back out.  This table names the repos the
cluster knows about; :mod:`turnstone.core.workspace` turns one into a bare mirror
at ``/workspace/repos/<repo_id>.git`` and gives each workstream its own
``git worktree`` under ``/workspace/ws/<ws_id>``.

``/workspace`` is a volume every node mounts, so a worktree created on the node
that handled the create is visible to whichever node later serves the session —
dispatch works across the cluster without pinning.

The workstream→repo binding is NOT a column here: it rides ``workstream_config``
under the ``repo_id`` key, the same way ``model_alias`` and the persona stamp do.

Revision ID: 069
Revises: 068
Create Date: 2026-08-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "069"
down_revision = "068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "repos",
        sa.Column("repo_id", sa.Text, primary_key=True),
        sa.Column("name", sa.Text, nullable=False, unique=True),
        sa.Column("git_url", sa.Text, nullable=False),
        sa.Column("default_branch", sa.Text, nullable=False, server_default="main"),
        sa.Column("project_id", sa.Text, nullable=False, server_default=""),
        sa.Column("enabled", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_by", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )
    op.create_index("idx_repos_enabled", "repos", ["enabled"])


def downgrade() -> None:
    op.drop_index("idx_repos_enabled", table_name="repos")
    op.drop_table("repos")
