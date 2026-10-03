"""Timing spans for the web UI's process tracking and Trajectory timeline.

One row per timed unit of a workstream's work: a user turn, an LLM call
(step), or a tool execution.  The live SSE stream carries the same timing
(``turn_end`` / ``step_timing`` / ``tool_result.duration_ms``), but SSE does
not replay history, so a reopened or past workstream rebuilds its timeline
from these rows via ``GET /v1/api/workstreams/{ws_id}/spans``.

A table of its own rather than ``conversations.meta``: ``meta`` is routed by
role into the canonical Turn model (and from there towards the provider
wire), several spans have no conversation row at all (a turn envelope, a
failed or retried LLM call), and ``usage_events`` already sets the
precedent of a per-LLM-call table beside the transcript.

Times are epoch milliseconds (INTEGER); the transcript's second-precision
text timestamps are too coarse for a timeline.

Revision ID: 081
Revises: 080
Create Date: 2026-10-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "081"
down_revision = "080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workstream_spans",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ws_id", sa.Text, nullable=False),
        sa.Column("turn_id", sa.Text, nullable=False, server_default=""),
        sa.Column("step", sa.Integer, nullable=False, server_default="0"),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("call_id", sa.Text, nullable=False, server_default=""),
        sa.Column("parent_call_id", sa.Text, nullable=False, server_default=""),
        sa.Column("name", sa.Text, nullable=False, server_default=""),
        sa.Column("started_at", sa.BigInteger, nullable=False),
        sa.Column("ended_at", sa.BigInteger, nullable=False),
        sa.Column("ttft_ms", sa.Integer, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default=""),
        sa.Column("tok_in", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tok_out", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tok_cache_read", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tok_cache_write", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created", sa.Text, nullable=False),
    )
    op.create_index("idx_workstream_spans_ws", "workstream_spans", ["ws_id", "started_at"])


def downgrade() -> None:
    op.drop_index("idx_workstream_spans_ws", table_name="workstream_spans")
    op.drop_table("workstream_spans")
