"""A ``dispatcher`` persona that can only delegate coding work, never do it.

Observed during a real coordinator fan-out: a spawned child was told to call
``bind_repo`` -> ``setup_env`` -> ``dispatch_agent``, and instead bound the repo
and then edited the file itself with ``edit_file`` and ``bash``.  That is
reasonable model judgment on a one-line change, and wrong for the architecture:

* the dispatched agent CLIs are better at coding than the orchestration layer,
  which is the entire reason turnstone delegates rather than reimplementing an
  agent loop; and
* work done directly in the child is invisible to the dispatch path — no agent
  session to resume, no per-run cost attribution, no adapter event stream.

Prompting alone does not fix this reliably; a capable model reasonably concludes
the direct edit is simpler.  So the constraint is made structural using the
persona ``tool_allowlist`` that already exists: a dispatcher child is given the
tools to *understand* and *delegate* and simply does not have the tools to edit.

Allowed: bind_repo, setup_env, dispatch_agent (the delegation chain); read_file,
search, diff_file (understand the code well enough to brief an agent, and review
what came back); kb, memory, recall (record and reuse findings); notify.

Deliberately absent: bash, write_file, edit_file, task_agent — every route to
doing the work directly.  A coordinator spawns coding children with
``persona="dispatcher"``.

Revision ID: 071
Revises: 070
Create Date: 2026-08-14
"""

from __future__ import annotations

import datetime
import json

import sqlalchemy as sa
from alembic import op

revision = "071"
down_revision = "070"
branch_labels = None
depends_on = None

PERSONA_ID = "builtin-dispatcher"
PERSONA_NAME = "dispatcher"

ALLOWED_TOOLS = [
    "bind_repo",
    "setup_env",
    "dispatch_agent",
    "read_file",
    "search",
    "diff_file",
    "kb",
    "memory",
    "recall",
    "notify",
]

PROMPT = """You coordinate coding work on one repository. You do not write code yourself.

You have no editing tools. That is deliberate: the dispatched agent CLIs are
better at writing code than you are, and work you did directly would be
invisible to the dispatch path — no resumable agent session, no cost
attribution, no streamed tool events.

Your loop:

1. `bind_repo` — bind the repository you were asked about.
2. `setup_env` (action="use") — provision its toolchain, so the agent you
   dispatch can build and test rather than only edit.
3. Read enough of the code (`read_file`, `search`) to write a brief a competent
   engineer could act on: what to change, where, and how to know it worked.
4. `dispatch_agent` — hand over that brief. Prefer one well-specified dispatch
   to several vague ones.
5. Review the returned diff. If it is wrong or incomplete, dispatch again with
   `continue_session=true` so the agent keeps its context.
6. Record anything worth knowing next time with `kb` — especially measured
   results, via `kb(action="experiment")`.

Report what changed, what was verified, and what you are unsure about. Never
claim a change was tested unless the diff or an experiment shows it."""


def upgrade() -> None:
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S")
    op.get_bind().execute(
        sa.text(
            "INSERT INTO personas (persona_id, name, display_name, description, "
            "base_prompt, tool_allowlist, mcp_enabled, memory_enabled, "
            "applies_to_kinds, is_default, enabled, org_id, created_by, created, updated) "
            "VALUES (:pid, :name, :dname, :desc, :prompt, :tools, 1, 1, "
            ":kinds, 0, 1, '', 'migration', :now, :now)"
        ),
        {
            "pid": PERSONA_ID,
            "name": PERSONA_NAME,
            "dname": "Dispatcher",
            "desc": (
                "Delegates coding work to an agent CLI. Can read, brief and review — "
                "structurally cannot edit."
            ),
            "prompt": PROMPT,
            "tools": json.dumps(ALLOWED_TOOLS),
            "kinds": json.dumps(["interactive"]),
            "now": now,
        },
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM personas WHERE persona_id = :pid"), {"pid": PERSONA_ID}
    )
