"""Per-repo toolchains via Nix, without a sibling container.

A dispatched agent is only as useful as its ability to *verify* its own work,
and that needs the repo's toolchain: a Go repo needs ``go test``, a Rust repo
needs ``cargo``.  Baking every runtime into the node image doesn't scale — each
repo would pay for every other repo's dependencies.

Nix is the one option that solves this **in-process**: ``nix develop --command``
materializes a toolchain into the same container the agent already runs in.  No
Docker socket, no docker-in-docker, no sibling container — which is what makes
this a tool rather than an architecture change.

Two directories, deliberately:

    /workspace/ws/<ws_id>/flake.nix     generated INTO the worktree, so the
                                        agent can read and tune it, and the
                                        operator can adopt it into the repo
    /workspace/envs/<ws_id>/            a tiny copy holding only flake.nix,
                                        which is what Nix actually evaluates

The split is a measured decision, not fastidiousness.  A flake reference to a
directory copies that directory into the Nix store, and the copy is redone
whenever its contents change.  Evaluating from the worktree cost **16.9s per
command** after each agent edit (measured on a 2 MB repo); evaluating from the
tiny env dir costs **0.27s**.  The agent edits constantly during a dispatch, so
that difference is the feature.

``path:`` references are used rather than plain paths because a flake inside a
git repository is evaluated from the *git tree*, and a generated file that was
never committed is invisible to it — Nix fails and suggests ``git add -N``.
``path:`` reads the filesystem directly, so nothing has to be staged, and the
generated flake never lands in a review diff.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from turnstone.core.log import get_logger
from turnstone.core.workspace import _SAFE_ID, workspace_root

log = get_logger(__name__)

# Nix ships in the image at a fixed profile path; PATH is unreliable here (a
# login shell rewrites it), so probe the known location before falling back.
_NIX_CANDIDATES = (
    "/nix/var/nix/profiles/default/bin/nix",
    "/run/current-system/sw/bin/nix",
)

# Marker file -> nixpkgs attributes.  Ordered longest-first so a specific marker
# (pyproject.toml) is preferred over a generic one (requirements.txt) when both
# appear; every match contributes, because real repos are polyglot — Kokoro-go
# carries go.mod AND pyproject.toml.
TOOLCHAIN_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("go.mod", ("go", "gopls")),
    ("Cargo.toml", ("cargo", "rustc")),
    ("pyproject.toml", ("python312", "uv")),
    ("requirements.txt", ("python312", "uv")),
    ("setup.py", ("python312",)),
    ("package.json", ("nodejs_22",)),
    ("pnpm-lock.yaml", ("nodejs_22", "pnpm")),
    ("Gemfile", ("ruby",)),
    ("pom.xml", ("maven", "jdk")),
    ("build.gradle", ("gradle", "jdk")),
    ("CMakeLists.txt", ("cmake", "gcc")),
    ("Makefile", ("gnumake",)),
)

# Always present: an agent that can't run git or a shell is useless.
_BASE_PACKAGES = ("git", "coreutils", "bash")

_PROVISION_TIMEOUT = 900
_NIXPKGS_REF = "github:NixOS/nixpkgs/nixos-unstable"


class NixEnvError(RuntimeError):
    """Raised when Nix is unavailable or a shell fails to build."""


@dataclass
class EnvSpec:
    """What a worktree appears to need."""

    packages: list[str] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)
    repo_flake: bool = False


@dataclass
class EnvInfo:
    env_dir: Path
    flake_path: Path
    packages: list[str]
    repo_flake: bool


def nix_binary() -> str | None:
    """Path to the nix executable, or None when Nix isn't installed."""
    for candidate in _NIX_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return shutil.which("nix")


def is_available() -> bool:
    return nix_binary() is not None


def envs_root() -> Path:
    return workspace_root() / "envs"


def env_dir_for(ws_id: str) -> Path:
    if not ws_id or not _SAFE_ID.match(ws_id):
        raise NixEnvError(f"invalid ws_id: {ws_id!r}")
    return envs_root() / ws_id


def detect(worktree: Path) -> EnvSpec:
    """Infer the toolchain from files present in *worktree*.

    A repo's own ``flake.nix`` wins outright — it is the author's declaration and
    almost certainly better than anything inferred.  Otherwise every matching
    marker contributes packages, because polyglot repos are the normal case.
    """
    spec = EnvSpec()
    if (worktree / "flake.nix").is_file():
        spec.repo_flake = True
        spec.markers.append("flake.nix")
        return spec
    seen: set[str] = set()
    for marker, packages in TOOLCHAIN_MARKERS:
        if not (worktree / marker).exists():
            continue
        spec.markers.append(marker)
        for pkg in packages:
            if pkg not in seen:
                seen.add(pkg)
                spec.packages.append(pkg)
    if not spec.packages:
        # No markers at all (a bare script repo) still deserves an interpreter;
        # this is the common case, not an edge case.
        spec.packages = ["python312"]
    return spec


def render_flake(packages: list[str]) -> str:
    """Generate a devShell flake for *packages*."""
    pkg_lines = "\n".join(f"            pkgs.{p}" for p in packages)
    return f"""{{
  # Generated by turnstone for coding-agent dispatch.
  # Edit freely — it is re-read on the next setup_env(action="provision").
  # It is NOT committed by turnstone; adopt it into the repo if it works well.
  description = "turnstone dev environment";

  inputs.nixpkgs.url = "{_NIXPKGS_REF}";

  outputs = {{ self, nixpkgs }}:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${{system}};
    in
    {{
      devShells.${{system}}.default = pkgs.mkShell {{
        packages = [
{pkg_lines}
        ];
      }};
    }};
}}
"""


def provision(ws_id: str, worktree: Path, *, timeout: int = _PROVISION_TIMEOUT) -> EnvInfo:
    """Materialize the toolchain for *worktree* and return its env handle.

    Writes the flake into the worktree (agent-visible) and syncs a copy into the
    evaluation directory, then builds the shell so the first dispatch doesn't pay
    the download.
    """
    nix = nix_binary()
    if nix is None:
        raise NixEnvError("nix is not installed on this node")
    spec = detect(worktree)
    env_dir = env_dir_for(ws_id)
    env_dir.mkdir(parents=True, exist_ok=True)

    if spec.repo_flake:
        # Evaluate the repo's own flake from its own directory: it may reference
        # sibling files, so it cannot be copied out in isolation.
        flake_path = worktree / "flake.nix"
        source_dir = worktree
    else:
        packages = [*_BASE_PACKAGES, *spec.packages]
        content = render_flake(packages)
        flake_path = worktree / "flake.nix"
        # Preserve a flake the agent has tuned: only write when absent or when
        # turnstone itself generated the existing one.
        existing = flake_path.read_text(encoding="utf-8", errors="replace") if flake_path.is_file() else ""
        if not existing or "Generated by turnstone" in existing:
            flake_path.write_text(content, encoding="utf-8")
        (env_dir / "flake.nix").write_text(
            flake_path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8"
        )
        source_dir = env_dir

    try:
        proc = subprocess.run(  # noqa: S603 - argv list, fixed binary
            [nix, "develop", f"path:{source_dir}", "--command", "true"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise NixEnvError(f"nix develop timed out after {timeout}s") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise NixEnvError(f"nix develop failed: {detail[-500:]}")

    log.info(
        "nixenv.provisioned",
        ws_id=ws_id,
        repo_flake=spec.repo_flake,
        markers=spec.markers,
    )
    return EnvInfo(
        env_dir=source_dir,
        flake_path=flake_path,
        packages=spec.packages,
        repo_flake=spec.repo_flake,
    )


def wrap_command(argv: list[str], source_dir: str | Path) -> list[str]:
    """Wrap *argv* so it runs inside the provisioned shell.

    ``--command`` takes an argument vector, not a shell string, so a prompt
    containing quotes or ``;`` stays inert — the same discipline the rest of the
    dispatch path follows.
    """
    nix = nix_binary()
    if nix is None:
        return argv
    return [nix, "develop", f"path:{source_dir}", "--command", *argv]


def teardown(ws_id: str) -> bool:
    """Remove a workstream's evaluation directory.  Store paths are left to
    ``nix store gc``; deleting them here could yank a shell out from under
    another workstream sharing the same derivation."""
    env_dir = env_dir_for(ws_id)
    if not env_dir.exists():
        return False
    shutil.rmtree(env_dir, ignore_errors=True)
    return True
