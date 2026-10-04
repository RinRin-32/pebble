"""The HTTP edge API: what each route does once it is allowed to.

Authority is pinned in ``test_edge_api_scopes.py``. These tests are about
behaviour, and about the contract the routes inherit from the core functions
they wrap: truncation is reported, pulls are not usage, globals are not
published from a device, and failures never come back success-shaped.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from pebble.core import edge_api, kb_mcp
from pebble.core import skill_publish as sp
from pebble.core import skill_transfer as st
from pebble.core.auth import AuthResult, parse_scopes
from tests._edge_test_helpers import EdgeStore, call, injected_client, skill

if TYPE_CHECKING:
    from pathlib import Path

    from starlette.testclient import TestClient


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EdgeStore:
    monkeypatch.setenv("PEBBLE_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("PEBBLE_PUBLIC_URL", raising=False)
    (tmp_path / "kb").mkdir()
    monkeypatch.setattr(kb_mcp, "_sync_index", lambda: None)
    monkeypatch.setattr(
        sp, "policy_check", lambda *a, **k: {"ok": True, "verdict": "allow", "reason": "fine"}
    )
    s = EdgeStore()
    s.caps = {"pub": ["skill_publish"]}
    return s


def _client(store: EdgeStore, user: str = "pub", scopes: str = "write") -> TestClient:
    auth = AuthResult(user_id=user, scopes=parse_scopes(scopes), token_source="database")
    return injected_client(store, auth)


def _post(client: TestClient, name: str, body: Any) -> Any:
    return client.post(f"/v1{edge_api.OPERATIONS[name].path}", json=body)


class TestPull:
    def test_truncation_is_reported_not_silent(self, store: EdgeStore) -> None:
        # An edge that quietly received half its skills would look like a
        # skill that does not work. The names that were left out must come
        # back, so the edge can ask for them.
        store.rows = [skill(f"s{i}", "repo-a", tokens=400) for i in range(5)]
        out = call(_client(store), "skills.pull", {"repo": "repo-a", "max_tokens": 1000}).json()
        assert out["ok"] is True
        assert out["count"] == 2
        assert out["truncated"] == ["s2", "s3", "s4"]
        assert out["token_budget"] == 1000

    def test_named_skills_survive_the_budget_and_misses_are_named(self, store: EdgeStore) -> None:
        store.rows = [skill("big", "repo-a", tokens=5000), skill("small", "repo-a", tokens=10)]
        out = call(
            _client(store),
            "skills.pull",
            {"repo": "repo-a", "names": ["big", "ghost"], "max_tokens": 100},
        ).json()
        assert [s["name"] for s in out["skills"]][0] == "big"
        assert out["not_found"] == ["ghost"]

    def test_scope_shadowing_and_archive_come_from_build_bundle(self, store: EdgeStore) -> None:
        store.rows = [
            skill("review", "", content="generic"),
            skill("review", "repo-a", content="tuned"),
            skill("old", "repo-a", archived=1),
            skill("theirs", "repo-b"),
        ]
        out = call(_client(store), "skills.pull", {"repo": "repo-a"}).json()
        assert [(s["name"], s["content"]) for s in out["skills"]] == [("review", "tuned")]

    def test_a_pull_is_recorded_as_pulled_never_invoked(self, store: EdgeStore) -> None:
        store.rows = [skill("deploy", "repo-a")]
        call(_client(store), "skills.pull", {"repo": "repo-a"})
        assert [e["event"] for e in store.events] == ["pulled"]

    def test_an_edge_cannot_raise_the_budget_past_the_default(self, store: EdgeStore) -> None:
        out = call(_client(store), "skills.pull", {"max_tokens": 10**9}).json()
        assert out["token_budget"] == st.DEFAULT_BUNDLE_TOKENS

    @pytest.mark.parametrize("bad", [0, -5, "lots", True])  # type: ignore[misc]
    def test_a_bad_budget_is_a_400_naming_the_field(self, store: EdgeStore, bad: Any) -> None:
        resp = call(_client(store), "skills.pull", {"max_tokens": bad})
        assert resp.status_code == 400
        assert "max_tokens" in resp.json()["error"]

    def test_names_must_be_a_list_of_strings(self, store: EdgeStore) -> None:
        resp = call(_client(store), "skills.pull", {"names": "deploy"})
        assert resp.status_code == 400 and "names" in resp.json()["error"]

    def test_an_unreadable_store_is_not_success_shaped(self, store: EdgeStore) -> None:
        def boom(*_a: Any, **_k: Any) -> list[Any]:
            raise RuntimeError("db down")

        store.list_prompt_templates = boom  # type: ignore[method-assign]
        resp = call(_client(store), "skills.pull", {"repo": "repo-a"})
        assert resp.status_code == 503 and resp.json()["ok"] is False


class TestPublish:
    def test_a_repo_skill_is_published_with_attribution(self, store: EdgeStore) -> None:
        resp = call(
            _client(store),
            "skills.publish",
            {"name": "build", "body": "# Build\n\nRun make.", "repo": "repo-a", "paths": ["*.py"]},
        )
        assert resp.status_code == 200
        created = store.created[0]
        assert created["repo_id"] == "repo-a"
        assert created["origin"] == "edge"
        assert created["created_by"] == "pub"

    def test_asking_for_a_global_is_refused_explicitly(self, store: EdgeStore) -> None:
        # Refused, not quietly scoped to the repo: a caller who asked for a
        # global and got a repo skill would believe every machine has it.
        resp = call(
            _client(store),
            "skills.publish",
            {"name": "build", "body": "# b", "repo": "repo-a", "global": True},
        )
        assert resp.status_code == 400
        assert "GLOBAL" in resp.json()["error"]
        assert not store.created

    def test_no_repo_is_publishs_own_refusal(self, store: EdgeStore) -> None:
        resp = call(_client(store), "skills.publish", {"name": "build", "body": "# b"})
        assert resp.status_code == 400
        assert "repo is required" in resp.json()["error"]
        assert not store.created

    def test_a_policy_refusal_is_a_422_with_the_reason(
        self, store: EdgeStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            sp,
            "policy_check",
            lambda *a, **k: {"ok": False, "verdict": "refuse", "reason": "exfil"},
        )
        resp = call(
            _client(store), "skills.publish", {"name": "x", "body": "# x", "repo": "repo-a"}
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["refused_by"] == "policy" and body["error"] == "exfil"
        assert not store.created


class TestHook:
    def test_it_mints_a_report_only_token(self, store: EdgeStore) -> None:
        resp = call(_client(store), "skills.hook", {"report_url": "https://pebble.example/"})
        assert resp.status_code == 200
        out = resp.json()
        assert store.minted[0]["scopes"] == "skills.report"
        assert store.minted[0]["user_id"] == "pub"
        command = out["settings_json"]["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        assert "https://pebble.example/v1/api/skills/report" in command
        assert out["expires_hours"] == st.REPORT_TOKEN_HOURS

    def test_no_url_and_no_default_is_refused_not_guessed(self, store: EdgeStore) -> None:
        resp = call(_client(store), "skills.hook", {})
        assert resp.status_code == 400
        assert "report_url" in resp.json()["error"]
        assert not store.minted

    def test_the_token_is_never_logged(
        self, store: EdgeStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []

        class _Log:
            def __getattr__(self, _level: str) -> Any:
                return lambda *a, **k: seen.append(repr((a, k)))

        monkeypatch.setattr(st, "log", _Log())
        monkeypatch.setattr(edge_api, "log", _Log())
        out = call(_client(store), "skills.hook", {"report_url": "https://p.example"}).json()
        command = out["settings_json"]["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        raw = command.split("Bearer ", 1)[1].split("'", 1)[0]
        assert raw.startswith("ts_")
        assert seen and not any(raw in line for line in seen)


class TestVault:
    def test_write_then_read_then_search(self, store: EdgeStore) -> None:
        client = _client(store)
        wrote = call(
            client, "kb.write", {"title": "Edge Note", "body": "see [[Other]]", "repo": "r"}
        )
        assert wrote.status_code == 200 and wrote.json()["links"] == ["Other"]
        read = call(client, "kb.read", {"title": "Edge Note"}).json()
        assert read["ok"] is True and read["body"].strip() == "see [[Other]]"
        found = call(client, "kb.search", {"query": "Edge", "repo": "r"}).json()
        assert found["ok"] is True
        assert [r["title"] for r in found["results"]] == ["Edge Note"]

    def test_writes_say_which_surface_and_user_wrote_them(self, store: EdgeStore) -> None:
        from pebble.core.knowledge import read_note

        call(_client(store), "kb.write", {"title": "Attributed", "body": "x"})
        note = read_note("Attributed")
        assert note is not None and note.ws_id == "edge:pub"

    def test_a_missing_note_is_a_404_not_an_empty_success(self, store: EdgeStore) -> None:
        resp = call(_client(store), "kb.read", {"title": "Nope"})
        assert resp.status_code == 404
        assert resp.json()["ok"] is False and resp.json()["found"] is False

    def test_write_validation_comes_back_as_an_error(self, store: EdgeStore) -> None:
        resp = call(_client(store), "kb.write", {"title": "  ", "body": "x"})
        assert resp.status_code == 400 and resp.json()["ok"] is False

    def test_an_experiment_is_recorded_not_run(self, store: EdgeStore) -> None:
        from pebble.core.knowledge import read_note

        resp = call(
            _client(store),
            "kb.experiment",
            {"title": "Speed", "command": "make bench", "exit_code": 2, "output": "slow"},
        )
        assert resp.status_code == 200 and resp.json()["verdict"] == "exit 2"
        note = read_note("Speed")
        assert note is not None and note.kind == "experiment" and note.ws_id == "edge:pub"

    def test_an_experiment_without_an_exit_code_is_refused(self, store: EdgeStore) -> None:
        # Defaulting to 0 would record a pass nobody measured.
        resp = call(_client(store), "kb.experiment", {"title": "Speed", "command": "make"})
        assert resp.status_code == 400 and "exit_code" in resp.json()["error"]


class TestCapabilities:
    def test_a_reader_is_told_what_it_cannot_do_and_why(self, store: EdgeStore) -> None:
        out = call(_client(store, "reader", "read"), "capabilities").json()
        ops = {o["name"]: o for o in out["operations"]}
        assert out["user_id"] == "reader" and out["scopes"] == ["read"]
        assert ops["kb.search"]["available"] is True
        assert ops["kb.write"]["available"] is False
        assert ops["kb.write"]["missing"] == ["write"]
        assert ops["skills.publish"]["missing"] == ["write", "skill_publish"]
        assert ops["skills.publish"]["path"] == "/v1/api/edge/skills/publish"

    def test_a_publisher_sees_everything_available(self, store: EdgeStore) -> None:
        store.caps["pub"] = ["skill_publish", "full_access"]
        out = call(_client(store), "capabilities").json()
        assert out["capabilities"] == ["full_access", "skill_publish"]
        assert all(o["available"] for o in out["operations"])

    def test_session_arming_is_unavailable_without_its_capability(self, store: EdgeStore) -> None:
        # 'write' + skill_publish is not enough: arming switches a session's
        # approval gate off, and the UI must not offer a button that 403s.
        out = call(_client(store), "capabilities").json()
        ops = {o["name"]: o for o in out["operations"]}
        for name in (
            "sessions.arm-full-access",
            "sessions.disarm-full-access",
            "sessions.full-access-status",
        ):
            assert ops[name]["available"] is False
            assert ops[name]["missing"] == ["full_access"]
        assert out["full_access"]["can_arm"] is False


class TestFailuresAreExplicit:
    def test_an_exception_is_a_500_naming_the_operation(
        self, store: EdgeStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: Any, **_k: Any) -> dict[str, Any]:
            raise RuntimeError("secret detail")

        monkeypatch.setattr(kb_mcp, "search_vault", boom)
        resp = call(_client(store), "kb.search", {"query": "x"})
        assert resp.status_code == 500
        body = resp.json()
        assert body["ok"] is False and "kb.search" in body["error"]
        assert "secret detail" not in body["error"]

    def test_a_non_json_body_is_a_400(self, store: EdgeStore) -> None:
        resp = _client(store).post(
            "/v1/api/edge/kb/search",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400 and resp.json()["ok"] is False

    def test_a_json_array_body_is_a_400(self, store: EdgeStore) -> None:
        resp = _post(_client(store), "kb.search", ["query"])
        assert resp.status_code == 400

    def test_no_storage_is_a_503(self, store: EdgeStore) -> None:
        client = _client(store)
        client.app.state.auth_storage = None  # type: ignore[attr-defined]
        resp = call(client, "kb.search", {"query": "x"})
        assert resp.status_code == 503 and resp.json()["ok"] is False


class TestFullAccess:
    """Sediment arms its own session so it runs unattended, and can stop it."""

    @pytest.fixture
    def armable(self, store: EdgeStore) -> EdgeStore:
        store.caps["pub"] = ["full_access"]
        store.add_workstream("ws-pub", "pub")
        return store

    def test_arm_then_disarm_round_trips_through_storage(self, armable: EdgeStore) -> None:
        client = _client(armable)
        out = call(client, "sessions.arm-full-access", {"ws_id": "ws-pub"}).json()
        assert out["ok"] is True and out["armed"] is True and out["changed_by"] == "pub"
        assert out["budget_override_prompts"] is True
        assert armable.configs["ws-pub"]["full_access"] == "1"

        status = call(client, "sessions.full-access-status", {"ws_id": "ws-pub"}).json()
        assert status["armed"] is True and status["can_arm"] is True

        out = call(client, "sessions.disarm-full-access", {"ws_id": "ws-pub"}).json()
        assert out["ok"] is True and out["armed"] is False
        assert armable.configs["ws-pub"]["full_access"] == "0"

    def test_capabilities_reports_the_named_sessions_armed_state(self, armable: EdgeStore) -> None:
        client = _client(armable)
        before = client.get("/v1/api/edge/capabilities?ws_id=ws-pub").json()["full_access"]
        assert before["can_arm"] is True and before["session"]["armed"] is False
        call(client, "sessions.arm-full-access", {"ws_id": "ws-pub"})
        after = client.get("/v1/api/edge/capabilities?ws_id=ws-pub").json()["full_access"]
        assert after["session"]["armed"] is True
        # Without ?ws_id there is no per-session read at all.
        assert call(client, "capabilities").json()["full_access"]["session"] is None

    def test_audit_names_the_actor_and_never_the_token(self, armable: EdgeStore) -> None:
        from tests._edge_test_helpers import real_client

        token = armable.add_token("pub", "write")
        client = real_client(armable)
        for name in ("sessions.arm-full-access", "sessions.disarm-full-access"):
            assert call(client, name, {"ws_id": "ws-pub"}, token).status_code == 200
        actions = [a["action"] for a in armable.audits]
        assert actions == ["workstream.full_access.arm", "workstream.full_access.disarm"]
        assert all(a["user_id"] == "pub" and a["resource_id"] == "ws-pub" for a in armable.audits)
        assert '"surface": "edge"' in armable.audits[0]["detail"]
        assert token not in repr(armable.audits)

    def test_revoking_the_capability_suspends_an_armed_session(self, armable: EdgeStore) -> None:
        from pebble.core import full_access as fa

        call(_client(armable), "sessions.arm-full-access", {"ws_id": "ws-pub"})
        assert fa.is_armed(armable, "ws-pub") is True
        armable.caps["pub"] = []
        # The gate stops at once; the row still says armed, reported as such.
        assert fa.is_armed(armable, "ws-pub") is False
        status = fa.read_status(armable, "ws-pub")
        assert status["armed"] is False and status["suspended"] is True

    def test_a_failed_disarm_is_a_loud_503_not_an_ok(self, armable: EdgeStore) -> None:
        client = _client(armable)
        call(client, "sessions.arm-full-access", {"ws_id": "ws-pub"})

        def refuse(_ws_id: str, _config: dict[str, str]) -> None:
            raise RuntimeError("db down")

        armable.save_workstream_config = refuse  # type: ignore[method-assign]
        resp = call(client, "sessions.disarm-full-access", {"ws_id": "ws-pub"})
        assert resp.status_code == 503
        assert "NOT recorded" in resp.json()["error"]
        # Not audited as a change that did not happen.
        assert [a["action"] for a in armable.audits] == ["workstream.full_access.arm"]

    def test_a_missing_ws_id_is_a_400(self, armable: EdgeStore) -> None:
        resp = call(_client(armable), "sessions.arm-full-access", {})
        assert resp.status_code == 400 and "ws_id" in resp.json()["error"]
