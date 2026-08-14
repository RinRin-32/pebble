"""Graph knowledge base: notes on disk, links indexed in the database.

A knowledge base that grows across sessions and links freely between findings —
for iterative research and for accumulating what's been learned about a codebase.

Two halves, deliberately:

- **Source of truth is markdown on the shared /workspace volume**, in Obsidian's
  native dialect (YAML frontmatter + ``[[wikilinks]]``).  Plain files mean the
  operator can open the same folder in Obsidian for the graph view, edit notes by
  hand, and keep everything in git — no lock-in to turnstone's schema, and no
  attempt to containerize a desktop GUI.
- **The database indexes the link graph** so turnstone can traverse it
  programmatically (neighbours, backlinks, orphans) without re-parsing the whole
  vault on every query.  The index is derived state and is rebuilt from the files.

``kb_links.to_title`` intentionally stores the raw wikilink target rather than a
foreign key: Obsidian links may point at notes that do not exist yet, and those
dangling links are the useful part — they mark where research should go next.

Revision ID: 070
Revises: 069
Create Date: 2026-08-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "070"
down_revision = "069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kb_notes",
        sa.Column("note_id", sa.Text, primary_key=True),
        # Title doubles as the wikilink target, so it is unique per vault.
        sa.Column("title", sa.Text, nullable=False, unique=True),
        sa.Column("path", sa.Text, nullable=False),
        # Free-form classification (research, decision, codebase, ...).
        sa.Column("kind", sa.Text, nullable=False, server_default="note"),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("tags", sa.Text, nullable=False, server_default="[]"),
        # Provenance: which workstream/repo produced this.
        sa.Column("ws_id", sa.Text, nullable=False, server_default=""),
        sa.Column("repo_id", sa.Text, nullable=False, server_default=""),
        sa.Column("created", sa.Text, nullable=False),
        sa.Column("updated", sa.Text, nullable=False),
    )
    op.create_index("idx_kb_notes_kind", "kb_notes", ["kind"])
    op.create_index("idx_kb_notes_repo", "kb_notes", ["repo_id"])

    op.create_table(
        "kb_links",
        sa.Column("from_note", sa.Text, nullable=False),
        # Raw wikilink target; may name a note that doesn't exist yet.
        sa.Column("to_title", sa.Text, nullable=False),
        sa.Column("created", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("from_note", "to_title"),
    )
    op.create_index("idx_kb_links_to", "kb_links", ["to_title"])


def downgrade() -> None:
    op.drop_index("idx_kb_links_to", table_name="kb_links")
    op.drop_table("kb_links")
    op.drop_index("idx_kb_notes_repo", table_name="kb_notes")
    op.drop_index("idx_kb_notes_kind", table_name="kb_notes")
    op.drop_table("kb_notes")
