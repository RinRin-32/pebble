"""Per-user model/persona access allow-lists + per-guild persona preference.

Adds authority scoping (#tts-access): an operator creates a user in the console,
sets which models and personas that user may use, and links the user's API key
into Discord (per member via ``/link`` or per server via ``/global-link``).
Discord activity then acts *as* the linked user, so the same allow-list is
enforced whether the user is on the web console or driving the bot.

Three tables:

- ``user_allowed_models`` — ``(user_id, alias)``.  A user with zero rows is
  UNRESTRICTED (backward-compatible); any rows restrict them to the listed
  aliases.
- ``user_allowed_personas`` — ``(user_id, persona_id)``.  Same empty=unrestricted
  semantics.  Stores ``persona_id`` (stable across rename); the kind's default
  persona is always permitted by enforcement regardless of this list, so
  ``/orchestrate`` (coordinator) and the interactive default never get blocked.
- ``guild_prefs`` — ``(guild_id, persona)`` — the persona chosen by a server's
  ``/set-persona``, used for interactive workstreams when no explicit persona is
  given.  Keyed by ``guild_id`` so one linked user can run different personas in
  different servers; the global default persona remains the fallback.

Revision ID: 068
Revises: 067
Create Date: 2026-07-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "068"
down_revision = "067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_allowed_models",
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("alias", sa.Text, nullable=False),
        sa.Column("created", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("user_id", "alias"),
    )
    op.create_table(
        "user_allowed_personas",
        sa.Column("user_id", sa.Text, nullable=False),
        sa.Column("persona_id", sa.Text, nullable=False),
        sa.Column("created", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("user_id", "persona_id"),
    )
    op.create_table(
        "guild_prefs",
        sa.Column("guild_id", sa.Text, primary_key=True),
        sa.Column("persona", sa.Text, nullable=True),
        sa.Column("updated", sa.Text, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("guild_prefs")
    op.drop_table("user_allowed_personas")
    op.drop_table("user_allowed_models")
