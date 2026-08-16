"""Finding what has stopped earning its place, and saying why.

The janitor never deletes.  It has exactly one destructive-looking power —
archiving a skill — and archiving is reversible by construction: the row, its
resources and its whole event history stay, it simply stops being shipped.

**Every finding carries its reasons.**  A cleanup tool that reports "12
candidates" and expects agreement is asking for trust it has not earned.  The
useful output is "this skill was pulled 14 times, invoked 0, and no note
mentions it" — which the operator can disagree with on the evidence rather
than on faith.

**Never-invoked is a weak signal, and that is the point.**  A skill nobody has
run may be useless, or may cover the rare case that matters exactly when it
happens.  So the strongest negative signal here is not silence but the absence
of evidence *for* it: no note in the vault refers to it, meaning nobody wrote
down anything they learned that this skill encodes.  A skill nothing supports
is a skill nobody learned anything from.

Note support is measured by searching the vault for the skill's name.  That is
a proxy for a real skill→note edge and is documented as one: it will miss a
note that discusses a skill without naming it, and it will over-count a skill
whose name is a common word.  Both failure modes push toward KEEPING things,
which is the right direction for a tool whose mistakes are unrecoverable in
one direction and merely annoying in the other.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pebble.core.log import get_logger

log = get_logger(__name__)

#: How long a skill must have existed before silence about it means anything.
#: A skill added yesterday has not been ignored; it has not had a chance yet.
MIN_AGE_DAYS = 30

#: How long an ARCHIVED skill must sit untouched before it is offered for
#: deletion.  Deliberately longer than the first window: the cost of a wrong
#: archive is an operator restoring it, and the cost of a wrong deletion is
#: work nobody can recover.
DELETE_AFTER_DAYS = 90


def _age_days(stamp: str) -> float:
    """Days since an ISO timestamp; a huge number when it cannot be read.

    Unparseable dates read as OLD rather than new, which sounds backwards
    until you notice the alternative: a row with a corrupt timestamp would
    otherwise be permanently immune to review, which is exactly how a mess
    becomes permanent.  Being *offered* for review is harmless — nothing is
    deleted without a human.
    """
    text = (stamp or "").strip()
    if not text:
        return 10_000.0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return (datetime.now(UTC) - parsed).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return 10_000.0


def _note_support(name: str, repo: str = "") -> int:
    """How many vault notes mention this skill by name.

    A proxy for a skill→note edge, not the edge itself.  See the module
    docstring for what it gets wrong and why the errors lean toward keeping.
    """
    try:
        from pebble.core.knowledge import search_notes

        return len(search_notes(name, limit=5, repo=repo))
    except Exception:
        log.debug("janitor.note_support_failed", exc_info=True)
        # Unknown support means "do not use this as a reason to remove".
        return 1


def analyze_skills(storage: Any, *, min_age_days: int = MIN_AGE_DAYS) -> dict[str, Any]:
    """Which skills have stopped earning their place, and on what evidence."""
    try:
        rows = storage.list_prompt_templates()
        usage = storage.skill_usage_all()
    except Exception:
        log.warning("janitor.read_failed", exc_info=True)
        return {"ok": False, "error": "could not read skills"}

    archive_candidates: list[dict[str, Any]] = []
    delete_candidates: list[dict[str, Any]] = []
    keep: list[dict[str, Any]] = []

    for row in rows:
        skill_id = row.get("template_id", "")
        name = row.get("name", "")
        stats = usage.get(skill_id) or {"pulled": 0, "invoked": 0, "invoked_sessions": 0}
        age = _age_days(row.get("created", ""))
        archived = (row.get("archived") or "").strip()
        entry = {
            "name": name,
            "skill_id": skill_id,
            "repo": row.get("repo_id", ""),
            "age_days": round(age, 1),
            "pulled": stats["pulled"],
            "invoked": stats["invoked"],
            "invoked_sessions": stats.get("invoked_sessions", 0),
            "note_support": _note_support(name, row.get("repo_id", "")),
            "archived": archived,
        }

        if archived:
            waited = _age_days(archived)
            entry["archived_days"] = round(waited, 1)
            if waited >= DELETE_AFTER_DAYS and stats["invoked"] == 0:
                entry["reasons"] = [
                    f"archived {int(waited)} days ago and never invoked since",
                    "nothing has asked for it while it was hidden",
                ]
                delete_candidates.append(entry)
            else:
                entry["reasons"] = [f"archived {int(waited)} days ago; review window not elapsed"]
                keep.append(entry)
            continue

        reasons: list[str] = []
        if age < min_age_days:
            entry["reasons"] = [f"only {int(age)} days old — not ignored, just new"]
            keep.append(entry)
            continue
        if stats["invoked"] == 0:
            reasons.append(f"never invoked in {int(age)} days")
        if stats["pulled"] == 0:
            reasons.append("never even shipped to a device")
        if entry["note_support"] == 0:
            # The strongest signal: not silence, but no evidence FOR it.
            reasons.append("no note in the vault mentions it")

        # Archive only when the evidence agrees from more than one direction.
        # Never-invoked alone would sweep away the rare-but-critical.
        if stats["invoked"] == 0 and entry["note_support"] == 0:
            entry["reasons"] = reasons
            archive_candidates.append(entry)
        else:
            entry["reasons"] = reasons or ["in use"]
            keep.append(entry)

    return {
        "ok": True,
        "archive_candidates": archive_candidates,
        "delete_candidates": delete_candidates,
        "keep": keep,
        "thresholds": {
            "min_age_days": min_age_days,
            "delete_after_days": DELETE_AFTER_DAYS,
            "rule": (
                "archive needs never-invoked AND no note support; "
                "never-invoked alone is not enough, because rare is not useless"
            ),
        },
    }


def analyze_vault() -> dict[str, Any]:
    """What is untidy in the knowledge vault.

    Reports, and proposes nothing automatic.  Orphans in particular are NOT
    treated as garbage: a note nobody linked yet is the normal state of a note
    written five minutes ago.  The frontier is not listed as a problem at all —
    dangling links are the research frontier by design, and a tool that
    "cleaned" them would be deleting the vault's most useful signal.
    """
    try:
        from pebble.core.knowledge import graph_summary, list_notes

        summary = graph_summary()
        notes = list_notes()
    except Exception:
        log.warning("janitor.vault_read_failed", exc_info=True)
        return {"ok": False, "error": "could not read the vault"}

    empty = [n.title for n in notes if len(n.body.strip()) < 80]
    untitled = [n.title for n in notes if not n.summary]
    return {
        "ok": True,
        "notes": summary["notes"],
        "orphans": summary["orphans"],
        "thin": empty[:20],
        "no_summary": untitled[:20],
        "note": (
            "Orphans and dangling links are NOT proposed for removal: an unlinked note "
            "is the normal state of a new one, and dangling links are the research "
            "frontier by design. Thin notes are worth a look, not an automatic sweep."
        ),
    }


def archive_skills(
    storage: Any, names: list[str], *, repo: str = "", by: str = "janitor", restore: bool = False
) -> dict[str, Any]:
    """Archive or restore named skills, reporting exactly what changed.

    Reports per name rather than a count, following the same contract the
    vault's delete/rename work pinned: a sweep that silently matched nothing
    reads identically to one that worked.
    """
    stamp = "" if restore else datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    done: list[str] = []
    missing: list[str] = []
    for raw in names or []:
        name = (raw or "").strip()
        if not name:
            continue
        try:
            row = storage.get_prompt_template_by_name(name, repo)
        except Exception:
            log.warning("janitor.lookup_failed", name=name, exc_info=True)
            row = None
        if not row:
            missing.append(name)
            continue
        try:
            changed = storage.set_skill_archived(
                row["template_id"], archived=stamp, by="" if restore else by
            )
        except Exception:
            log.warning("janitor.archive_failed", name=name, exc_info=True)
            missing.append(name)
            continue
        (done if changed else missing).append(name)
        if changed:
            try:
                storage.record_skill_event(
                    uuid.uuid4().hex,
                    event="restored" if restore else "archived",
                    skill_id=row["template_id"],
                    name=name,
                    repo_id=row.get("repo_id", ""),
                    user_id=by,
                )
            except Exception:
                log.debug("janitor.event_failed", exc_info=True)
    return {
        "ok": True,
        "restored" if restore else "archived": done,
        "not_found": missing,
    }


def deletion_review(storage: Any) -> dict[str, Any]:
    """The archived skills old enough to be worth deleting, with their reasons.

    This is the whole of the janitor's "delete" story: it produces a list for a
    human. Nothing here removes anything, because the first tool that deletes
    on a heuristic is the last one anybody trusts.
    """
    report = analyze_skills(storage)
    if not report.get("ok"):
        return report
    return {
        "ok": True,
        "candidates": report["delete_candidates"],
        "how_to_act": (
            "Nothing is deleted by this tool. Restore with kb_skills_archive(restore=true), "
            "or remove deliberately through the console."
        ),
        "cutoff_days": DELETE_AFTER_DAYS,
    }


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def stale_before(days: int) -> str:
    """Timestamp *days* in the past, for callers writing their own queries."""
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
