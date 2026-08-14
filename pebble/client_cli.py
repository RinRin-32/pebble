"""``pebble-client`` — drive a remote pebble from your own machine.

Everything this exposes was previously reachable only by opening a shell inside
a container and calling Python by hand, which is not a workflow anyone should
have to live with.  This is a thin client over the console HTTP API: point it at
a host, give it an API token once, and the fleet is usable from a laptop.

Being a *remote* client from the first commit is deliberate.  A tool that only
works next to the database grows assumptions that make it painful to move
later; this one has never had that option.

Credentials live in ``~/.config/pebble/client.toml`` (mode 0600), matching
the config location the rest of pebble already uses.  ``PEBBLE_URL`` and
``PEBBLE_TOKEN`` override the file, so CI can pass them in the environment
without writing anything to disk.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_DIR = Path("~/.config/pebble").expanduser()
#: Pre-rename location.  A laptop that ran ``pebble-client login`` should not
#: have to log in again just because the project changed names.
LEGACY_CONFIG_PATH = Path("~/.config/turnstone/client.toml").expanduser()


def _config_path() -> Path:
    current = CONFIG_DIR / "client.toml"
    if not current.is_file() and LEGACY_CONFIG_PATH.is_file():
        return LEGACY_CONFIG_PATH
    return current


CONFIG_PATH = _config_path()

DEFAULT_URL = "http://localhost:8090"


@dataclass
class ClientConfig:
    url: str = DEFAULT_URL
    token: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.token)


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def load_config(path: Path = CONFIG_PATH) -> ClientConfig:
    """Environment beats file, so a CI run never needs to write credentials."""
    cfg = ClientConfig()
    if path.is_file():
        try:
            import tomllib

            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        client = data.get("client", data) if isinstance(data, dict) else {}
        cfg.url = str(client.get("url") or cfg.url)
        cfg.token = str(client.get("token") or "")
    cfg.url = os.environ.get("PEBBLE_URL") or cfg.url
    cfg.token = os.environ.get("PEBBLE_TOKEN") or cfg.token
    return cfg


def save_config(cfg: ClientConfig, path: Path = CONFIG_PATH) -> None:
    """Persist credentials 0600 — this file holds an API token."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f"[client]\nurl = {_quote(cfg.url)}\ntoken = {_quote(cfg.token)}\n"
    path.write_text(body, encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def _client(cfg: ClientConfig) -> Any:
    from pebble.sdk.console import TurnstoneConsole

    return TurnstoneConsole(cfg.url, token=cfg.token)


def _require(cfg: ClientConfig) -> None:
    if not cfg.configured:
        raise SystemExit(
            "No API token configured.\n"
            "  pebble-client login --url https://host:8443 --token ts_...\n"
            "or set PEBBLE_URL and PEBBLE_TOKEN."
        )


def _emit(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))


# -- commands ---------------------------------------------------------------


def cmd_login(args: argparse.Namespace) -> int:
    cfg = load_config()
    cfg.url = args.url or cfg.url
    cfg.token = args.token or cfg.token
    if not cfg.token:
        raise SystemExit("--token is required (create one with: pebble-admin create-token)")
    with _client(cfg) as client:
        try:
            status = client.auth_status()
        except Exception as exc:
            raise SystemExit(f"Could not reach {cfg.url}: {exc}") from exc
    save_config(cfg)
    user = getattr(status, "user_id", None) or getattr(status, "username", None) or "authenticated"
    print(f"Saved {CONFIG_PATH} (0600)\nConnected to {cfg.url} as {user}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    _require(cfg)
    with _client(cfg) as client:
        overview = client.overview()
    data = overview if isinstance(overview, dict) else getattr(overview, "__dict__", {})
    _emit(data, args.json)
    if not args.json:
        print(f"{cfg.url}")
        for key in ("nodes", "workstreams", "healthy"):
            if key in data:
                print(f"  {key}: {data[key]}")
    return 0


def cmd_ws_list(args: argparse.Namespace) -> int:
    cfg = load_config()
    _require(cfg)
    with _client(cfg) as client:
        rows = client.workstreams()
    items = rows if isinstance(rows, list) else getattr(rows, "workstreams", []) or []
    norm = [i if isinstance(i, dict) else getattr(i, "__dict__", {}) for i in items]
    _emit(norm, args.json)
    if not args.json:
        if not norm:
            print("No workstreams.")
            return 0
        for w in norm[: args.limit]:
            # The overview endpoint calls these `id` and `node`; the storage
            # layer calls them ws_id and node_id. Accept either so the CLI does
            # not silently print blank columns if a field is renamed.
            ws_id = str(w.get("id") or w.get("ws_id") or "")[:12]
            node = str(w.get("node") or w.get("node_id") or "")
            print(
                f"  {ws_id:12}  {str(w.get('state', '')):10} {node:9} "
                f"{str(w.get('name') or w.get('title') or '')[:40]}"
            )
    return 0


def cmd_dispatch(args: argparse.Namespace) -> int:
    """Create a workstream and instruct it to bind a repo and delegate.

    The tools are model-invoked, so this sends the instruction rather than
    calling ``dispatch_agent`` directly — the brief is explicit precisely
    because a vague one is what makes a model edit files itself instead of
    delegating.
    """
    cfg = load_config()
    _require(cfg)
    brief = (
        f"Bind this workstream to the repository named {args.repo} by calling "
        f'bind_repo with repo="{args.repo}". Then call setup_env with '
        f'action="use". Then use dispatch_agent to carry out this task:\n\n'
        f"{args.task}\n\nReport the resulting diff."
    )
    with _client(cfg) as client:
        created = client.route_create_workstream(
            name=args.name or f"dispatch: {args.task[:40]}",
            model=args.model or "",
            persona=args.persona or "dispatcher",
            client_type="cli",
        )
        ws_id = created.get("ws_id") if isinstance(created, dict) else getattr(created, "ws_id", "")
        if not ws_id:
            raise SystemExit(f"Workstream creation returned no ws_id: {created!r}")
        client.route_send(brief, ws_id)
    _emit({"ws_id": ws_id}, args.json)
    if not args.json:
        print(f"Dispatched to {ws_id[:12]} (persona: {args.persona or 'dispatcher'})")
        print("  follow:  pebble-client ws list")
        print(f"  approve: pebble-client approve {ws_id[:12]}")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    cfg = load_config()
    _require(cfg)
    with _client(cfg) as client:
        client.route_send(args.message, args.ws_id)
    print(f"Sent to {args.ws_id[:12]}")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    cfg = load_config()
    _require(cfg)
    with _client(cfg) as client:
        client.route_approve(ws_id=args.ws_id, approved=not args.reject, always=args.always)
    print(f"{'Rejected' if args.reject else 'Approved'} {args.ws_id[:12]}")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    cfg = load_config()
    _require(cfg)
    with _client(cfg) as client:
        data = client.list_models()
    raw = data if isinstance(data, dict) else getattr(data, "__dict__", {})
    models = raw.get("models", [])
    norm = [m if isinstance(m, dict) else getattr(m, "__dict__", {}) for m in models]
    _emit(norm, args.json)
    if not args.json:
        for m in norm:
            print(f"  {m.get('alias', ''):26} {m.get('model', '')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pebble-client",
        description="Drive a remote pebble cluster from your own machine.",
    )
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="Save the host URL and API token")
    p_login.add_argument("--url", default="", help=f"console URL (default {DEFAULT_URL})")
    p_login.add_argument("--token", default="", help="API token (pebble-admin create-token)")
    p_login.set_defaults(func=cmd_login)

    sub.add_parser("status", help="Cluster overview").set_defaults(func=cmd_status)

    p_ws = sub.add_parser("ws", help="Workstreams")
    ws_sub = p_ws.add_subparsers(dest="ws_command", required=True)
    p_ws_list = ws_sub.add_parser("list", help="List workstreams")
    p_ws_list.add_argument("--limit", type=int, default=20)
    p_ws_list.set_defaults(func=cmd_ws_list)

    p_disp = sub.add_parser("dispatch", help="Dispatch a coding task to a repo")
    p_disp.add_argument("task", help="what the agent should do")
    p_disp.add_argument("--repo", required=True, help="registered repo name")
    p_disp.add_argument("--model", default="", help="model alias")
    p_disp.add_argument(
        "--persona",
        default="dispatcher",
        help="child persona; 'dispatcher' delegates instead of editing directly",
    )
    p_disp.add_argument("--name", default="", help="workstream name")
    p_disp.set_defaults(func=cmd_dispatch)

    p_send = sub.add_parser("send", help="Send a message to a workstream")
    p_send.add_argument("ws_id")
    p_send.add_argument("message")
    p_send.set_defaults(func=cmd_send)

    p_appr = sub.add_parser("approve", help="Approve a pending tool call")
    p_appr.add_argument("ws_id")
    p_appr.add_argument("--reject", action="store_true")
    p_appr.add_argument("--always", action="store_true", help="approve this tool from now on")
    p_appr.set_defaults(func=cmd_approve)

    sub.add_parser("models", help="List available models").set_defaults(func=cmd_models)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not hasattr(args, "json"):
        args.json = False
    try:
        return int(args.func(args) or 0)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
