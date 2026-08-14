"""Per-user capability grants — currently just code dispatch.

Distinct from the model/persona allow-lists, which narrow WHICH model a user may
pick.  This gates whether a user may run a coding agent at all, and the reason is
whose money is at stake: a dispatch spends the OPERATOR's credentials — a Claude
subscription or an OpenRouter key mounted into the containers — not the caller's.
In a Discord server with ``/global-link``, that is every member.

Enforcement is opt-in via ``agents.dispatch_requires_grant`` (default False) so
adding this table does not silently break an existing deployment; flipping the
setting turns it on.

Revision ID: 072
Revises: 071
Create Date: 2026-08-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "072"
down_revision = "071"
branch_labels = None
depends_on = None

CAPABILITY_CODE_DISPATCH = "code_dispatch"


def upgrade() -> None:
    op.create_table(
        "user_capabilities",
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("capability", sa.Text, nullable=False),
        sa.Column("granted_by", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("user_id", "capability"),
    )
    op.create_index("idx_user_capabilities_cap", "user_capabilities", ["capability"])


def downgrade() -> None:
    op.drop_index("idx_user_capabilities_cap", table_name="user_capabilities")
    op.drop_table("user_capabilities")
