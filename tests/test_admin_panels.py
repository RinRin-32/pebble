"""Storage accessors behind the Coding Jobs and Knowledge admin panels.

Both are read from the DATABASE, not the vault or the filesystem, because the
console container does not mount /workspace — the derived index exists so the
graph is queryable from a process that cannot see the notes on disk.
"""

from __future__ import annotations

from typing import Any


def _note(backend: Any, note_id: str, title: str, **over: Any) -> dict[str, Any]:
    row = {
        "note_id": note_id,
        "title": title,
        "path": f"/kb/{note_id}.md",
        "kind": "experiment",
        "summary": "ok",
        "tags": "[]",
        "ws_id": "",
        "repo_id": over.pop("repo_id", "r1"),
        "created": "2026-08-14T00:00:00",
        "updated": "2026-08-14T00:00:00",
    }
    row.update(over)
    return row


class TestKbOverview:
    def test_counts_links_in_both_directions(self, backend: Any) -> None:
        backend.replace_kb_index(
            [_note(backend, "a", "Alpha"), _note(backend, "b", "Beta")],
            [{"from_note": "b", "to_title": "Alpha", "created": "t"}],
        )
        by_title = {n["title"]: n for n in backend.kb_overview()[0]["notes"]}
        assert by_title["Beta"]["links_out"] == 1
        assert by_title["Alpha"]["links_in"] == 1
        assert by_title["Alpha"]["links_out"] == 0

    def test_frontier_is_links_to_notes_that_do_not_exist(self, backend: Any) -> None:
        backend.replace_kb_index(
            [_note(backend, "a", "Alpha")],
            [{"from_note": "a", "to_title": "Unwritten", "created": "t"}],
        )
        frontier = backend.kb_overview()[0]["frontier"]
        # Something was named and never written up — that is the research
        # frontier, and it must not be silently dropped.
        assert len(frontier) == 1
        assert frontier[0]["title"] == "Unwritten"
        assert frontier[0]["count"] == 1

    def test_existing_targets_are_not_frontier(self, backend: Any) -> None:
        backend.replace_kb_index(
            [_note(backend, "a", "Alpha"), _note(backend, "b", "Beta")],
            [{"from_note": "a", "to_title": "Beta", "created": "t"}],
        )
        assert backend.kb_overview()[0]["frontier"] == []

    def test_empty_vault(self, backend: Any) -> None:
        backend.replace_kb_index([], [])
        assert backend.kb_overview()[0]["notes"] == []


class TestCodingJobs:
    def _ws(self, backend: Any, ws_id: str, **cfg: str) -> None:
        backend.register_workstream(ws_id=ws_id, name=f"job {ws_id}")
        if cfg:
            backend.save_workstream_config(ws_id, cfg)

    def test_only_repo_bound_workstreams_appear(self, backend: Any) -> None:
        self._ws(backend, "wsbound", repo_id="kokoro-go", nix_env="kokoro-go")
        self._ws(backend, "wsplain")  # ordinary chat, not coding work
        jobs = backend.coding_jobs()
        ids = {j["ws_id"] for j in jobs}
        assert "wsbound" in ids and "wsplain" not in ids

    def test_carries_repo_env_and_model(self, backend: Any) -> None:
        self._ws(
            backend,
            "wsfull",
            repo_id="kokoro-go",
            nix_env="go-dev",
            model_alias="deepseek-v4-or",
        )
        job = next(j for j in backend.coding_jobs() if j["ws_id"] == "wsfull")
        assert job["repo"] == "kokoro-go"
        assert job["env"] == "go-dev"
        assert job["model"] == "deepseek-v4-or"

    def test_none_bound(self, backend: Any) -> None:
        self._ws(backend, "wsnone")
        assert backend.coding_jobs() == []

    def test_limit_is_respected(self, backend: Any) -> None:
        for i in range(5):
            self._ws(backend, f"wslim{i}", repo_id="r")
        assert len(backend.coding_jobs(limit=2)) == 2
