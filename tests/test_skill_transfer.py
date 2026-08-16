"""Shipping skills to an edge device, and hearing back about them.

Two things here are load-bearing and everything else is detail.

**Pulls are not usage.** A skill shipped in every bundle and never invoked
must not look popular, or the janitor deletes the rare-but-critical one and
keeps the useless one.

**The hook token must buy nothing.** It is minted into a settings.json on a
laptop pebble does not control. If it can read the vault, the telemetry
channel has become a lateral movement path.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from pebble.core import skill_transfer as st


class _Store:
    """Just the accessors build_bundle and record_invocation touch."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.events: list[dict[str, Any]] = []
        self.tokens: list[dict[str, Any]] = []

    def list_prompt_templates(self, org_id: str = "", limit: int = 0, offset: int = 0):
        return list(self.rows)

    def get_prompt_template_by_name(self, name: str, repo_id: str = ""):
        for scope in [repo_id, ""] if repo_id else [""]:
            for r in self.rows:
                if r["name"] == name and r.get("repo_id", "") == scope:
                    return r
        return None

    def record_skill_event(self, event_id: str, **kw: Any) -> None:
        self.events.append({"event_id": event_id, **kw})

    def create_api_token(self, **kw: Any) -> None:
        self.tokens.append(kw)


def _skill(name: str, repo: str = "", tokens: int = 100, **over: Any) -> dict[str, Any]:
    row = {
        "template_id": f"id-{name}-{repo or 'global'}",
        "name": name,
        "repo_id": repo,
        "content": f"# {name}",
        "description": "",
        "version": "1.0.0",
        "allowed_tools": '["Bash"]',
        "paths": '["**/*.py"]',
        "tags": "[]",
        "activation": "named",
        "token_estimate": tokens,
        "enabled": 1,
    }
    row.update(over)
    return row


class TestScopeSelection:
    def test_a_repo_gets_its_own_skills_and_globals(self) -> None:
        store = _Store([_skill("deploy", "repo-a"), _skill("review"), _skill("other", "repo-b")])
        out = st.build_bundle(store, user_id="u1", repo="repo-a")
        assert {s["name"] for s in out["skills"]} == {"deploy", "review"}

    def test_a_repos_skill_shadows_a_global_of_the_same_name(self) -> None:
        # Same resolution rule the single lookup uses, applied to a set: a
        # project that tuned a skill meant it.
        store = _Store(
            [_skill("review", "", content="generic"), _skill("review", "repo-a", content="tuned")]
        )
        out = st.build_bundle(store, user_id="u1", repo="repo-a")
        assert len(out["skills"]) == 1
        assert out["skills"][0]["content"] == "tuned"

    def test_disabled_skills_are_not_shipped(self) -> None:
        store = _Store([_skill("off", "repo-a", enabled=0), _skill("on", "repo-a")])
        out = st.build_bundle(store, user_id="u1", repo="repo-a")
        assert [s["name"] for s in out["skills"]] == ["on"]

    def test_paths_globs_are_shipped_not_applied(self) -> None:
        """Pebble must not evaluate globs against a tree it cannot see.

        The edge owns its working directory; pebble depending on it would be
        depending on something that drifts between requests and that pebble
        has no way to verify.
        """
        store = _Store([_skill("deploy", "repo-a")])
        out = st.build_bundle(store, user_id="u1", repo="repo-a")
        assert out["skills"][0]["paths"] == ["**/*.py"]


class TestNamedRequests:
    def test_a_named_skill_survives_the_budget(self) -> None:
        # Asking for one by name is deliberate; dropping it silently would
        # look like the skill is broken.
        store = _Store(
            [_skill("wanted", "repo-a", tokens=9999), _skill("filler", "repo-a", tokens=10)]
        )
        out = st.build_bundle(store, user_id="u1", repo="repo-a", names=["wanted"], max_tokens=50)
        assert "wanted" in [s["name"] for s in out["skills"]]

    def test_a_name_that_does_not_exist_is_reported(self) -> None:
        store = _Store([_skill("real", "repo-a")])
        out = st.build_bundle(store, user_id="u1", repo="repo-a", names=["ghost"])
        assert out["not_found"] == ["ghost"]

    def test_an_out_of_scope_name_is_not_smuggled_in(self) -> None:
        # Naming a skill must not defeat scoping — otherwise the namespace
        # work is decorative.
        store = _Store([_skill("secret", "repo-b")])
        out = st.build_bundle(store, user_id="u1", repo="repo-a", names=["secret"])
        assert out["skills"] == [] and out["not_found"] == ["secret"]


class TestBudget:
    def test_truncation_names_what_was_dropped(self) -> None:
        # A bundle that quietly arrived half-size looks exactly like a skill
        # that does not work.
        store = _Store([_skill(f"s{i}", "repo-a", tokens=100) for i in range(10)])
        out = st.build_bundle(store, user_id="u1", repo="repo-a", max_tokens=250)
        assert out["truncated"], "dropped skills must be named"
        assert len(out["skills"]) + len(out["truncated"]) == 10

    def test_repo_skills_are_preferred_over_globals_when_budget_is_tight(self) -> None:
        store = _Store(
            [_skill("zzz-repo", "repo-a", tokens=100), _skill("aaa-global", "", tokens=100)]
        )
        out = st.build_bundle(store, user_id="u1", repo="repo-a", max_tokens=100)
        assert [s["name"] for s in out["skills"]] == ["zzz-repo"]


class TestPullsAreNotUsage:
    def test_a_pull_records_pulled_only(self) -> None:
        store = _Store([_skill("deploy", "repo-a")])
        st.build_bundle(store, user_id="u1", repo="repo-a")
        assert [e["event"] for e in store.events] == ["pulled"]

    def test_a_truncated_skill_is_not_recorded_as_pulled(self) -> None:
        # It was never sent, so counting it would inflate exactly the number
        # the janitor reads as "this gets shipped a lot".
        store = _Store([_skill("a", "repo-a", tokens=100), _skill("b", "repo-a", tokens=100)])
        st.build_bundle(store, user_id="u1", repo="repo-a", max_tokens=100)
        assert len(store.events) == 1

    def test_invocation_is_a_separate_event(self) -> None:
        store = _Store([_skill("deploy", "repo-a")])
        out = st.record_invocation(
            store, name="deploy", user_id="u1", session_id="s1", repo="repo-a"
        )
        assert out["ok"] and out["resolved"] is True
        assert store.events[-1]["event"] == "invoked"
        assert store.events[-1]["session_id"] == "s1"

    def test_an_unresolvable_invocation_is_still_recorded(self) -> None:
        """Dropping it would bias the janitor toward deleting exactly the
        skills whose rows have since changed."""
        store = _Store([])
        out = st.record_invocation(store, name="renamed-since", user_id="u1")
        assert out["ok"] and out["resolved"] is False
        assert store.events[-1]["event"] == "invoked"

    def test_telemetry_failure_never_costs_the_caller_their_skills(self) -> None:
        class _Broken(_Store):
            def record_skill_event(self, event_id: str, **kw: Any) -> None:
                raise RuntimeError("events table gone")

        store = _Broken([_skill("deploy", "repo-a")])
        out = st.build_bundle(store, user_id="u1", repo="repo-a")
        assert out["ok"] is True and len(out["skills"]) == 1


class TestReportToken:
    def test_it_is_minted_report_only_and_expiring(self) -> None:
        store = _Store()
        raw = st.mint_report_token(store, "u1")
        assert raw.startswith("ts_")
        row = store.tokens[-1]
        assert row["scopes"] == "skills.report"
        assert row["expires"], "a credential left on a laptop must expire"

    def test_the_raw_token_is_not_stored(self) -> None:
        store = _Store()
        raw = st.mint_report_token(store, "u1")
        assert raw not in json.dumps(store.tokens)


class TestReportScopeIsolation:
    """The property the whole design rests on.

    The hook config sits in a settings.json on a device pebble does not
    control. If this scope reached anything else, the telemetry channel would
    be a lateral movement path rather than a measurement.
    """

    @staticmethod
    def _report_token() -> Any:
        from pebble.core.auth import AuthResult, parse_scopes

        return AuthResult(
            user_id="u1", scopes=parse_scopes("skills.report"), token_source="api_token"
        )

    def test_it_can_reach_the_report_path(self) -> None:
        from pebble.core.auth import SKILL_REPORT_PATH, required_scope

        assert self._report_token().has_scope(required_scope("POST", f"/v1{SKILL_REPORT_PATH}"))

    @pytest.mark.parametrize(  # type: ignore[misc]
        "path",
        ["/v1/api/workstreams", "/mcp", "/v1/api/admin/users", "/v1/api/skills", "/v1/api/nodes"],
    )
    def test_it_can_reach_nothing_else(self, path: str) -> None:
        from pebble.core.auth import required_scope

        assert not self._report_token().has_scope(required_scope("GET", path))

    def test_an_ordinary_token_cannot_forge_a_report(self) -> None:
        # Usage data is only worth something if it comes from the hook.
        from pebble.core.auth import AuthResult, parse_scopes

        for grant in ("read", "write", "approve"):
            who = AuthResult(user_id="u1", scopes=parse_scopes(grant), token_source="api_token")
            assert not who.has_scope("skills.report"), grant

    def test_the_scope_does_not_expand_to_read(self) -> None:
        from pebble.core.auth import parse_scopes

        assert parse_scopes("skills.report") == frozenset({"skills.report"})


class TestHookConfig:
    def test_it_matches_the_skill_tool_after_use(self) -> None:
        cfg = st.hook_config("https://pebble.example/v1/api/skills/report", "ts_abc")
        entry = cfg["hooks"]["PostToolUse"][0]
        assert entry["matcher"] == "Skill"
        assert "curl" in entry["hooks"][0]["command"]

    def test_it_cannot_fail_the_session(self) -> None:
        # A hook that returns non-zero interrupts the thing it is measuring.
        cmd = st.hook_config("https://x/r", "ts_abc")["hooks"]["PostToolUse"][0]["hooks"][0][
            "command"
        ]
        assert cmd.rstrip().endswith("|| true")
        assert "-m 5" in cmd, "an unreachable pebble must not hang the session"

    def test_it_claims_invocation_not_outcome(self) -> None:
        # Nothing in the payload should imply we know whether it worked.
        cmd = st.hook_config("https://x/r", "ts_abc")["hooks"]["PostToolUse"][0]["hooks"][0][
            "command"
        ]
        for invented in ("success", "failed", "exit_code", "worked"):
            assert invented not in cmd
