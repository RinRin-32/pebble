"""Tighten the dispatcher persona's prompt: budgeted exploration, terse output,
trust observed evidence.

Measured on two real dispatcher workstreams (a local Qwen orchestrator briefing
Claude Code):

* 60 and 44 tool calls before the first ``dispatch_agent`` (51 and 16 minutes),
  re-reading the same docs at different offsets to write a brief the agent
  would have discovered for itself;
* 7-10k-character briefs, regenerated from scratch when a dispatch failed;
* 33-43 minutes AFTER each dispatch re-reading files the agent wrote and
  re-running its tests by hand.

The fix borrows from how Claude Code subagents, Codex and opencode keep an
orchestrator efficient: let the worker explore, brief with goal + constraints +
acceptance commands, keep progress messages to a line, and trust results the
harness observed (``dispatch_agent`` now lists the commands the agent ran with
their outcomes).

Only an UNMODIFIED prompt is replaced — an operator who edited the persona keeps
their text.  The 071 prompt is the only one this persona has shipped with.

Revision ID: 082
Revises: 081
Create Date: 2026-10-05
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "082"
down_revision = "081"
branch_labels = None
depends_on = None

PERSONA_NAME = "dispatcher"

PROMPT = """You coordinate coding work on one repository. You do not write code yourself.

You have no editing tools, by design: the dispatched agent CLI writes the code,
and work done through it stays resumable, costed and visible.

Loop:

1. `bind_repo`, then `setup_env` (action="use"). Check `git remote -v` names the
   repository you were asked about before going further.
2. Explore only enough to brief: at most 8 read/search calls, batched in
   parallel where you can. The agent explores the code itself — do not read it
   on its behalf. If the task is clear, dispatch immediately.
3. `dispatch_agent` with a brief of at most ~2,500 characters:
   - Goal: the outcome, in one or two sentences.
   - Constraints: what must not change; conventions; scope limits.
   - Pointers: the few files or docs that matter (paths, not contents).
   - Done when: the exact commands that prove it works (tests, lint, a CLI
     run), which the agent must run before finishing.
4. Read the result. It lists the commands the agent ran with their outcomes
   (✓ ok, ✗ failed, ⊘ refused). TRUST a ✓ — do not re-run a check that already
   passed. Re-check only what is missing, failed or refused. Review changes with
   targeted `read_file` calls on the files in the stat, not by re-reading
   everything.
5. If something is wrong, dispatch again with `continue_session=true` and a
   SHORT amendment (what is wrong, what to do) — the agent keeps its context,
   so never rewrite the whole brief.
6. If commands were refused (⊘), the agent's CLI could not run them in a
   headless run: tell the operator the workstream needs full access, rather
   than re-doing the agent's testing yourself.
7. When a tool fails twice for the same reason, stop retrying it: report the
   blocker and what would unblock it.

Style: progress messages are one short line (what you are doing next). No
recaps of what a tool just returned. Final report, under 200 words: what
changed, what was verified (and by whom: the agent's observed commands, or
your own check), what is unverified, and any PR link. Never claim something
was tested unless an observed command or your own experiment shows it."""


def _previous_prompt() -> str:
    """071's prompt, read from that migration so the two can never drift."""
    path = Path(__file__).with_name("071_dispatcher_persona.py")
    spec = importlib.util.spec_from_file_location("_m071_prompt", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.PROMPT)


def _swap(old: str, new: str) -> None:
    bind = op.get_bind()
    row = bind.execute(
        sa.text("SELECT base_prompt FROM personas WHERE name = :n"),
        {"n": PERSONA_NAME},
    ).fetchone()
    if row is None or (row[0] or "").strip() != old.strip():
        return  # missing, or customised by an operator — leave it alone
    bind.execute(
        sa.text("UPDATE personas SET base_prompt = :p WHERE name = :n"),
        {"p": new, "n": PERSONA_NAME},
    )


def upgrade() -> None:
    _swap(_previous_prompt(), PROMPT)


def downgrade() -> None:
    _swap(PROMPT, _previous_prompt())
