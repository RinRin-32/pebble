"""Tests for the coding-dispatch preflight checks.

Each check encodes a failure that actually cost time to diagnose during
development, so the tests assert the *diagnosis*, not just a boolean.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from turnstone.core import preflight


class TestWorkspaceCheck:
    def test_missing_workspace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TURNSTONE_WORKSPACE", str(tmp_path / "nope"))
        check = preflight._check_workspace()
        assert check.ok is False and "does not exist" in check.detail
        assert check.fix

    def test_present_but_not_mounted_is_flagged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A directory baked into the image is not the shared volume; without the
        # mount, worktrees are node-local and dispatch stops being clustered.
        ws = tmp_path / "workspace"
        ws.mkdir()
        monkeypatch.setenv("TURNSTONE_WORKSPACE", str(ws))
        check = preflight._check_workspace()
        assert check.ok is False and "NOT a mount" in check.detail

    def test_write_probe_is_cleaned_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        monkeypatch.setenv("TURNSTONE_WORKSPACE", str(ws))
        preflight._check_workspace()
        assert list(ws.iterdir()) == []


class TestPathCheck:
    def test_truncated_path_flagged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A short PATH once presented as an authentication failure.
        monkeypatch.setenv("PATH", "/app/.venv/bin")
        check = preflight._check_path()
        assert check.ok is False
        assert "/usr/bin" in check.detail and "/bin" in check.detail

    def test_full_path_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", "/app/.venv/bin:/usr/bin:/bin")
        assert preflight._check_path().ok is True


class TestAuthChecks:
    def test_api_key_satisfies_claude(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-xxx")
        assert preflight._check_claude_auth().ok is True

    def test_blank_key_does_not_count(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # An empty value is not a credential; treating it as one is how a
        # missing login turns into a confusing runtime error.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
        monkeypatch.setenv("HOME", str(tmp_path))
        check = preflight._check_claude_auth()
        assert check.ok is False and check.severity == preflight.SEVERITY_OPTIONAL

    def test_mounted_login_satisfies_claude(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        creds = tmp_path / ".claude"
        creds.mkdir()
        (creds / ".credentials.json").write_text("{}")
        assert preflight._check_claude_auth().ok is True

    def test_root_owned_opencode_dir_is_diagnosed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # The real failure: Docker creates a missing bind-mount parent as root,
        # and opencode dies with EACCES creating its own state beside auth.json.
        monkeypatch.setenv("HOME", str(tmp_path))
        share = tmp_path / ".local" / "share" / "opencode"
        share.mkdir(parents=True)
        share.chmod(0o555)
        try:
            check = preflight._check_opencode_auth()
            assert check.ok is False and "not writable" in check.detail
            assert "parent" in check.fix
        finally:
            share.chmod(0o755)

    def test_provider_key_satisfies_opencode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-xxx")
        assert preflight._check_opencode_auth().ok is True


class TestReport:
    def test_required_failure_is_fatal(self) -> None:
        checks = [preflight.Check("db", False, "gone", "fix it")]
        report = preflight.format_report(checks)
        assert "required check(s) failing" in report and "-> fix it" in report

    def test_optional_failure_is_degraded_not_fatal(self) -> None:
        checks = [
            preflight.Check("nix", False, "absent", "seed it", preflight.SEVERITY_OPTIONAL),
            preflight.Check("workspace", True, "ok"),
        ]
        report = preflight.format_report(checks)
        assert "degrades rather than breaking" in report
        assert "required check(s) failing" not in report

    def test_all_pass(self) -> None:
        assert "All checks passed" in preflight.format_report(
            [preflight.Check("workspace", True, "ok")]
        )

    def test_symbols_distinguish_severity(self) -> None:
        assert preflight.Check("a", True, "").symbol == "✓"
        assert preflight.Check("a", False, "").symbol == "✗"
        assert (
            preflight.Check("a", False, "", severity=preflight.SEVERITY_OPTIONAL).symbol == "!"
        )

    def test_run_all_returns_checks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TURNSTONE_DB_URL", "")
        checks = preflight.run_all()
        names = {c.name for c in checks}
        assert {"workspace", "agent CLIs", "PATH"} <= names
