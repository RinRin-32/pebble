"""Tests for per-repo Nix toolchains (turnstone/core/nixenv.py).

Detection, flake rendering and command wrapping are pure functions of the
filesystem, so they're tested directly. Actually building a shell needs Nix and
a network, so that path is exercised in the container rather than here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pebble.core import nixenv

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PEBBLE_WORKSPACE", str(tmp_path / "workspace"))


class TestDetect:
    def test_go_repo(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module x\n")
        spec = nixenv.detect(tmp_path)
        assert "go" in spec.packages and spec.markers == ["go.mod"]

    def test_polyglot_repo_gets_both(self, tmp_path: Path) -> None:
        # Kokoro-go is real: go.mod AND pyproject.toml in one tree.
        (tmp_path / "go.mod").write_text("module x\n")
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        spec = nixenv.detect(tmp_path)
        assert "go" in spec.packages and "python312" in spec.packages
        assert set(spec.markers) == {"go.mod", "pyproject.toml"}

    def test_bare_repo_still_gets_an_interpreter(self, tmp_path: Path) -> None:
        # bobthesumo has no markers at all; that's the common case, not an edge.
        spec = nixenv.detect(tmp_path)
        assert spec.packages == ["python312"]
        assert spec.markers == []

    def test_repo_flake_wins_outright(self, tmp_path: Path) -> None:
        (tmp_path / "flake.nix").write_text("{}\n")
        (tmp_path / "go.mod").write_text("module x\n")
        spec = nixenv.detect(tmp_path)
        # The author's own declaration beats anything inferred.
        assert spec.repo_flake is True
        assert spec.packages == []

    def test_no_duplicate_packages(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[project]\n")
        (tmp_path / "requirements.txt").write_text("x\n")
        spec = nixenv.detect(tmp_path)
        assert spec.packages.count("python312") == 1


class TestRenderFlake:
    def test_multi_system_for_portability(self) -> None:
        out = nixenv.render_flake(["go"])
        # An environment that only builds on x86 is not portable, and these are
        # meant to be cloned onto other machines (one target repo is a Pi project).
        for system in ("x86_64-linux", "aarch64-linux", "aarch64-darwin"):
            assert system in out
        assert "forAllSystems" in out

    def test_includes_packages_and_marker(self) -> None:
        out = nixenv.render_flake(["go", "python312"])
        assert "pkgs.go" in out and "pkgs.python312" in out
        assert nixenv._GENERATED_MARKER in out

    def test_rejects_injection_in_package_name(self) -> None:
        with pytest.raises(nixenv.NixEnvError):
            nixenv.render_flake(["go; rm -rf /"])


class TestNamedEnvs:
    def test_create_list_and_get(self) -> None:
        nixenv.create_env("go-dev", ["go"])
        names = [e.name for e in nixenv.list_envs()]
        assert "go-dev" in names
        env = nixenv.get_env("go-dev")
        assert env is not None and "go" in env.packages
        # Base packages are always present: an agent without git or a shell is useless.
        assert "git" in env.packages

    def test_add_packages_is_the_curation_path(self) -> None:
        nixenv.create_env("go-dev", ["go"])
        env = nixenv.add_packages("go-dev", ["ffmpeg"])
        assert "ffmpeg" in env.packages and "go" in env.packages
        # Re-reading picks it up, so other workstreams see it too.
        assert "ffmpeg" in nixenv.get_env("go-dev").packages

    def test_add_to_missing_env(self) -> None:
        with pytest.raises(nixenv.NixEnvError):
            nixenv.add_packages("nope", ["go"])

    def test_hand_edited_flake_is_never_clobbered(self) -> None:
        env = nixenv.create_env("tuned", ["go"])
        (env.path / "flake.nix").write_text("{ /* mine, no marker */ }\n")
        # Curation has to survive an accidental re-provision.
        with pytest.raises(nixenv.NixEnvError):
            nixenv.create_env("tuned", ["python312"])
        with pytest.raises(nixenv.NixEnvError):
            nixenv.add_packages("tuned", ["ffmpeg"])

    @pytest.mark.parametrize("bad", ["../escape", "UPPER", "", "a/b", "x" * 100])
    def test_rejects_unsafe_names(self, bad: str) -> None:
        with pytest.raises(nixenv.NixEnvError):
            nixenv.env_path(bad)

    def test_env_for_repo_bootstraps_then_reuses(self, tmp_path: Path) -> None:
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "go.mod").write_text("module x\n")
        first = nixenv.env_for_repo("kokoro-go", wt)
        assert "go" in first.packages
        # A second workstream on the same repo reuses it rather than rebuilding.
        again = nixenv.env_for_repo("kokoro-go", wt)
        assert again.path == first.path

    def test_delete(self) -> None:
        nixenv.create_env("tmp-env", ["go"])
        assert nixenv.delete_env("tmp-env") is True
        assert nixenv.delete_env("tmp-env") is False


class TestRegistryIsGit:
    def test_registry_is_versioned(self) -> None:
        """The env set is portable precisely because it is a git repo."""
        import subprocess

        nixenv.create_env("go-dev", ["go"])
        root = nixenv.envs_root()
        assert (root / ".git").exists()
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=str(root), capture_output=True, text=True
        )
        assert "go-dev" in log.stdout


class TestWrapCommand:
    def test_wraps_with_path_ref(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(nixenv, "nix_binary", lambda: "/usr/bin/nix")
        argv = ["opencode", "run", "do; rm -rf /"]
        out = nixenv.wrap_command(argv, "/workspace/envs/go-dev")
        assert out[:4] == ["/usr/bin/nix", "develop", "path:/workspace/envs/go-dev", "--command"]
        assert out[4:] == argv

    def test_passthrough_without_nix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(nixenv, "nix_binary", lambda: None)
        assert nixenv.wrap_command(["claude"], "/env") == ["claude"]


class TestExtensionFallback:
    """A repo with no build file must not be handed the wrong interpreter."""

    def test_java_without_pom(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "Main.java").write_text("class Main {}\n")
        spec = nixenv.detect(tmp_path)
        assert "jdk" in spec.packages and "python312" not in spec.packages

    def test_go_without_gomod(self, tmp_path: Path) -> None:
        (tmp_path / "main.go").write_text("package main\n")
        assert "go" in nixenv.detect(tmp_path).packages

    def test_build_file_still_wins(self, tmp_path: Path) -> None:
        (tmp_path / "go.mod").write_text("module x\n")
        (tmp_path / "Main.java").write_text("class Main {}\n")
        # An explicit build file is a stronger signal than a stray source file.
        assert "go" in nixenv.detect(tmp_path).packages

    def test_truly_empty_repo_gets_python(self, tmp_path: Path) -> None:
        assert nixenv.detect(tmp_path).packages == ["python312"]
