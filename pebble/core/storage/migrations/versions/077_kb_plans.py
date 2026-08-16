"""Planning conversations with pebble, and their transcripts.

A session on another machine talks a plan through with pebble, which brings
what the vault knows about every codebase to a conversation about one.

Its own table rather than a ``kind`` column on ``kb_interviews``: the two look
alike (a persisted multi-turn conversation keyed by id and owner) but their
rules are opposites.  An interview is capped at three rounds and must end in a
written note; a plan runs as long as the thinking takes and writes one only if
asked.  A shared table would make every read a discriminated union, and every
rule a conditional on a column.

``tokens`` is carried because the brake here is spend, not rounds — the
interview's cap works because both sides want to stop, and neither side of a
planning conversation does.

Revision ID: 077
Revises: 076
Create Date: 2026-08-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "077"
down_revision = "076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kb_plans",
        sa.Column("plan_id", sa.Text, primary_key=True),
        sa.Column("user_id", sa.Text, nullable=False, server_default=""),
        sa.Column("repo", sa.Text, nullable=False, server_default=""),
        sa.Column("goal", sa.Text, nullable=False, server_default=""),
        # open | closed
        sa.Column("state", sa.Text, nullable=False, server_default="open"),
        sa.Column("turns", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("transcript", sa.Text, nullable=False, server_default="[]"),
        sa.Column("note_title", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )
    op.create_index("idx_kb_plans_user", "kb_plans", ["user_id"])


def downgrade() -> None:
    op.drop_index("idx_kb_plans_user", table_name="kb_plans")
    op.drop_table("kb_plans")
