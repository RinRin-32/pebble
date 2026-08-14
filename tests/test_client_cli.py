"""Tests for the remote client CLI.

Config resolution and argument wiring are pure, so they are tested directly;
anything touching the network is exercised through a stub client.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pebble import client_cli as cli


class TestConfig:
    def test_env_overrides_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = tmp_path / "client.toml"
        cli.save_config(cli.ClientConfig(url="http://file", token="from-file"), path)
        monkeypatch.setenv("PEBBLE_URL", "http://env")
        monkeypatch.setenv("PEBBLE_TOKEN", "from-env")
        cfg = cli.load_config(path)
        # CI passes credentials in the environment and must never need to write
        # them to disk.
        assert cfg.url == "http://env" and cfg.token == "from-env"

    def test_roundtrip(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PEBBLE_URL", raising=False)
        monkeypatch.delenv("PEBBLE_TOKEN", raising=False)
        path = tmp_path / "client.toml"
        cli.save_config(cli.ClientConfig(url="https://host:8443", token="ts_abc"), path)
        cfg = cli.load_config(path)
        assert cfg.url == "https://host:8443" and cfg.token == "ts_abc"
        assert cfg.configured is True

    def test_token_file_is_not_world_readable(self, tmp_path: Path) -> None:
        path = tmp_path / "client.toml"
        cli.save_config(cli.ClientConfig(token="ts_secret"), path)
        assert path.stat().st_mode & 0o077 == 0, "an API token must not be group/world readable"

    def test_quotes_are_escaped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PEBBLE_TOKEN", raising=False)
        monkeypatch.delenv("PEBBLE_URL", raising=False)
        path = tmp_path / "client.toml"
        cli.save_config(cli.ClientConfig(token='we"ird\\value'), path)
        assert cli.load_config(path).token == 'we"ird\\value'

    def test_missing_file_is_unconfigured(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("PEBBLE_TOKEN", raising=False)
        assert cli.load_config(tmp_path / "nope.toml").configured is False


class _StubClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.created: dict[str, Any] = {}
        self.approved: list[dict[str, Any]] = []

    def __enter__(self) -> _StubClient:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def route_create_workstream(self, **kw: Any) -> dict[str, str]:
        self.created = kw
        return {"ws_id": "ws-abc123456789"}

    def route_send(self, message: str, ws_id: str) -> None:
        self.sent.append((ws_id, message))

    def route_approve(self, **kw: Any) -> None:
        self.approved.append(kw)

    def workstreams(self) -> list[dict[str, str]]:
        return [{"ws_id": "ws-1", "state": "idle", "kind": "interactive", "name": "demo"}]


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> _StubClient:
    client = _StubClient()
    monkeypatch.setattr(cli, "_client", lambda cfg: client)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: cli.ClientConfig(token="t"))
    return client


class TestDispatch:
    def test_defaults_to_the_dispatcher_persona(self, stub: _StubClient, capsys) -> None:
        args = cli.build_parser().parse_args(["dispatch", "--repo", "kokoro-go", "fix the test"])
        args.json = False
        cli.cmd_dispatch(args)
        # Delegation is the default, so a CLI user does not get a child that
        # quietly edits files itself.
        assert stub.created["persona"] == "dispatcher"

    def test_brief_names_the_repo_and_the_chain(self, stub: _StubClient) -> None:
        args = cli.build_parser().parse_args(["dispatch", "--repo", "myrepo", "add tests"])
        args.json = False
        cli.cmd_dispatch(args)
        _, message = stub.sent[0]
        assert 'repo="myrepo"' in message
        for step in ("bind_repo", "setup_env", "dispatch_agent"):
            assert step in message, f"the brief must name {step}"
        assert "add tests" in message

    def test_persona_override(self, stub: _StubClient) -> None:
        args = cli.build_parser().parse_args(
            ["dispatch", "--repo", "r", "t", "--persona", "engineer"]
        )
        args.json = False
        cli.cmd_dispatch(args)
        assert stub.created["persona"] == "engineer"


class TestApprove:
    def test_approve_and_reject(self, stub: _StubClient) -> None:
        p = cli.build_parser()
        a = p.parse_args(["approve", "ws-1"]); a.json = False
        cli.cmd_approve(a)
        r = p.parse_args(["approve", "ws-1", "--reject"]); r.json = False
        cli.cmd_approve(r)
        assert stub.approved[0]["approved"] is True
        assert stub.approved[1]["approved"] is False


class TestGuards:
    def test_unconfigured_gives_actionable_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cli, "load_config", lambda *a, **k: cli.ClientConfig())
        args = cli.build_parser().parse_args(["ws", "list"])
        args.json = False
        with pytest.raises(SystemExit) as exc:
            cli.cmd_ws_list(args)
        assert "turnstone-client login" in str(exc.value)

    def test_parser_requires_a_command(self) -> None:
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args([])

    def test_dispatch_requires_repo(self) -> None:
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["dispatch", "just a task"])


class TestWorkstreamFieldMapping:
    """The overview endpoint returns `id`/`node`, the storage layer ws_id/node_id.

    Getting this wrong printed blank columns and made every row unactionable —
    you could see a workstream but not address it.
    """

    def _rows(self, monkeypatch, rows):
        class _C:
            def __enter__(self): return self
            def __exit__(self, *a): return None
            def workstreams(self): return rows
        monkeypatch.setattr(cli, "_client", lambda cfg: _C())
        monkeypatch.setattr(cli, "load_config", lambda *a, **k: cli.ClientConfig(token="t"))
        args = cli.build_parser().parse_args(["ws", "list"])
        args.json = False
        return args

    def test_api_shape_renders_the_id(self, monkeypatch, capsys) -> None:
        args = self._rows(monkeypatch, [
            {"id": "e18ed14fe462401d", "name": "Spawn Bob", "state": "idle", "node": "console"}
        ])
        cli.cmd_ws_list(args)
        out = capsys.readouterr().out
        assert "e18ed14fe462" in out and "console" in out and "Spawn Bob" in out

    def test_storage_shape_also_renders(self, monkeypatch, capsys) -> None:
        args = self._rows(monkeypatch, [
            {"ws_id": "abc123456789ff", "name": "Other", "state": "running", "node_id": "node-1"}
        ])
        cli.cmd_ws_list(args)
        out = capsys.readouterr().out
        assert "abc123456789" in out and "node-1" in out

    def test_empty_list_is_not_an_error(self, monkeypatch, capsys) -> None:
        args = self._rows(monkeypatch, [])
        assert cli.cmd_ws_list(args) == 0
        assert "No workstreams" in capsys.readouterr().out
