"""Git identity and push credentials for dispatched coding work.

The interesting tests here are the negative ones. The thing running these git
commands is a coding agent acting on a model's plan, so "which host gets the
operator's token" is not a theoretical question: if the agent adds a remote
pointing somewhere else, the token must not follow it there.
"""

from __future__ import annotations

import subprocess

import pytest

import pebble.core.git_identity as gi

_TOKEN = "SECRET-TOKEN-VALUE-123456"


def _env(monkeypatch: pytest.MonkeyPatch, **over: str) -> dict[str, str]:
    monkeypatch.setenv("PEBBLE_GIT_TOKEN", over.pop("token", _TOKEN))
    monkeypatch.setenv("PEBBLE_GIT_HOST", over.pop("host", "github.com"))
    for k, v in over.items():
        monkeypatch.setenv(k, v)
    gi._askpass_cache = None
    return gi.git_env()


class TestIdentity:
    def test_commits_are_authored_as_a_bot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Not the operator: `git log` should distinguish agent work from a
        # person's, which is the whole point of a separate identity.
        monkeypatch.delenv("PEBBLE_GIT_AUTHOR_NAME", raising=False)
        monkeypatch.delenv("PEBBLE_GIT_AUTHOR_EMAIL", raising=False)
        env = _env(monkeypatch)
        assert env["GIT_AUTHOR_NAME"] == gi.DEFAULT_AUTHOR_NAME
        assert env["GIT_COMMITTER_NAME"] == gi.DEFAULT_AUTHOR_NAME
        assert env["GIT_AUTHOR_EMAIL"] == env["GIT_COMMITTER_EMAIL"]

    def test_identity_is_overridable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        env = _env(
            monkeypatch,
            PEBBLE_GIT_AUTHOR_NAME="fleet-bot",
            PEBBLE_GIT_AUTHOR_EMAIL="bot@example.com",
        )
        assert env["GIT_AUTHOR_NAME"] == "fleet-bot"
        assert env["GIT_AUTHOR_EMAIL"] == "bot@example.com"


class TestCredentials:
    def test_no_token_means_no_askpass_and_fail_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PEBBLE_GIT_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        env = gi.git_env()
        assert "GIT_ASKPASS" not in env
        assert gi.has_push_credentials() is False
        # Still disabled: git must fail rather than hang a tool call on a
        # prompt nobody can answer.
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_github_token_is_accepted_as_a_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PEBBLE_GIT_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", _TOKEN)
        assert gi.git_token() == _TOKEN

    def test_agent_env_carries_gh_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The agent CLIs reach for `gh pr create` on their own; gh reads
        # GH_TOKEN with no login step, so one token serves push and PR alike.
        _env(monkeypatch)
        assert gi.agent_env()["GH_TOKEN"] == _TOKEN

    def test_token_never_lands_in_the_askpass_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The helper reads the token from the environment when git calls it;
        # writing it into the script would put a secret on disk.
        _env(monkeypatch)
        path = gi._askpass_path()
        assert path is not None
        with open(path) as fh:
            assert _TOKEN not in fh.read()


class TestHostScoping:
    """The token is offered to exactly one host, compared whole.

    A substring check would accept ``github.com.evil.net`` — one lookalike
    domain away from handing an attacker push rights.
    """

    def _ask(self, monkeypatch: pytest.MonkeyPatch, prompt: str) -> tuple[int, str]:
        env = _env(monkeypatch)
        path = gi._askpass_path()
        assert path is not None
        proc = subprocess.run(  # noqa: S603 - fixed path, list args, no shell
            [path, prompt], capture_output=True, text=True, env=env
        )
        return proc.returncode, (proc.stdout or "").strip()

    def test_configured_host_gets_the_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code, out = self._ask(monkeypatch, "Password for 'https://x-access-token@github.com': ")
        assert code == 0 and out == _TOKEN

    def test_username_prompt_gets_the_placeholder_not_the_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, out = self._ask(monkeypatch, "Username for 'https://github.com': ")
        assert code == 0 and out == gi.DEFAULT_USER and _TOKEN not in out

    def test_port_is_ignored_when_matching(self, monkeypatch: pytest.MonkeyPatch) -> None:
        code, out = self._ask(monkeypatch, "Password for 'https://u@github.com:443': ")
        assert code == 0 and out == _TOKEN

    @pytest.mark.parametrize(  # type: ignore[misc]
        "prompt",
        [
            "Password for 'https://evil.example.com': ",
            "Password for 'https://github.com.evil.net': ",  # lookalike suffix
            "Password for 'https://notgithub.com': ",  # lookalike prefix
            "Password for 'https://evil.net/github.com': ",  # host in the path
        ],
    )
    def test_other_hosts_get_nothing(self, monkeypatch: pytest.MonkeyPatch, prompt: str) -> None:
        code, out = self._ask(monkeypatch, prompt)
        assert code != 0
        assert _TOKEN not in out

    def test_unset_host_refuses_everything(self, monkeypatch: pytest.MonkeyPatch) -> None:
        env = _env(monkeypatch)
        path = gi._askpass_path()
        assert path is not None
        env["PEBBLE_GIT_HOST"] = ""
        proc = subprocess.run(  # noqa: S603 - fixed path, list args, no shell
            [path, "Password for 'https://github.com': "],
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc.returncode != 0 and _TOKEN not in (proc.stdout or "")


class TestRedaction:
    def test_token_is_scrubbed_from_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _env(monkeypatch)
        assert _TOKEN not in gi.redact(f"remote rejected: {_TOKEN} is invalid")

    def test_no_token_configured_is_a_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PEBBLE_GIT_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        assert gi.redact("nothing to hide") == "nothing to hide"


class TestTokenIdentification:
    """Who does this token belong to, and how far does it reach?

    Asked at link time so a commit carries the operator's own identity, and so
    the console can say out loud what it was just handed — a classic PAT with
    ``delete_repo`` looks exactly like a scoped one in a password box.
    """

    def test_wide_scopes_are_surfaced(self) -> None:
        scopes = "repo, delete_repo, admin:org, gist, workflow"
        assert gi.wide_scopes(scopes) == [
            "admin:org",
            "delete_repo",
            "workflow",
        ]

    def test_ordinary_scopes_raise_nothing(self) -> None:
        assert gi.wide_scopes("repo, read:user") == []

    def test_fine_grained_tokens_report_no_scopes(self) -> None:
        # GitHub sends the x-oauth-scopes header only for classic PATs, so an
        # empty value is the normal case for the token we recommend.
        assert gi.wide_scopes("") == []

    def test_identification_failure_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A token that cannot be identified may still push fine; refusing to
        # store it would be worse than not knowing the login.
        def boom(*a: object, **k: object) -> None:
            raise OSError("network down")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        out = gi.identify_token("ghp_whatever")
        assert out["login"] == "" and out["error"]
