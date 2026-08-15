"""Knowledge interviews, with their transcripts.

Pebble questioning an agent about work it just did, so a finding is written
down properly rather than as whatever the agent volunteered.

The transcript is kept rather than discarded after the note is written.  When
a note later reads wrong, the useful question is what was *asked* to produce
it — and a distilled note cannot answer that about itself.

Revision ID: 075
Revises: 074
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "075"
down_revision = "074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kb_interviews",
        sa.Column("interview_id", sa.Text, primary_key=True),
        sa.Column("user_id", sa.Text, nullable=False, server_default=""),
        sa.Column("repo", sa.Text, nullable=False, server_default=""),
        sa.Column("topic", sa.Text, nullable=False, server_default=""),
        sa.Column("state", sa.Text, nullable=False, server_default="open"),
        sa.Column("rounds", sa.Integer, nullable=False, server_default="0"),
        sa.Column("transcript", sa.Text, nullable=False, server_default="[]"),
        sa.Column("note_title", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )
    op.create_index("idx_kb_interviews_user", "kb_interviews", ["user_id"])


def downgrade() -> None:
    op.drop_index("idx_kb_interviews_user", table_name="kb_interviews")
    op.drop_table("kb_interviews")
