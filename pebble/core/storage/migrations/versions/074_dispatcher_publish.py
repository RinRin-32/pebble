"""Give the dispatcher persona the ``publish_work`` tool.

The dispatcher exists to delegate coding work and review what comes back.
Until now the reviewing ended there: the diff sat in a worktree and a human had
to extract it by hand.  Publishing is the other half of that job, and it is
still delegation rather than editing — the persona commits and pushes what the
agent wrote, it does not write code itself.

The tool_allowlist is a hard set (that is the whole point of this persona), so
adding a tool means rewriting the row rather than relying on a default.

Revision ID: 074
Revises: 073
Create Date: 2026-08-15
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "074"
down_revision = "073"
branch_labels = None
depends_on = None

PERSONA_NAME = "dispatcher"
NEW_TOOL = "publish_work"


def _rewrite(add: bool) -> None:
    bind = op.get_bind()
    row = bind.execute(
        sa.text("SELECT tool_allowlist FROM personas WHERE name = :n"),
        {"n": PERSONA_NAME},
    ).fetchone()
    if row is None or not row[0]:
        return
    try:
        tools = json.loads(row[0])
    except (TypeError, ValueError):
        return
    if not isinstance(tools, list):
        return
    if add and NEW_TOOL not in tools:
        tools.append(NEW_TOOL)
    elif not add and NEW_TOOL in tools:
        tools = [t for t in tools if t != NEW_TOOL]
    bind.execute(
        sa.text("UPDATE personas SET tool_allowlist = :t WHERE name = :n"),
        {"t": json.dumps(tools), "n": PERSONA_NAME},
    )


def upgrade() -> None:
    _rewrite(add=True)


def downgrade() -> None:
    _rewrite(add=False)
