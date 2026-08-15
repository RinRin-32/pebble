"""An optional display colour on knowledge notes.

The graph clusters by repo and picks a hue per cluster from a fixed palette.
That is a sensible default and a poor mandate: the person writing the notes
knows which codebase is which, and the palette cycles once there are more
repos than colours.

Empty means "choose for me", which is what every existing note gets.

Revision ID: 076
Revises: 075
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "076"
down_revision = "075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "kb_notes",
        sa.Column("color", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("kb_notes", "color")
