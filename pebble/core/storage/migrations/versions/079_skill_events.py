"""Skill usage events, so the janitor has evidence instead of a guess.

Two event kinds, and the distinction is the whole point:

``pulled`` — pebble sent this skill to an edge device.  Recorded server-side,
always true, and NOT usage.  A skill can be shipped in every bundle and never
once invoked.

``invoked`` — the edge actually ran it.  Reported back by a hook, because the
alternative (an MCP tool the session calls when it feels like it) measures a
model's conscientiousness rather than a skill's utility.

There is deliberately no ``worked`` / ``failed``.  Outcome is not observable
from a tool-call boundary, and a column that would have to be invented is
worse than a column that is missing: the janitor would then delete on
fabricated evidence.

``session_id`` is carried so repeated invocations inside one session can be
told apart from adoption across many.  A skill invoked forty times by one
session is a loop, not popularity.

Revision ID: 079
Revises: 078
Create Date: 2026-08-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "079"
down_revision = "078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "skill_events",
        sa.Column("event_id", sa.Text, primary_key=True),
        # The skill's row id when known. Kept alongside name+repo rather than
        # instead of them: a pull is evidence even after the skill is deleted,
        # and a foreign key would erase exactly the history the janitor needs
        # to explain what it removed.
        sa.Column("skill_id", sa.Text, nullable=False, server_default=""),
        sa.Column("name", sa.Text, nullable=False, server_default=""),
        sa.Column("repo_id", sa.Text, nullable=False, server_default=""),
        sa.Column("user_id", sa.Text, nullable=False, server_default=""),
        # pulled | invoked
        sa.Column("event", sa.Text, nullable=False),
        sa.Column("session_id", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
    )
    op.create_index("idx_skill_events_skill", "skill_events", ["skill_id", "event"])
    op.create_index("idx_skill_events_name_repo", "skill_events", ["name", "repo_id"])


def downgrade() -> None:
    op.drop_index("idx_skill_events_name_repo", table_name="skill_events")
    op.drop_index("idx_skill_events_skill", table_name="skill_events")
    op.drop_table("skill_events")
