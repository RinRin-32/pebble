"""Preflight checks for the coding-dispatch stack.

Dispatch grew a set of runtime requirements that a bare ``docker compose up``
does not satisfy on its own: a seeded Nix store, agent CLIs with credentials
that arrive by bind-mount, a writable shared workspace, and repos registered in
the database.  Each one fails in its own confusing way — a missing credential
directory shows up as ``EACCES`` from Bun, an unseeded store as "nix is not
installed", a wrong PATH as "Not logged in".  Every one of those cost real time
to diagnose during development.

So this reports them as a checklist with the fix attached, rather than leaving
the operator to rediscover each symptom.  Run it inside a node::

    docker compose exec node-1 /app/.venv/bin/python -m turnstone.core.preflight

Checks are advisory: dispatch degrades sensibly when a capability is missing
(no Nix means the base image's runtimes, no codegraph means more grepping), so
a failing line is "this feature is off", not "the stack is broken".
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

SEVERITY_REQUIRED = "required"
SEVERITY_OPTIONAL = "optional"


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    severity: str = SEVERITY_REQUIRED

    @property
    def symbol(self) -> str:
        if self.ok:
            return "✓"
        return "✗" if self.severity == SEVERITY_REQUIRED else "!"


def _check_workspace() -> Check:
    root = Path(os.environ.get("TURNSTONE_WORKSPACE") or "/workspace")
    if not root.is_dir():
        return Check(
            "workspace", False, f"{root} does not exist",
            "Mount it: the compose node services bind ${WORKSPACE_MOUNT:-workspace}:/workspace",
        )
    probe = root / ".preflight-write-test"
    try:
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return Check(
            "workspace", False, f"{root} is not writable ({exc})",
            "Check ownership — the container runs as uid 1000",
        )
    # A volume, not just an image directory: without the mount, worktrees are
    # invisible to the other nodes and dispatch silently stops being clustered.
    mounted = False
    try:
        with open("/proc/mounts") as fh:
            mounted = any(f" {root} " in line for line in fh)
    except OSError:
        mounted = True
    if not mounted:
        return Check(
            "workspace", False, f"{root} exists but is NOT a mount",
            "Worktrees would be node-local. Add the volume to every node service.",
        )
    return Check("workspace", True, f"{root} writable and mounted")


def _check_nix() -> Check:
    from turnstone.core import nixenv

    if nixenv.is_available():
        return Check("nix", True, f"{nixenv.nix_binary()}", severity=SEVERITY_OPTIONAL)
    return Check(
        "nix", False, "no nix binary; per-repo toolchains unavailable",
        "The nix-store volume was not seeded. Check: docker compose logs nix-init",
        severity=SEVERITY_OPTIONAL,
    )


def _check_agents() -> Check:
    from turnstone.core.agents import ADAPTERS, available_agents

    found = available_agents()
    if found:
        missing = sorted(set(ADAPTERS) - set(found))
        detail = ", ".join(found) + (f" (absent: {', '.join(missing)})" if missing else "")
        return Check("agent CLIs", True, detail)
    return Check(
        "agent CLIs", False, "none installed; dispatch_agent cannot run",
        "Rebuild the image — the CLIs are installed there via npm",
    )


def _check_codegraph() -> Check:
    if shutil.which("codegraph"):
        return Check("codegraph", True, "installed", severity=SEVERITY_OPTIONAL)
    return Check(
        "codegraph", False, "absent; agents will grep instead of querying a graph",
        "Rebuild the image", severity=SEVERITY_OPTIONAL,
    )


def _check_claude_auth() -> Check:
    if (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        return Check("claude auth", True, "ANTHROPIC_API_KEY set")
    creds = Path.home() / ".claude" / ".credentials.json"
    if creds.is_file():
        try:
            creds.read_text()
        except OSError:
            return Check(
                "claude auth", False, f"{creds} exists but is unreadable",
                "The mount must be readable by uid 1000",
            )
        return Check("claude auth", True, f"subscription login at {creds}")
    return Check(
        "claude auth", False, "no API key and no mounted login",
        "Set ANTHROPIC_API_KEY in .env, or bind-mount ~/.claude "
        "(see compose.override.yaml.example)",
        severity=SEVERITY_OPTIONAL,
    )


def _check_opencode_auth() -> Check:
    keys = [k for k in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
            if (os.environ.get(k) or "").strip()]
    share = Path.home() / ".local" / "share" / "opencode"
    auth = share / "auth.json"
    if share.is_dir() and not os.access(share, os.W_OK):
        # Docker creates a missing bind-mount PARENT as root; opencode then
        # cannot create its own state beside the credential file and dies with
        # EACCES on startup.
        return Check(
            "opencode auth", False, f"{share} is not writable by the runtime user",
            "The image pre-creates this directory; a bind mount over a missing "
            "parent recreates it as root. Mount the auth.json file, not the directory.",
        )
    if auth.is_file():
        return Check("opencode auth", True, f"login at {auth}")
    if keys:
        return Check("opencode auth", True, f"env keys: {', '.join(keys)}")
    return Check(
        "opencode auth", False, "no provider key and no mounted login",
        "Set OPENROUTER_API_KEY in .env, or mount ~/.local/share/opencode/auth.json",
        severity=SEVERITY_OPTIONAL,
    )


def _check_path() -> Check:
    # A truncated PATH once presented as an authentication failure, because the
    # agent could not spawn the helpers its login flow needs.
    entries = set((os.environ.get("PATH") or "").split(os.pathsep))
    missing = [p for p in ("/usr/bin", "/bin") if p not in entries]
    if missing:
        return Check(
            "PATH", False, f"missing {', '.join(missing)}",
            "run_agent repairs this for children, but the server's own PATH is short",
            severity=SEVERITY_OPTIONAL,
        )
    return Check("PATH", True, "system directories present")


def _open_storage() -> object | None:
    """Storage as the running server sees it.

    ``get_storage()`` returns a default SQLite backend in a process where app
    startup never ran, which makes a preflight in ``docker compose exec`` report
    a failure that does not exist in the server. Prefer the configured URL and
    fall back to the registry.
    """
    url = (os.environ.get("TURNSTONE_DB_URL") or "").strip()
    if url.startswith("postgresql"):
        try:
            from turnstone.core.storage._postgresql import PostgreSQLBackend

            return PostgreSQLBackend(url)
        except Exception:
            return None
    try:
        from turnstone.core.storage._registry import get_storage

        return get_storage()
    except Exception:
        return None


def _check_storage() -> list[Check]:
    out: list[Check] = []
    storage = _open_storage()
    if storage is None:
        return [
            Check(
                "database", False, "no reachable backend",
                "Check TURNSTONE_DB_URL / that postgres is up",
            )
        ]
    try:
        repos = storage.list_repos()
        out.append(
            Check(
                "repos registered", bool(repos),
                ", ".join(r["name"] for r in repos) if repos else "none",
                "bind_repo needs a registered repo; add one via storage.create_repo",
                severity=SEVERITY_OPTIONAL,
            )
        )
    except Exception as exc:
        out.append(Check("repos registered", False, f"query failed ({exc})", "Run migrations"))
    try:
        models = storage.list_model_definitions(enabled_only=True)
        out.append(
            Check(
                "models configured", bool(models),
                ", ".join(m["alias"] for m in models[:5]) if models else "none",
                "Add a model backend in the console UI",
            )
        )
    except Exception as exc:
        out.append(Check("models configured", False, f"query failed ({exc})", "Run migrations"))
    return out


def run_all() -> list[Check]:
    checks = [
        _check_workspace(),
        _check_agents(),
        _check_claude_auth(),
        _check_opencode_auth(),
        _check_nix(),
        _check_codegraph(),
        _check_path(),
    ]
    checks.extend(_check_storage())
    return checks


def format_report(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks) if checks else 0
    lines = ["turnstone coding-dispatch preflight", ""]
    for c in checks:
        lines.append(f"  {c.symbol} {c.name.ljust(width)}  {c.detail}")
        if not c.ok and c.fix:
            lines.append(f"      -> {c.fix}")
    failed = [c for c in checks if not c.ok and c.severity == SEVERITY_REQUIRED]
    degraded = [c for c in checks if not c.ok and c.severity == SEVERITY_OPTIONAL]
    lines.append("")
    if failed:
        lines.append(f"{len(failed)} required check(s) failing — dispatch will not work.")
    elif degraded:
        lines.append(
            f"Ready, with {len(degraded)} optional capability disabled "
            "(dispatch degrades rather than breaking)."
        )
    else:
        lines.append("All checks passed.")
    return "\n".join(lines)


def main() -> int:
    checks = run_all()
    print(format_report(checks))
    return 1 if any(not c.ok and c.severity == SEVERITY_REQUIRED for c in checks) else 0


if __name__ == "__main__":
    sys.exit(main())
