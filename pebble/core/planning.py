"""Talking a plan through with pebble, from a session on another machine.

The edge session knows the repository it is sitting in.  Pebble knows every
repository it has ever been told about.  Planning is where that asymmetry pays
— "we already solved this in intent-router, and here is the note" is worth more
than any amount of reasoning about the file in front of you.

**Why this is not** :mod:`pebble.core.interview`.  The two share a shape (a
persisted multi-turn conversation with a model) and have opposite rules.  An
interview is extractive: pebble asks, the engineer answers, and it must end in
a written note within three rounds.  A plan is generative: the engineer
proposes, pebble challenges, and it ends when the thinking is done — with a
note only if asked for one.  Forcing a plan through the interview's rules
would cap the conversation exactly where it gets useful.

**The vault is re-read every turn.**  The interview reads it once at open,
which is fine for a debrief that lasts three exchanges and wrong here: notes
written *during* a planning session were invisible to it, so it confidently
reported gaps that had just been filled.  A planner that cannot see what you
just wrote is worse than one with no memory, because it is confidently stale.

**The brake is spend, not turns.**  The interview's round cap works because
both parties want to finish.  Neither party to a planning conversation does —
there is always another consideration — so what is reported back each turn is
the token spend, and the ceiling is a runaway guard rather than a schedule.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from pebble.core.interview import _ask_model, _turns
from pebble.core.log import get_logger

log = get_logger(__name__)

#: Runaway guard, not a schedule.  A real planning conversation is 5-15
#: exchanges; this exists so a loop in a caller cannot bill forever.
MAX_TURNS = 40

#: Ceiling on total model output for one conversation.  Reported every turn so
#: the caller can see it coming rather than hit it.
MAX_PLAN_TOKENS = 120_000

#: Per-message cap on what the caller sends, quoted back when it bites.
MAX_MESSAGE_CHARS = 12_000

#: How much of the vault to put in front of the planner each turn.  Larger
#: than the interview's, because a planner is asked to connect things across
#: repositories rather than to ask about one.
MAX_VAULT_NOTES = 8
MAX_VAULT_CHARS = 6000

#: Output allowance per reply.  A reasoning model bills thinking against this
#: ceiling, so a budget fitted to the visible answer gets truncated replies.
#:
#: Higher than the interview's because a planning prompt is bigger — it
#: carries the engineer's context AND a vault search on every turn — and
#: reasoning scales with the prompt: 4_000 was measured fine on a 2_700-char
#: prompt and returned zero content twice on a real console-design one.
#: `_ask_model` also DOUBLES this on a truncated attempt, so this is a
#: starting point rather than a limit.
REPLY_TOKENS = 8000
MIN_REPLY_CHARS = 60

_SYSTEM = """You are pebble, planning a piece of work with an engineer who is \
in another codebase right now and cannot see what you know.

Your advantage is memory across projects. Use it: when the vault holds \
something relevant, say so and name the note title exactly, so they can read \
it. When it does not, say that too rather than inventing a precedent.

How to be useful here:
- Challenge the approach before refining it. If the plan solves the wrong \
problem, say so in the first line.
- Ask about what would make this hard: prior art, load-bearing assumptions, \
what breaks if it is wrong.
- Prefer one sharp question to three broad ones.
- Say plainly what you do not know. You cannot see their code.
- Do not write the implementation. Shape the plan; they write the code.

Keep replies tight — a few paragraphs, or a short list. This is a \
conversation, not a document."""

_NOTE = """Write the planning note now, from this conversation.

Format:
TITLE: <the decision or approach settled on, not "plan for X">
SUMMARY: <one line someone can scan>
BODY: <markdown. What was decided and why. What was considered and rejected, \
with the reason — that is the part nobody can reconstruct later. What is still \
open. Reference related notes as [[Wiki Links]], using each note's title \
EXACTLY as it was given to you — do not append a kind, a category or any \
parenthetical. A link whose title you altered points at nothing, and this \
vault treats a link to a missing note as unwritten research, so an invented \
suffix files a real note as a gap.>

Record what the conversation actually established. If it ended without \
settling something important, say so in the body rather than inventing a \
resolution."""


def _planner_alias(config_store: Any) -> str:
    """Which model plans.

    Its own setting first, because planning and reviewing want different
    things — a reviewer is rewarded for finding fault, a planner for finding a
    route — but falling back through the reviewer to the default so a
    deployment that has not thought about it still gets a planner.
    """
    for key in ("agents.planner_model_alias", "agents.reviewer_model_alias", "model.default_alias"):
        alias = (config_store.get(key, "") or "").strip()
        if alias:
            return str(alias)
    return ""


def vault_context(query: str, repo: str = "") -> str:
    """What the vault knows that bears on *query*, re-read every turn.

    Searched unscoped when no repo is given: the whole point of asking pebble
    rather than thinking alone is that the answer may live in a different
    project. A repo, when supplied, ranks that project's notes first without
    excluding the rest.
    """
    lines: list[str] = []
    try:
        from pebble.core.knowledge import search_notes

        seen: set[str] = set()
        for scope in [repo, ""] if repo else [""]:
            for note, _score in search_notes(query, limit=MAX_VAULT_NOTES, repo=scope):
                if note.title in seen:
                    continue
                seen.add(note.title)
                lines.append(f"- {note.title} ({note.kind}): {note.summary or '(no summary)'}")
            if len(seen) >= MAX_VAULT_NOTES:
                break
    except Exception:
        log.debug("planning.vault_search_failed", exc_info=True)
    if not lines:
        return "The vault has nothing on this yet."
    return ("Possibly relevant notes in the vault:\n" + "\n".join(lines[:MAX_VAULT_NOTES]))[
        :MAX_VAULT_CHARS
    ]


def _budget(turns: int, tokens: int) -> dict[str, Any]:
    return {
        "turn": turns,
        "turns_max": MAX_TURNS,
        "tokens_spent": tokens,
        "tokens_max": MAX_PLAN_TOKENS,
        "message_chars_max": MAX_MESSAGE_CHARS,
    }


def _clip(text: str) -> tuple[str, bool]:
    body = (text or "").strip()
    return (body[:MAX_MESSAGE_CHARS], len(body) > MAX_MESSAGE_CHARS)


def _estimate_tokens(text: str) -> int:
    """Rough output accounting: 4 chars/token, the same constant the session
    layer calibrates against. Exact enough for a spend ceiling, and it costs
    no extra API call to know."""
    return len(text) // 4


def start(
    storage: Any,
    config_store: Any,
    *,
    user_id: str,
    goal: str,
    context: str = "",
    repo: str = "",
) -> dict[str, Any]:
    """Open a planning conversation and return pebble's first response."""
    goal = (goal or "").strip()
    if not goal:
        return {"ok": False, "error": "goal is required", "retryable": False}

    body, truncated = _clip(context)
    known = vault_context(f"{goal} {body[:400]}", repo)
    opening = (
        f"Goal: {goal}\n\n"
        f"Repo: {repo or '(not stated)'}\n\n"
        f"What the engineer says:\n{body or '(nothing yet)'}\n\n"
        f"{known}"
    )
    transcript = [{"role": "user", "content": opening}]
    reply, err = _ask_model(
        config_store,
        storage,
        _turns(_SYSTEM, transcript),
        max_tokens=REPLY_TOKENS,
        min_chars=MIN_REPLY_CHARS,
        alias=_planner_alias(config_store),
    )
    if not reply:
        return {
            "ok": False,
            "error": err or "the planner model returned nothing",
            # An unconfigured model is a settings problem; a failed call is
            # usually transient. Retrying the first is a waste of a round trip.
            "retryable": "not registered" not in err and "no " not in err[:12],
        }

    plan_id = uuid.uuid4().hex
    transcript.append({"role": "assistant", "content": reply})
    storage.create_plan(plan_id, user_id=user_id, repo=repo, goal=goal)
    spent = _estimate_tokens(reply)
    storage.update_plan(plan_id, transcript=json.dumps(transcript), turns=1, tokens=spent)
    return {
        "ok": True,
        "plan_id": plan_id,
        "reply": reply,
        "context_truncated": truncated,
        "budget": _budget(1, spent),
    }


def reply(
    storage: Any,
    config_store: Any,
    *,
    plan_id: str,
    message: str,
    user_id: str = "",
) -> dict[str, Any]:
    """Continue a planning conversation."""
    row = storage.get_plan(plan_id)
    if row is None:
        return {"ok": False, "error": "no such plan", "retryable": False}
    if user_id and row["user_id"] and row["user_id"] != user_id:
        # Someone else's planning session: their goal, their context, and a
        # reply here would be attributed to them.
        return {"ok": False, "error": "that plan belongs to another user", "retryable": False}
    if row["state"] != "open":
        return {"ok": False, "error": "that plan is closed", "retryable": False}

    turns, tokens = row["turns"], row["tokens"]
    if turns >= MAX_TURNS:
        return {
            "ok": False,
            "error": f"this conversation has run {MAX_TURNS} turns; close it and open a new one",
            "budget": _budget(turns, tokens),
            "retryable": False,
        }
    if tokens >= MAX_PLAN_TOKENS:
        return {
            "ok": False,
            "error": "this conversation has spent its token budget; close it and open a new one",
            "budget": _budget(turns, tokens),
            "retryable": False,
        }

    try:
        transcript = json.loads(row["transcript"] or "[]")
    except Exception:
        log.warning("planning.transcript_unreadable", plan_id=plan_id, exc_info=True)
        transcript = []

    body, truncated = _clip(message)
    # Re-read the vault against what was JUST said, not against the opening
    # goal: a conversation moves, and notes written since it opened are the
    # ones most likely to matter.
    known = vault_context(f"{row['goal']} {body[:400]}", row["repo"])
    transcript.append({"role": "user", "content": f"{body}\n\n---\n{known}"})

    text, err = _ask_model(
        config_store,
        storage,
        _turns(_SYSTEM, transcript),
        max_tokens=REPLY_TOKENS,
        min_chars=MIN_REPLY_CHARS,
        alias=_planner_alias(config_store),
    )
    if not text:
        # The caller's message is NOT persisted on failure: replaying it would
        # otherwise duplicate it into the transcript.
        return {
            "ok": False,
            "error": err or "the planner model returned nothing",
            "budget": _budget(turns, tokens),
            "retryable": True,
        }

    transcript.append({"role": "assistant", "content": text})
    turns += 1
    tokens += _estimate_tokens(text)
    storage.update_plan(plan_id, transcript=json.dumps(transcript), turns=turns, tokens=tokens)
    return {
        "ok": True,
        "plan_id": plan_id,
        "reply": text,
        "message_truncated": truncated,
        "budget": _budget(turns, tokens),
    }


def close(
    storage: Any,
    config_store: Any,
    *,
    plan_id: str,
    write_note: bool = False,
    user_id: str = "",
) -> dict[str, Any]:
    """Close a planning conversation, optionally writing it up.

    The note is *offered*, not forced. An interview must end in writing
    because extracting knowledge and not recording it is the whole failure it
    exists to prevent; a plan that gets abandoned mid-conversation should not
    leave a confident note behind saying what was decided.
    """
    row = storage.get_plan(plan_id)
    if row is None:
        return {"ok": False, "error": "no such plan"}
    if user_id and row["user_id"] and row["user_id"] != user_id:
        return {"ok": False, "error": "that plan belongs to another user"}

    try:
        transcript = json.loads(row["transcript"] or "[]")
    except Exception:
        transcript = []

    if not write_note:
        storage.update_plan(
            plan_id,
            transcript=json.dumps(transcript),
            turns=row["turns"],
            tokens=row["tokens"],
            state="closed",
        )
        return {"ok": True, "plan_id": plan_id, "closed": True, "note_written": False}

    from pebble.core.interview import MIN_NOTE_CHARS, NOTE_TOKENS, _parse_note

    text, err = _ask_model(
        config_store,
        storage,
        # The instruction is repeated as a final user turn for the same reason
        # it is in the interview: a model answers the last thing it was asked,
        # and a swapped system prompt over a transcript that ends
        # mid-conversation gets a reply ABOUT the conversation.
        _turns(_NOTE, [*transcript, {"role": "user", "content": _NOTE}]),
        attempts=3,
        max_tokens=NOTE_TOKENS,
        min_chars=MIN_NOTE_CHARS,
        require="TITLE:",
        alias=_planner_alias(config_store),
    )
    if not text:
        # Left OPEN deliberately: closing it here would strand a conversation
        # the caller asked to have written up, with nothing written.
        return {
            "ok": False,
            "error": f"could not write the note: {err}",
            "closed": False,
            "retryable": True,
        }

    title, summary, body = _parse_note(text, fallback_title=row["goal"])
    try:
        from pebble.core.knowledge import Note, extract_links
        from pebble.core.knowledge import write_note as _write

        note = Note(
            title=title,
            body=body,
            kind="plan",
            summary=summary,
            repo_id=row["repo"],
            ws_id=f"plan:{plan_id}",
            links=extract_links(body),
        )
        _write(note)
        from pebble.core.kb_mcp import _sync_index

        _sync_index()
    except Exception as exc:
        log.warning("planning.note_write_failed", plan_id=plan_id, exc_info=True)
        return {"ok": False, "error": f"could not file the note: {exc}", "closed": False}

    storage.update_plan(
        plan_id,
        transcript=json.dumps(transcript),
        turns=row["turns"],
        tokens=row["tokens"],
        state="closed",
        note_title=title,
    )
    return {"ok": True, "plan_id": plan_id, "closed": True, "note_written": True, "title": title}
