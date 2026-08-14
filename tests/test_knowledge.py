"""Tests for the graph knowledge base (turnstone/core/knowledge.py).

Exercises the real vault on disk: the files are the source of truth, so writing
and re-reading them is the behaviour that matters.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pebble.core import knowledge as kb


@pytest.fixture(autouse=True)
def _vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PEBBLE_WORKSPACE", str(tmp_path / "workspace"))


class TestSlugAndLinks:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Dispatch adapter protocol", "dispatch-adapter-protocol"),
            ("Worktree/Isolation!", "worktree-isolation"),
            ("  spaced  out  ", "spaced-out"),
        ],
    )
    def test_slugify(self, title: str, expected: str) -> None:
        assert kb.slugify(title) == expected

    def test_slugify_non_ascii_never_empty(self) -> None:
        # A CJK-only title must still produce a usable, unique filename.
        a, b = kb.slugify("設計メモ"), kb.slugify("別のメモ")
        assert a and b and a != b

    def test_extract_links_forms_and_dedupe(self) -> None:
        body = "See [[Alpha]] and [[Beta|the beta note]] and [[Alpha]] again, [[Gamma#section]]."
        assert kb.extract_links(body) == ["Alpha", "Beta", "Gamma"]

    def test_extract_links_none(self) -> None:
        assert kb.extract_links("plain prose, no links") == []


class TestWriteRead:
    def test_roundtrip(self) -> None:
        kb.write_note(
            kb.Note(
                title="Adapter protocol",
                body="We normalize agents. See [[Worktree isolation]].",
                kind="decision",
                summary="one event stream",
                tags=["architecture", "agents"],
            )
        )
        note = kb.read_note("Adapter protocol")
        assert note is not None
        assert note.kind == "decision"
        assert note.summary == "one event stream"
        assert note.tags == ["architecture", "agents"]
        assert note.links == ["Worktree isolation"]

    def test_file_is_obsidian_shaped(self) -> None:
        path = kb.write_note(kb.Note(title="Shape", body="body [[X]]"))
        text = path.read_text()
        # Frontmatter + wikilinks == openable in Obsidian without conversion.
        assert text.startswith("---\n") and "title: Shape" in text
        assert "[[X]]" in text

    def test_write_replaces_append_grows(self) -> None:
        kb.write_note(kb.Note(title="Log", body="first finding"))
        kb.write_note(kb.Note(title="Log", body="second finding"), append=True)
        note = kb.read_note("Log")
        assert note is not None
        assert "first finding" in note.body and "second finding" in note.body
        kb.write_note(kb.Note(title="Log", body="replaced"))
        note = kb.read_note("Log")
        assert note is not None and "first finding" not in note.body

    def test_append_preserves_tags_and_merges(self) -> None:
        kb.write_note(kb.Note(title="T", body="a", tags=["x"]))
        kb.write_note(kb.Note(title="T", body="b", tags=["y"]), append=True)
        note = kb.read_note("T")
        assert note is not None and note.tags == ["x", "y"]

    def test_missing_note(self) -> None:
        assert kb.read_note("nope") is None

    def test_title_required_and_bounded(self) -> None:
        with pytest.raises(kb.KnowledgeError):
            kb.write_note(kb.Note(title="  ", body="x"))
        with pytest.raises(kb.KnowledgeError):
            kb.write_note(kb.Note(title="t" * 200, body="x"))

    def test_colon_in_title_survives_frontmatter(self) -> None:
        # An unquoted colon would corrupt the YAML block.
        kb.write_note(kb.Note(title="RAG: not the answer", body="x"))
        note = kb.read_note("RAG: not the answer")
        assert note is not None and note.title == "RAG: not the answer"


class TestSearch:
    def _seed(self) -> None:
        kb.write_note(kb.Note(title="Worktree isolation", body="git worktree per workstream"))
        kb.write_note(
            kb.Note(title="Cost accounting", body="opencode sums per-step", tags=["billing"])
        )

    def test_finds_by_title_and_body(self) -> None:
        self._seed()
        assert kb.search_notes("worktree")[0][0].title == "Worktree isolation"
        assert any(n.title == "Cost accounting" for n, _ in kb.search_notes("opencode"))

    def test_title_outranks_body(self) -> None:
        kb.write_note(kb.Note(title="Zebra", body="x"))
        kb.write_note(kb.Note(title="Other", body="zebra zebra zebra"))
        assert kb.search_notes("zebra")[0][0].title == "Zebra"

    def test_empty_query_and_no_match(self) -> None:
        self._seed()
        assert kb.search_notes("") == []
        assert kb.search_notes("quantumbanana") == []


class TestGraph:
    def _seed(self) -> None:
        kb.write_note(kb.Note(title="Hub", body="see [[Leaf]] and [[Unwritten]]"))
        kb.write_note(kb.Note(title="Leaf", body="back to [[Hub]]"))
        kb.write_note(kb.Note(title="Lonely", body="no links here"))

    def test_neighbours(self) -> None:
        self._seed()
        n = kb.neighbours("Hub")
        assert set(n["outgoing"]) == {"Leaf", "Unwritten"}
        assert n["backlinks"] == ["Leaf"]
        # A link to a note that doesn't exist yet is the research frontier.
        assert n["dangling"] == ["Unwritten"]

    def test_graph_summary(self) -> None:
        self._seed()
        g = kb.graph_summary()
        assert g["notes"] == 3
        assert g["links"] == 3
        assert ("Unwritten", 1) in g["frontier"]
        assert "Lonely" in g["orphans"]

    def test_empty_vault(self) -> None:
        g = kb.graph_summary()
        assert g["notes"] == 0 and g["links"] == 0

    def test_hand_edited_file_is_tolerated(self) -> None:
        # The vault must survive a human editing it outside turnstone.
        root = kb.vault_root()
        root.mkdir(parents=True, exist_ok=True)
        (root / "manual.md").write_text("no frontmatter at all, but [[Hub]]\n")
        notes = kb.list_notes()
        assert any(n.title == "manual" and n.links == ["Hub"] for n in notes)


class TestExperiments:
    """The KB records MEASURED facts, not claimed ones.

    An experiment runs its own command, so a recorded number cannot be one an
    agent invented after the fact. That is the whole point of it being a tool
    action rather than free-form prose.
    """

    def test_runs_the_command_and_captures_real_output(self, tmp_path: Path) -> None:
        res = kb.run_experiment("echo hello-from-shell", cwd=tmp_path)
        assert res.ok is True
        assert "hello-from-shell" in res.output
        assert res.exit_code == 0 and res.duration_s >= 0

    def test_records_failure_honestly(self, tmp_path: Path) -> None:
        res = kb.run_experiment("exit 3", cwd=tmp_path)
        assert res.ok is False and res.exit_code == 3

    def test_captures_stderr_too(self, tmp_path: Path) -> None:
        res = kb.run_experiment("echo oops >&2", cwd=tmp_path)
        assert "oops" in res.output

    def test_output_is_bounded(self, tmp_path: Path) -> None:
        res = kb.run_experiment("head -c 200000 /dev/zero | tr '\\0' 'x'", cwd=tmp_path)
        assert len(res.output) <= kb.MAX_CAPTURE + 100
        assert "truncated" in res.output

    def test_timeout_is_recorded_not_raised(self, tmp_path: Path) -> None:
        res = kb.run_experiment("sleep 5", cwd=tmp_path, timeout=1)
        assert res.timed_out is True and res.ok is False

    def test_note_carries_provenance(self, tmp_path: Path) -> None:
        res = kb.run_experiment("echo measured", cwd=tmp_path)
        note = kb.experiment_note("Cost of X", "X should be cheap", res, repo_id="r1")
        kb.write_note(note)
        stored = kb.read_note("Cost of X")
        assert stored is not None
        assert stored.kind == "experiment"
        assert "X should be cheap" in stored.body  # hypothesis
        assert "echo measured" in stored.body  # method
        assert "measured" in stored.body  # captured result
        assert "Verdict" in stored.body  # prompts for interpretation

    def test_note_links_related_findings(self, tmp_path: Path) -> None:
        res = kb.run_experiment("true", cwd=tmp_path)
        note = kb.experiment_note("A", "h", res, links=["Worktree isolation"])
        kb.write_note(note)
        assert "Worktree isolation" in kb.read_note("A").links


class TestStaleness:
    """A finding about code has an expiry date."""

    def _repo(self, tmp_path: Path) -> Path:
        import subprocess

        src = tmp_path / "repo"
        src.mkdir()
        for cmd in (
            ["git", "init", "-q", "-b", "main"],
            ["git", "config", "user.email", "t@t"],
            ["git", "config", "user.name", "t"],
        ):
            subprocess.run(cmd, cwd=src, check=True, capture_output=True)
        (src / "a.txt").write_text("1\n")
        subprocess.run(["git", "add", "-A"], cwd=src, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "one"], cwd=src, check=True, capture_output=True)
        return src

    def test_commit_of_real_repo(self, tmp_path: Path) -> None:
        assert kb.commit_of(self._repo(tmp_path)) != ""

    def test_commit_of_non_repo_is_empty(self, tmp_path: Path) -> None:
        assert kb.commit_of(tmp_path) == ""

    def test_finding_flagged_after_code_moves(self, tmp_path: Path) -> None:
        import subprocess

        repo = self._repo(tmp_path)
        res = kb.run_experiment("echo v1", cwd=repo)
        kb.write_note(kb.experiment_note("Finding", "h", res, repo_id="r1"))
        assert kb.stale_notes(res.commit, repo_id="r1") == []
        # Code moves on; the finding was measured against the old tree.
        (repo / "a.txt").write_text("2\n")
        subprocess.run(["git", "commit", "-aqm", "two"], cwd=repo, check=True, capture_output=True)
        drifted = kb.stale_notes(kb.commit_of(repo), repo_id="r1")
        assert len(drifted) == 1 and drifted[0][0].title == "Finding"

    def test_notes_without_measurement_are_never_stale(self) -> None:
        kb.write_note(kb.Note(title="Design decision", body="prose only"))
        assert kb.stale_notes("abc1234") == []

    def test_login_shell_does_not_reset_path(self, tmp_path: Path) -> None:
        """Regression: `bash -lc` rebuilt PATH and discarded the Nix toolchain.

        A wrapped experiment reported "command not found" for a tool that was
        provisioned correctly — the same login-shell PATH reset that once made
        Claude Code look unauthenticated.
        """
        import os

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        tool = fake_bin / "onlytool"
        tool.write_text("#!/bin/sh\necho present\n")
        tool.chmod(0o755)
        env_path = f"{fake_bin}{os.pathsep}{os.environ['PATH']}"
        old = os.environ["PATH"]
        os.environ["PATH"] = env_path
        try:
            res = kb.run_experiment("onlytool", cwd=tmp_path)
        finally:
            os.environ["PATH"] = old
        assert "present" in res.output, "inherited PATH was discarded by a login shell"

    def test_secrets_are_redacted_from_captured_output(self, tmp_path: Path) -> None:
        """Captured output becomes a PERMANENT note.

        A command that echoes a token would otherwise write it into the vault
        forever, surviving the container and any later rotation.
        """
        res = kb.run_experiment(
            "echo 'Authorization: Bearer sk-ant-api03-SUPERSECRETVALUE123'", cwd=tmp_path
        )
        assert "SUPERSECRETVALUE123" not in res.output, "a token reached the vault"

    def test_redaction_keeps_the_useful_part(self, tmp_path: Path) -> None:
        res = kb.run_experiment("echo 'tests passed in 12.3s'", cwd=tmp_path)
        assert "tests passed" in res.output


class TestVaultIsGit:
    """The vault syncs across devices as a git repo, not via a server."""

    def test_vault_becomes_a_repo_on_first_write(self) -> None:
        kb.write_note(kb.Note(title="First", body="hello"))
        assert (kb.vault_root() / ".git").exists()

    def test_each_note_is_committed(self) -> None:
        import subprocess

        kb.write_note(kb.Note(title="Alpha", body="a"))
        kb.write_note(kb.Note(title="Beta", body="b"))
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=kb.vault_root(),
            capture_output=True, text=True,
        ).stdout
        assert "Alpha" in log and "Beta" in log

    def test_append_is_its_own_commit(self) -> None:
        import subprocess

        kb.write_note(kb.Note(title="Log", body="one"))
        kb.write_note(kb.Note(title="Log", body="two"), append=True)
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=kb.vault_root(),
            capture_output=True, text=True,
        ).stdout
        # History is the point: a finding can be traced to when it was learned.
        assert "append: Log" in log and "write: Log" in log

    def test_codegraph_is_ignored(self) -> None:
        kb.write_note(kb.Note(title="X", body="x"))
        assert ".codegraph/" in (kb.vault_root() / ".gitignore").read_text()

    def test_a_failed_commit_never_loses_the_note(self, monkeypatch) -> None:
        # The note is the product; version control is a convenience on top.
        monkeypatch.setattr(kb, "_commit_vault", lambda msg: (_ for _ in ()).throw(OSError("no git")))
        try:
            kb.write_note(kb.Note(title="Survives", body="content"))
        except OSError:
            pass
        assert kb.note_path("Survives").exists()

    def test_readme_is_not_a_note(self) -> None:
        # The vault's own README would otherwise appear as an orphan node.
        kb.write_note(kb.Note(title="Real finding", body="x"))
        titles = [n.title for n in kb.list_notes()]
        assert titles == ["Real finding"]
        assert (kb.vault_root() / "README.md").exists()
