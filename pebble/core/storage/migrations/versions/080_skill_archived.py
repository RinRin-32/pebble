"""Archive a skill instead of deleting it.

The janitor needs teeth without being able to destroy anything.  Archiving is
that: an archived skill is hidden from ``list_skills`` and from transfer
bundles, so it stops costing anyone context, and it is still there — the row,
its resources, and its whole event history.

A timestamp rather than a boolean, because "when" is the question that comes
next: a skill archived this morning by a janitor run is a different situation
from one archived six months ago that nobody has missed, and only the second
is a deletion candidate.  ``""`` means active.

``archived_by`` distinguishes the janitor's own doing from an operator's.  A
sweep that archived forty skills should be recognisable as one action, and an
operator's deliberate archive should not be undone by a janitor that thinks it
made a mistake.

Revision ID: 080
Revises: 079
Create Date: 2026-08-16
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "080"
down_revision = "079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("prompt_templates") as batch_op:
        batch_op.add_column(sa.Column("archived", sa.Text, nullable=False, server_default=""))
        batch_op.add_column(sa.Column("archived_by", sa.Text, nullable=False, server_default=""))
    op.create_index("idx_prompt_templates_archived", "prompt_templates", ["archived"])


def downgrade() -> None:
    op.drop_index("idx_prompt_templates_archived", table_name="prompt_templates")
    with op.batch_alter_table("prompt_templates") as batch_op:
        batch_op.drop_column("archived_by")
        batch_op.drop_column("archived")
