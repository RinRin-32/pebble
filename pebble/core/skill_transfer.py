"""Shipping skills to an edge device, and hearing back about them.

A session on a laptop asks pebble for the skills that apply to the repository
it is working in.  Pebble answers with a BUNDLE — N skills verbatim, each
keeping its own name, version and ``allowed_tools`` — plus a hook the edge
installs so invocations come back as telemetry.

**Bundle, not synthesis.**  Composing several skills into one derived skill is
a different product with its own problems (whose instruction wins on conflict,
union or intersection of tool grants, provenance when a source changes).  It
is also much better informed once we know which skills are invoked *together*,
which is data this module produces.  So it comes after, not instead.

**Scope here, filter there.**  Pebble decides what a caller is ALLOWED to see:
the repo's skills plus globals.  The edge decides what applies, by evaluating
each skill's ``paths`` globs against its own working tree — the column the
schema has stored and never acted on.  Pebble does not ask for the file tree:
it would be taking a dependency on a directory it cannot see, should not
trust, and which drifts between requests.  Named requests bypass the glob
entirely, because deliberately asking for a skill should not be silently
overruled by a pattern.

**Pulls are not usage.**  Sending a skill is recorded as ``pulled`` and means
only that it was offered.  ``invoked`` comes from the hook.  Conflating them
would let the janitor delete a rare-but-critical skill for being unpopular,
and keep a useless one for being in every bundle.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pebble.core.log import get_logger

log = get_logger(__name__)

#: Ceiling on a bundle, in estimated tokens.  A repo's skills plus every
#: global one can be far more context than a session should spend on
#: capability it may not use.  Truncation is REPORTED, never silent — an edge
#: that quietly received half its skills would look like a skill that does not
#: work.
DEFAULT_BUNDLE_TOKENS = 30_000

#: How long a minted report token lives.  Long enough for a working session,
#: short enough that a hook config left in a settings.json on a laptop stops
#: being a credential by the next day.
REPORT_TOKEN_HOURS = 12

#: Cap on how many skills one bundle may carry regardless of token budget.
MAX_BUNDLE_SKILLS = 50


def mint_report_token(storage: Any, user_id: str, *, hours: int = REPORT_TOKEN_HOURS) -> str:
    """A short-lived credential that can ONLY report skill invocations.

    Minted here because the transfer is the one moment that knows an edge
    session is starting, so it is the only place that can scope a credential
    to that session's life.  The alternative — the operator pasting their
    long-lived token into a settings.json on every machine — puts an
    unrevocable credential at rest on a node pebble does not control, which is
    the same shape as a token ending up somewhere it cannot be taken back
    from.

    The ``skills.report`` scope expands to itself and nothing else, so the
    worst case for a leaked hook config is forged usage data rather than read
    access to the vault.
    """
    from pebble.core.auth import generate_token, hash_token, token_prefix

    raw = generate_token()
    expires = (datetime.now(UTC) + timedelta(hours=max(1, hours))).strftime("%Y-%m-%dT%H:%M:%S")
    storage.create_api_token(
        token_id=uuid.uuid4().hex,
        token_hash=hash_token(raw),
        token_prefix=token_prefix(raw),
        user_id=user_id,
        name=f"skill-report (expires {expires})",
        scopes="skills.report",
        expires=expires,
    )
    log.info("skill_transfer.report_token_minted", user_id=user_id, expires=expires)
    return raw


def hook_config(report_url: str, token: str) -> dict[str, Any]:
    """Claude Code hook that reports a skill invocation, as settings.json JSON.

    A hook rather than an MCP tool the session calls, because a tool call is
    voluntary: a model that is busy simply will not make it, and the resulting
    data would measure the model's conscientiousness rather than the skill's
    utility.  A ``PostToolUse`` hook on the Skill tool fires whether or not
    anybody remembered.

    Honest about its limit: this reports that a skill was INVOKED.  Whether it
    worked is not observable at a tool-call boundary, and no field here
    pretends otherwise.
    """
    command = (
        "curl -s -m 5 -X POST "
        f"'{report_url}' "
        f"-H 'Authorization: Bearer {token}' "
        "-H 'Content-Type: application/json' "
        '-d "{\\"name\\": \\"$CLAUDE_SKILL_NAME\\", '
        '\\"session_id\\": \\"$CLAUDE_SESSION_ID\\"}" >/dev/null 2>&1 || true'
    )
    return {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "Skill",
                    "hooks": [{"type": "command", "command": command}],
                }
            ]
        }
    }


def _row_to_skill(row: dict[str, Any]) -> dict[str, Any]:
    """Project a skill row into what an edge device needs to install it."""

    def _json(field: str) -> Any:
        try:
            return json.loads(row.get(field) or "[]")
        except Exception:
            return []

    return {
        "name": row.get("name", ""),
        "description": row.get("description", ""),
        "content": row.get("content", ""),
        "version": row.get("version", ""),
        "repo": row.get("repo_id", ""),
        "global": not row.get("repo_id"),
        "allowed_tools": _json("allowed_tools"),
        # Shipped so the EDGE can decide applicability against its own tree.
        "paths": _json("paths"),
        "tags": _json("tags"),
        "activation": row.get("activation", "named"),
        "token_estimate": int(row.get("token_estimate") or 0),
        "skill_id": row.get("template_id", ""),
    }


def build_bundle(
    storage: Any,
    *,
    user_id: str,
    repo: str = "",
    names: list[str] | None = None,
    max_tokens: int = DEFAULT_BUNDLE_TOKENS,
) -> dict[str, Any]:
    """Select the skills an edge device may install, and record the pulls.

    Named skills come first and are never dropped by the budget: asking for
    one by name is deliberate, and silently omitting it would look like the
    skill is broken.  Repo-scoped skills outrank globals, because a project
    that tuned a skill meant it.
    """
    wanted = [n.strip() for n in (names or []) if n and n.strip()]
    try:
        rows = storage.list_prompt_templates()
    except Exception:
        log.warning("skill_transfer.list_failed", exc_info=True)
        return {"ok": False, "error": "could not read skills"}

    in_scope = [
        r
        for r in rows
        if r.get("enabled", 1) and (not r.get("repo_id") or r.get("repo_id") == repo)
    ]
    # A repo's own skill shadows a global of the same name — the same
    # resolution rule get_prompt_template_by_name uses, applied to the set.
    by_name: dict[str, dict[str, Any]] = {}
    for row in in_scope:
        name = row.get("name", "")
        if name not in by_name or row.get("repo_id"):
            by_name[name] = row

    named = [by_name[n] for n in wanted if n in by_name]
    missing = [n for n in wanted if n not in by_name]
    rest = sorted(
        (r for r in by_name.values() if r.get("name") not in set(wanted)),
        # Repo-scoped first, then by name for a stable, explainable order.
        key=lambda r: (not r.get("repo_id"), r.get("name", "")),
    )

    bundle: list[dict[str, Any]] = []
    spent = 0
    truncated: list[str] = []
    for row in [*named, *rest]:
        skill = _row_to_skill(row)
        is_named = skill["name"] in set(wanted)
        if not is_named and (
            spent + skill["token_estimate"] > max_tokens or len(bundle) >= MAX_BUNDLE_SKILLS
        ):
            truncated.append(skill["name"])
            continue
        bundle.append(skill)
        spent += skill["token_estimate"]

    for skill in bundle:
        try:
            storage.record_skill_event(
                uuid.uuid4().hex,
                event="pulled",
                skill_id=skill["skill_id"],
                name=skill["name"],
                repo_id=skill["repo"],
                user_id=user_id,
            )
        except Exception:
            # Telemetry must never cost the caller their skills.
            log.warning("skill_transfer.pull_event_failed", exc_info=True)

    return {
        "ok": True,
        "repo": repo,
        "count": len(bundle),
        "skills": bundle,
        "token_estimate": spent,
        "token_budget": max_tokens,
        # Named separately from a bare bool: "which ones" is what makes this
        # actionable, and an edge that wants them can ask by name.
        "truncated": truncated,
        "not_found": missing,
    }


def record_invocation(
    storage: Any, *, name: str, user_id: str, session_id: str = "", repo: str = ""
) -> dict[str, Any]:
    """Record that an edge device actually ran a skill.

    The skill id is resolved when it can be, and the event is written either
    way: an invocation of something we cannot resolve is still evidence, and
    dropping it would quietly bias the janitor toward deleting exactly the
    skills whose rows have since changed.
    """
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "name is required"}
    skill_id = ""
    try:
        row = storage.get_prompt_template_by_name(name, repo)
        skill_id = (row or {}).get("template_id", "")
    except Exception:
        log.debug("skill_transfer.resolve_failed", exc_info=True)
    storage.record_skill_event(
        uuid.uuid4().hex,
        event="invoked",
        skill_id=skill_id,
        name=name,
        repo_id=repo,
        user_id=user_id,
        session_id=(session_id or "").strip()[:120],
    )
    return {"ok": True, "recorded": name, "resolved": bool(skill_id)}
