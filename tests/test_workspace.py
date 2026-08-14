"""Tests for per-workstream git worktrees (turnstone/core/workspace.py).

Uses a real git repo on disk — the module is a thin, security-sensitive shell
over ``git``, so mocking the subprocess would test nothing that matters.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from turnstone.core import workspace


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True)


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A tiny upstream repo to mirror."""
    src = tmp_path / "origin"
    src.mkdir()
    _run("git", "init", "-q", "-b", "main", cwd=src)
    _run("git", "config", "user.email", "t@t", cwd=src)
    _run("git", "config", "user.name", "t", cwd=src)
    (src / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    _run("git", "add", "-A", cwd=src)
    _run("git", "commit", "-qm", "init", cwd=src)
    return src


@pytest.fixture(autouse=True)
def _workspace_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TURNSTONE_WORKSPACE", str(tmp_path / "workspace"))


class TestIdValidation:
    @pytest.mark.parametrize("bad", ["../escape", "a/b", "", ".", "x" * 200, "-flag"])
    def test_rejects_unsafe_ids(self, bad: str) -> None:
        with pytest.raises(workspace.WorkspaceError):
            workspace.worktree_path(bad)

    def test_rejects_flag_like_url(self) -> None:
        with pytest.raises(workspace.WorkspaceError):
            workspace.ensure_mirror("repo1", "--upload-pack=evil")

    def test_paths_stay_under_root(self) -> None:
        root = workspace.workspace_root().resolve()
        assert str(workspace.worktree_path("ws1").resolve()).startswith(str(root))
        assert str(workspace.mirror_path("repo1").resolve()).startswith(str(root))


class TestMirrorAndWorktree:
    def test_clone_then_fetch_is_idempotent(self, origin: Path) -> None:
        p1 = workspace.ensure_mirror("repo1", str(origin))
        assert p1.exists()
        p2 = workspace.ensure_mirror("repo1", str(origin))  # second call fetches
        assert p1 == p2

    def test_create_worktree_checks_out_files(self, origin: Path) -> None:
        workspace.ensure_mirror("repo1", str(origin))
        info = workspace.create_worktree("repo1", "ws0001")
        assert info.path.is_dir()
        assert (info.path / "calc.py").read_text().startswith("def add")
        assert info.branch == "turnstone/ws0001"

    def test_worktree_requires_mirror(self) -> None:
        with pytest.raises(workspace.WorkspaceError):
            workspace.create_worktree("nope", "ws0002")

    def test_two_workstreams_are_isolated(self, origin: Path) -> None:
        workspace.ensure_mirror("repo1", str(origin))
        a = workspace.create_worktree("repo1", "wsaaa")
        b = workspace.create_worktree("repo1", "wsbbb")
        assert a.path != b.path
        (a.path / "calc.py").write_text("changed by A\n")
        # B must not see A's edit — the whole point of per-workstream trees.
        assert (b.path / "calc.py").read_text().startswith("def add")

    def test_create_is_reentrant(self, origin: Path) -> None:
        workspace.ensure_mirror("repo1", str(origin))
        first = workspace.create_worktree("repo1", "wsre")
        (first.path / "wip.txt").write_text("in progress\n")
        again = workspace.create_worktree("repo1", "wsre")
        # A reactivated workstream keeps its in-progress edits.
        assert (again.path / "wip.txt").exists()


class TestDiff:
    def test_diff_includes_modifications(self, origin: Path) -> None:
        workspace.ensure_mirror("repo1", str(origin))
        info = workspace.create_worktree("repo1", "wsdiff")
        (info.path / "calc.py").write_text("def add(a, b):\n    return a + b + 1\n")
        diff = workspace.worktree_diff("wsdiff")
        assert "calc.py" in diff and "+    return a + b + 1" in diff

    def test_diff_includes_new_files(self, origin: Path) -> None:
        workspace.ensure_mirror("repo1", str(origin))
        info = workspace.create_worktree("repo1", "wsnew")
        (info.path / "brand_new.py").write_text("x = 1\n")
        diff = workspace.worktree_diff("wsnew")
        # Untracked files must appear or a dispatch that adds a module looks
        # like it did nothing.
        assert "brand_new.py" in diff

    def test_diff_truncates(self, origin: Path) -> None:
        workspace.ensure_mirror("repo1", str(origin))
        info = workspace.create_worktree("repo1", "wsbig")
        (info.path / "big.txt").write_text("line\n" * 50_000)
        diff = workspace.worktree_diff("wsbig", max_bytes=1000)
        assert "truncated" in diff and len(diff) < 1500

    def test_stat_summary(self, origin: Path) -> None:
        workspace.ensure_mirror("repo1", str(origin))
        info = workspace.create_worktree("repo1", "wsstat")
        (info.path / "calc.py").write_text("changed\n")
        assert "calc.py" in workspace.worktree_stat("wsstat")

    def test_stat_includes_new_files(self, origin: Path) -> None:
        # A dispatch that only CREATES files (a .gitignore, a new module) must
        # not report an empty summary and read as having done nothing.
        workspace.ensure_mirror("repo1", str(origin))
        info = workspace.create_worktree("repo1", "wsstatnew")
        (info.path / ".gitignore").write_text("__pycache__/\n")
        assert ".gitignore" in workspace.worktree_stat("wsstatnew")

    def test_diff_without_worktree_errors(self) -> None:
        with pytest.raises(workspace.WorkspaceError):
            workspace.worktree_diff("ghostws")


class TestResolveCwdAndRemoval:
    def test_resolve_cwd_none_when_unbound(self) -> None:
        # Legacy sessions have no worktree and must keep working.
        assert workspace.resolve_cwd("nosuchws") is None
        assert workspace.resolve_cwd("") is None
        assert workspace.resolve_cwd("../evil") is None

    def test_resolve_cwd_returns_path(self, origin: Path) -> None:
        workspace.ensure_mirror("repo1", str(origin))
        info = workspace.create_worktree("repo1", "wscwd")
        assert workspace.resolve_cwd("wscwd") == str(info.path)

    def test_remove_worktree(self, origin: Path) -> None:
        workspace.ensure_mirror("repo1", str(origin))
        info = workspace.create_worktree("repo1", "wsrm")
        assert workspace.remove_worktree("repo1", "wsrm") is True
        assert not info.path.exists()
        assert workspace.remove_worktree("repo1", "wsrm") is False


class TestLocalExcludes:
    """A dispatched agent that RUNS the code it wrote must not pollute the diff."""

    def test_build_artifacts_excluded(self, origin: Path) -> None:
        workspace.ensure_mirror("repo1", str(origin))
        info = workspace.create_worktree("repo1", "wsexcl")
        (info.path / "__pycache__").mkdir()
        (info.path / "__pycache__" / "calc.cpython-312.pyc").write_bytes(b"\x00fake")
        (info.path / "node_modules").mkdir()
        (info.path / "node_modules" / "x.js").write_text("1")
        (info.path / "real_change.py").write_text("x = 1\n")
        diff = workspace.worktree_diff("wsexcl")
        assert "real_change.py" in diff
        assert "__pycache__" not in diff
        assert "node_modules" not in diff

    def test_repo_gitignore_untouched(self, origin: Path) -> None:
        workspace.ensure_mirror("repo1", str(origin))
        info = workspace.create_worktree("repo1", "wsgi")
        # Excludes live in the worktree's private admin dir, not tracked files.
        assert not (info.path / ".gitignore").exists()


class TestConcurrency:
    """Two Discord users dispatching on the same repo at the same moment.

    Regression: concurrent ``git worktree add`` raced on the mirror's
    config.lock ("could not lock config file"), found by an 8-way stress run.
    """

    def test_concurrent_worktree_creation(self, origin: Path) -> None:
        import concurrent.futures as cf

        workspace.ensure_mirror("repo1", str(origin))

        def mk(i: int) -> str:
            info = workspace.create_worktree("repo1", f"wsrace{i:03d}")
            (info.path / f"marker_{i}.txt").write_text(str(i))
            return str(info.path)

        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            paths = list(ex.map(mk, range(8)))
        assert len(set(paths)) == 8

    def test_concurrent_same_ws_id_is_safe(self, origin: Path) -> None:
        import concurrent.futures as cf

        workspace.ensure_mirror("repo1", str(origin))
        with cf.ThreadPoolExecutor(max_workers=4) as ex:
            infos = list(ex.map(lambda _: workspace.create_worktree("repo1", "wsdupe"), range(4)))
        # All four callers converge on one worktree rather than clobbering.
        assert len({str(i.path) for i in infos}) == 1

    def test_no_cross_contamination(self, origin: Path) -> None:
        import concurrent.futures as cf

        workspace.ensure_mirror("repo1", str(origin))

        def mk(i: int) -> set[str]:
            info = workspace.create_worktree("repo1", f"wsiso{i:03d}")
            (info.path / f"only_{i}.txt").write_text("x")
            return {f.name for f in info.path.iterdir() if f.is_file()}

        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            listings = list(ex.map(mk, range(6)))
        for i, files in enumerate(listings):
            assert f"only_{i}.txt" in files
            assert not files & {f"only_{j}.txt" for j in range(6) if j != i}


class TestBaseRefFallback:
    """A registered default_branch can disagree with the remote's real HEAD."""

    def test_default_branch_reads_mirror_head(self, origin: Path) -> None:
        workspace.ensure_mirror("repo1", str(origin))
        assert workspace.default_branch("repo1") == "main"

    def test_bad_base_ref_falls_back(self, origin: Path) -> None:
        workspace.ensure_mirror("repo1", str(origin))
        # 'master' does not exist here (the fixture uses 'main'); rather than
        # failing with "invalid reference", fall back to the mirror's HEAD.
        info = workspace.create_worktree("repo1", "wsfallback", base_ref="master")
        assert (info.path / "calc.py").is_file()

    def test_default_branch_missing_mirror(self) -> None:
        assert workspace.default_branch("nosuchrepo") == ""
