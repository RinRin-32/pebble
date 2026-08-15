"""Per-user git push credentials, stored encrypted.

A dispatched agent produces a diff; pushing it needs a credential.  The
instance-level token (``PEBBLE_GIT_TOKEN``) works, but it is one identity
shared by everyone: under ``/global-link`` that is every member of a Discord
server pushing as the same bot, with GitHub unable to tell them apart or to
bound what any one of them may reach.

A per-user token inverts that.  GitHub's own permissions become the
enforcement boundary — pebble does not have to model repo ACLs — the commit
and the PR say who actually asked for the change, and revoking one person's
access is something they can do themselves.

The token is encrypted at rest (``core/secret_cipher.py``); ``token_hint``
holds only a short trailing fragment so the console can say "set, ending
1a2b" without ever reading the secret back out.

Revision ID: 073
Revises: 072
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "073"
down_revision = "072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_git_credentials",
        sa.Column("user_id", sa.Text, primary_key=True),
        sa.Column("host", sa.Text, nullable=False, server_default="github.com"),
        sa.Column("login", sa.Text, nullable=False, server_default=""),
        sa.Column("token_ct", sa.LargeBinary, nullable=False),
        sa.Column("token_hint", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("user_git_credentials")
