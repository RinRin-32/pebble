"""Pebble interviewing an agent about work it just did.

The MCP write tools take whatever a session volunteers.  That is usually the
happy path — what was changed, that it worked — and it omits the parts worth
keeping: what was tried and abandoned, what the number actually was, what the
next person will trip over.  A note written from the volunteered version reads
fine and teaches nothing.

So this inverts the flow.  The edge session says what it did; pebble asks
about it, with the vault and (when it has one) the codebase in hand; and
pebble writes the note from the answers.  It is the senior-engineer move of
asking "what did you actually measure?" before letting something be written
down as known.

**Context.** Good questions require knowing the subject.  Pebble pulls prior
notes for the repo, so it can ask about contradictions instead of re-asking
settled things, and reads the code graph when the repo is bound locally.  For
a repo it has never seen — the normal case for a laptop — the context has to
come from the edge, which is why the opening call takes one.

**Budget.** Every exchange is a round trip across the internet paid for in
somebody's tokens, and both sides have an incentive to keep talking: the model
can always think of another question, and an agent can always give a longer
answer.  So the budget is fixed, small, and *stated in every response*.  When
it runs out the note is written from whatever is there — dragging it out buys
nothing, which is the only reliable way to stop it.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pebble.core.log import get_logger

if TYPE_CHECKING:
    from pebble.core.trajectory import Turn

log = get_logger(__name__)

#: Hard ceiling on exchanges before the note is written.  Three is enough for
#: "what did you do" -> "what did you measure" -> "what would you warn about";
#: beyond that the answers repeat.
MAX_ROUNDS = 3

#: Per-round caps.  Both are quoted back to the caller so the limit shapes the
#: answer rather than silently truncating it.
MAX_ANSWER_CHARS = 6000
MAX_CONTEXT_CHARS = 8000
MAX_QUESTIONS = 3

#: Floors below which a model reply is treated as truncated rather than short.
#: A question set has to be at least a sentence, and a note that fits in a
#: tweet is not worth filing.  Both are well under any genuine reply, so they
#: catch provider truncation without rejecting a terse-but-real one.
MIN_QUESTION_CHARS = 40
MIN_NOTE_CHARS = 120

#: Output allowance per call.  Sized for a REASONING reviewer, where thinking
#: is billed against the same ceiling as the answer: at 1_200 tokens, four of
#: eight calls on this module's prompt stopped on ``length`` and the replies
#: that survived were fragments.  At 4_000, none of six truncated, and the
#: questions themselves only ever run 400-650 chars — the headroom is entirely
#: for reasoning, so trimming this to "what the answer needs" reopens the bug.
QUESTION_TOKENS = 4000
NOTE_TOKENS = 6000

#: Ceiling when a truncated attempt is retried at a larger budget.  A prompt
#: that makes the model reason past its allowance will do so again at the same
#: allowance, so the retry doubles instead of repeating — bounded here so a
#: pathological prompt cannot escalate without limit.
MAX_ESCALATED_TOKENS = 24_000

#: How much prior knowledge to put in front of the interviewer.
MAX_PRIOR_NOTES = 6


@dataclass
class Budget:
    """What is left, from both sides' point of view."""

    round: int
    max_rounds: int
    answer_chars: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "of": self.max_rounds,
            "answer_chars_max": self.answer_chars,
            "note": (
                "Answer in this round's budget. When the rounds run out the note is "
                "written from what has been said, so padding costs you accuracy, not time."
            ),
        }


def _reviewer_alias(config_store: Any) -> str:
    """Which model asks the questions.

    The reviewer role, falling back to the default alias — a deployment that
    has not chosen a reviewer should still get an interview rather than an
    error, just a less specialised one.
    """
    for key in ("agents.reviewer_model_alias", "model.default_alias"):
        try:
            val = (config_store.get(key) or "").strip() if config_store else ""
        except Exception:
            val = ""
        if val:
            return val
    return ""


def gather_context(repo: str, topic: str) -> str:
    """What pebble already knows about this repo, for the interviewer.

    Prior notes come first: the point is to ask about gaps and contradictions
    rather than re-asking what is already written down.  The code graph joins
    when the repo is bound here — for a laptop's repo it will not be, and the
    caller's own context is all there is.
    """
    parts: list[str] = []
    try:
        from pebble.core.knowledge import search_notes

        hits = search_notes(topic or repo, limit=MAX_PRIOR_NOTES, repo=repo)
        if hits:
            lines = [f"- {n.title} ({n.kind}): {n.summary or '(no summary)'}" for n, _ in hits]
            parts.append("Notes already in the vault for this repo:\n" + "\n".join(lines))
        else:
            parts.append(f"The vault has nothing recorded for repo {repo!r} yet.")
    except Exception:
        log.debug("interview.vault_context_failed", exc_info=True)

    try:
        from pebble.core.workspace import worktree_path

        wt = worktree_path(repo)
        if wt.exists():
            parts.append(f"This repo is bound locally at {wt}; a code graph may exist.")
    except Exception:
        pass
    return "\n\n".join(parts)[:MAX_CONTEXT_CHARS]


_SYSTEM = """You are a senior engineer debriefing a colleague who has just finished a piece of work.

Your job is NOT to praise, summarise, or restate what they told you. It is to find what is
missing before it gets written into a shared knowledge base that other people and other
agents will rely on.

Ask about, in rough priority:
- Measurements. "It's faster" is not a finding; "18.2s to 4.1s on a 40k-token input" is.
  If they claim an outcome, ask what they measured and how.
- What was tried and abandoned, and why. That is the part nobody writes down and everybody
  re-derives.
- The failure mode the next person will hit. What looks obvious but is wrong here?
- Contradictions with what is already in the vault.

Rules:
- At most {max_q} questions per round. Fewer is better if fewer will do.
- Ask what you cannot infer. Never ask something the context already answers.
- No preamble, no "great work". Questions only.
- If you have enough to write a genuinely useful note, say exactly: ENOUGH
"""

_WRITE = """Write the knowledge-base note now, from this debrief.

Format:
TITLE: <a specific claim or finding, not a topic label>
SUMMARY: <one line someone can scan>
BODY: <markdown. Lead with what was measured or decided and the evidence for it. Include
what was tried and rejected. Call out the failure mode the next person will hit. Reference
related notes as [[Wiki Links]] — a link to a note that does not exist yet is useful, it
marks where research should go next.>

Write what is supported by the debrief. Do not invent measurements. If something important
was never answered, say so in the body rather than papering over it.
"""


def _ask_model(
    config_store: Any,
    storage: Any,
    turns: list[Turn],
    *,
    attempts: int = 2,
    max_tokens: int = QUESTION_TOKENS,
    min_chars: int = 0,
    sentinel: str = "",
    require: str = "",
    alias: str = "",
) -> tuple[str, str]:
    """One call to a utility model, as ``(text, error)``.

    Defaults to the reviewer; ``alias`` overrides it, so the planner can use a
    different model without duplicating the retry, truncation and marker rules
    below — every one of which was earned by a specific failure in production
    and would have to be rediscovered in a copy.

    The two failures are reported apart on purpose.  "No reviewer configured"
    is fixed in settings; "the call failed" is usually transient and fixed by
    retrying.  Collapsing both into one message sent a debugging session after
    configuration that was correct the whole time — the first call after a
    restart had simply failed.

    A reply is rejected when it stops on ``length`` or falls under
    ``min_chars``, because both mean the same thing: the model ran out of
    room before it finished saying anything usable.  Measured against
    deepseek-v4-pro on this module's own prompt, four of eight calls at 1_200
    tokens stopped on ``length``, two of them returning NO content and two
    returning fragments (85 and 189 chars — one arrived as literally
    ``"1. What test"``).  Accepting that asks the engineer half a question,
    then distils a note from the answer to it.

    ``sentinel`` exempts a protocol word from the length floor, so a
    deliberately terse control reply (``ENOUGH``) is not mistaken for a
    truncated one and retried into a fresh round of questions.

    ``require`` is a marker the reply must contain to count as an answer —
    the write step asks for ``TITLE:``.  A model handed a finished transcript
    sometimes replies ABOUT the debrief instead of producing the note, and
    that reply is fluent, long, and passes every other check.

    When every attempt truncates, the longest reply that still clears
    ``min_chars`` is returned rather than an error: a cut-off note beats no
    note, and this module's whole contract is that a debrief ends in writing.
    A reply that never produced ``require`` is not eligible — it is not a
    short note, it is not a note.
    """
    alias = alias or _reviewer_alias(config_store)
    if not alias:
        return "", "no reviewer model configured"
    try:
        from pebble.core.model_registry import load_model_registry
        from pebble.core.model_turn import model_turn, resolve_lane

        registry = load_model_registry(storage=storage)
        if not registry.has_alias(alias):
            log.warning("interview.unknown_alias", alias=alias)
            return "", f"model alias {alias!r} is not registered"
        client, model, _cfg = registry.resolve(alias)
        lane = resolve_lane(
            registry.get_provider(alias),
            client,
            model,
            alias=alias,
            registry=registry,
            config_store=config_store,
        )
        # One retry on an empty reply.  Observed in practice: the same model
        # and prompt returns content on one call and nothing on the next, and
        # an interview that dies on a blank response wastes the round trip
        # AND the operator's attention on something a retry fixes.
        best = ""
        budget = max_tokens
        for attempt in range(max(1, attempts)):
            result = model_turn(lane, turns, max_tokens=budget)
            text = (result.content or "").strip()
            cut = getattr(result, "finish_reason", "") == "length"
            if cut:
                # Retrying a truncation at the SAME ceiling mostly truncates
                # again: the cause is that this prompt makes the model reason
                # longer than the budget allows, and that does not change
                # between attempts.  Escalating is what actually recovers.
                #
                # Sizing by measurement rather than by taste: visible replies
                # run 800-1400 chars whatever the ceiling, so the headroom is
                # entirely reasoning and a bigger number costs nothing on the
                # calls that did not need it — output tokens are billed on
                # use, not on allowance.
                budget = min(budget * 2, MAX_ESCALATED_TOKENS)
            if text and (sentinel and sentinel in text.upper()[:40]):
                return text, ""
            usable = bool(text) and (not require or require in text)
            if usable and not cut and len(text) >= min_chars:
                return text, ""
            if usable and len(text) > len(best) and len(text) >= min_chars:
                best = text
            if attempt + 1 < attempts:
                log.info(
                    "interview.thin_reply_retrying",
                    alias=alias,
                    attempt=attempt + 1,
                    chars=len(text),
                    truncated=cut,
                    next_budget=budget,
                )
                time.sleep(1.0 * (attempt + 1))
        if best:
            log.warning("interview.using_truncated_reply", alias=alias, chars=len(best))
            return best, ""
        return "", f"{alias} returned nothing usable after {attempts} attempts"
    except Exception as exc:
        log.warning("interview.model_call_failed", exc_info=True)
        return "", f"{alias} call failed: {type(exc).__name__}: {exc}"[:200]


def _turns(system: str, transcript: list[dict[str, str]]) -> list[Turn]:
    """Build the Turn IR trajectory model_turn expects.

    The transcript is stored as plain role/content dicts — that is what a
    JSON column can hold and what a human reading the row wants — and lowered
    to Turn IR here, at the one place that calls a model.
    """
    from pebble.core.trajectory import Role, TextBlock, Turn

    roles = {"user": Role.USER, "assistant": Role.ASSISTANT, "system": Role.SYSTEM}
    out = [Turn(role=Role.SYSTEM, content=(TextBlock(system),))]
    for t in transcript:
        out.append(
            Turn(
                role=roles.get(t.get("role", "user"), Role.USER),
                content=(TextBlock(t.get("content", "")),),
            )
        )
    return out


def start(
    storage: Any,
    config_store: Any,
    *,
    user_id: str,
    repo: str,
    topic: str,
    context: str,
) -> dict[str, Any]:
    """Open an interview and return the first questions."""
    if not (topic or "").strip():
        return {"ok": False, "error": "topic is required"}
    interview_id = uuid.uuid4().hex
    known = gather_context(repo, topic)
    opening = (
        f"Repo: {repo or '(unspecified)'}\nTopic: {topic}\n\n"
        f"What the engineer reports:\n{(context or '(nothing supplied)')[:MAX_ANSWER_CHARS]}\n\n"
        f"What pebble already knows:\n{known or '(nothing)'}"
    )
    transcript = [{"role": "user", "content": opening}]
    reply, err = _ask_model(
        config_store,
        storage,
        _turns(_SYSTEM.format(max_q=MAX_QUESTIONS), transcript),
        min_chars=MIN_QUESTION_CHARS,
    )
    if not reply:
        return {
            "ok": False,
            "error": err or "the reviewer model returned nothing",
            "retryable": "not registered" not in err and "no reviewer" not in err,
        }
    transcript.append({"role": "assistant", "content": reply})
    storage.create_interview(interview_id, user_id=user_id, repo=repo, topic=topic)
    storage.update_interview(interview_id, transcript=json.dumps(transcript), rounds=1)
    return {
        "ok": True,
        "interview_id": interview_id,
        "questions": reply,
        "budget": Budget(1, MAX_ROUNDS, MAX_ANSWER_CHARS).as_dict(),
    }


def answer(
    storage: Any, config_store: Any, *, interview_id: str, answers: str, user_id: str = ""
) -> dict[str, Any]:
    """Answer the outstanding questions; get more, or the finished note."""
    row = storage.get_interview(interview_id)
    if row is None:
        return {"ok": False, "error": "unknown interview_id"}
    if row["state"] != "open":
        return {
            "ok": False,
            "error": f"this interview is already {row['state']}",
            "note_title": row["note_title"],
        }
    if user_id and row["user_id"] and user_id != row["user_id"]:
        # Interviews are per-user: another caller answering someone else's
        # debrief would attribute their words to the wrong person.
        return {"ok": False, "error": "this interview belongs to another user"}

    try:
        transcript = json.loads(row["transcript"])
    except (TypeError, ValueError):
        transcript = []
    clipped = (answers or "")[:MAX_ANSWER_CHARS]
    truncated = len(answers or "") > MAX_ANSWER_CHARS
    transcript.append({"role": "user", "content": clipped})
    rounds = row["rounds"]

    # Out of budget: write from what is here.  Deliberately not an error —
    # ending with a note is what makes stretching pointless.
    if rounds >= MAX_ROUNDS:
        return _write_note(storage, config_store, row, transcript, rounds, truncated)

    reply, _err = _ask_model(
        config_store,
        storage,
        _turns(_SYSTEM.format(max_q=MAX_QUESTIONS), transcript),
        min_chars=MIN_QUESTION_CHARS,
        sentinel="ENOUGH",
    )
    if not reply:
        # Questioning failed; go straight to writing rather than stranding an
        # open interview nobody can close.
        return _write_note(storage, config_store, row, transcript, rounds, truncated)
    if "ENOUGH" in reply.upper()[:40]:
        return _write_note(storage, config_store, row, transcript, rounds, truncated)

    transcript.append({"role": "assistant", "content": reply})
    rounds += 1
    storage.update_interview(interview_id, transcript=json.dumps(transcript), rounds=rounds)
    return {
        "ok": True,
        "interview_id": interview_id,
        "questions": reply,
        "answer_truncated": truncated,
        "budget": Budget(rounds, MAX_ROUNDS, MAX_ANSWER_CHARS).as_dict(),
    }


def _write_note(
    storage: Any,
    config_store: Any,
    row: dict[str, Any],
    transcript: list[dict[str, str]],
    rounds: int,
    truncated: bool,
) -> dict[str, Any]:
    """Distil the debrief into a note and file it in the vault."""
    # The instruction is repeated as a final USER turn, not left in the system
    # prompt alone.  Swapping the system prompt under a transcript that ends
    # mid-conversation got a conversational reply instead of a note — an
    # actual filed note read "The conversation appears complete. ... let me
    # know."  The model answers the last thing it was asked, so the last thing
    # it is asked has to be the write instruction.
    text, err = _ask_model(
        config_store,
        storage,
        _turns(_WRITE, [*transcript, {"role": "user", "content": _WRITE}]),
        # The last call is the one that must not be lost: everything said so
        # far is only worth something if a note comes out of it.  A note also
        # needs more room than a couple of questions.
        attempts=4,
        max_tokens=NOTE_TOKENS,
        min_chars=MIN_NOTE_CHARS,
        require="TITLE:",
    )
    if not text:
        storage.update_interview(
            row["interview_id"], transcript=json.dumps(transcript), rounds=rounds, state="open"
        )
        return {
            "ok": False,
            "error": f"could not write the note: {err or 'the reviewer model returned nothing'}",
            "retryable": True,
        }

    title, summary, body = _parse_note(text, fallback_title=row["topic"])
    try:
        from pebble.core.knowledge import Note, extract_links, write_note

        note = Note(
            title=title,
            body=body,
            kind="finding",
            summary=summary,
            repo_id=row["repo"],
            ws_id=f"interview:{row['user_id'] or 'unknown'}",
            links=extract_links(body),
        )
        write_note(note)
    except Exception as exc:
        log.warning("interview.write_failed", exc_info=True)
        return {"ok": False, "error": f"could not write the note: {exc}"}

    storage.update_interview(
        row["interview_id"],
        transcript=json.dumps(transcript),
        rounds=rounds,
        state="written",
        note_title=title,
    )
    try:
        from pebble.core.knowledge import sync_index

        sync_index(storage)
    except Exception:
        log.debug("interview.index_sync_failed", exc_info=True)
    return {
        "ok": True,
        "written": True,
        "interview_id": row["interview_id"],
        "title": title,
        "summary": summary,
        "answer_truncated": truncated,
        "rounds_used": rounds,
    }


def _parse_note(text: str, *, fallback_title: str) -> tuple[str, str, str]:
    """Pull TITLE / SUMMARY / BODY out of the model's reply.

    Tolerant on purpose: a note that lands with a weaker title beats losing a
    whole debrief because a header was formatted differently.
    """
    title, summary, body_lines = "", "", []
    in_body = False
    for line in (text or "").splitlines():
        upper = line.strip().upper()
        if not in_body and upper.startswith("TITLE:"):
            title = line.split(":", 1)[1].strip()
        elif not in_body and upper.startswith("SUMMARY:"):
            summary = line.split(":", 1)[1].strip()
        elif upper.startswith("BODY:"):
            in_body = True
            rest = line.split(":", 1)[1].strip()
            if rest:
                body_lines.append(rest)
        elif in_body:
            body_lines.append(line)
    body = "\n".join(body_lines).strip()
    if not body:
        # No BODY header at all — keep the whole reply rather than filing an
        # empty note.
        body = (text or "").strip()
    return (title or fallback_title or "Untitled finding")[:120], summary[:300], body
