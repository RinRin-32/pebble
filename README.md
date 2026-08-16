# Pebble

[![CI](https://github.com/RinRin-32/pebble/actions/workflows/ci.yml/badge.svg)](https://github.com/RinRin-32/pebble/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

Self-hosted, local-first orchestration for tool-using AI agents — and a place to
put the work they produce. Give LLMs real tools (shell, files, search, web),
hand coding tasks to agent CLIs in isolated git worktrees, and keep what was
learned in a knowledge vault that any machine can reach. Your code, your models,
your data stay on hardware you control: no telemetry, no phone-home.

> **This is a heavily customised personal fork of
> [Turnstone](https://github.com/turnstonelabs/turnstone)** (Patrick Buckley,
> Apache-2.0), built around one person's workflow rather than as a general
> release. It adds agentic coding dispatch, a knowledge vault exposed over MCP,
> per-user access and git credentials, and Nix toolchains.
>
> **Looking for the real project? Go to
> [turnstonelabs/turnstone](https://github.com/turnstonelabs/turnstone).** That
> is the maintained one, with releases, documentation and support behind it.
> This fork makes no such promises and may change in ways that suit its author
> and nobody else.

<p align="center">
  <img src="docs/diagrams/architecture-overview.svg" alt="System architecture" width="960"/>
</p>

## What it does

**Orchestration.** LLMs get tools — shell, files, search, web, planning — across
multi-turn conversations where the model investigates, acts, and reports. A
coordinator can fan work out to child workstreams and collect the results.

**Agentic coding.** `dispatch_agent` hands a task to an external coding agent
(Claude Code, opencode, or Codex) running in this workstream's own git worktree,
with a Nix toolchain provisioned so it can build and test rather than only edit.
`publish_work` then commits, pushes the workstream's branch, and opens a pull
request — never to the default branch, and behind an approval gate.

**A knowledge vault that outlives the session.** Agents record measured results
as Obsidian-format markdown (frontmatter + `[[wikilinks]]`), and the console
draws the graph. Dangling links are kept on purpose: a name someone reached for
and nobody has written is the research frontier.

**Reachable from anywhere.** Pebble is also an **MCP server** — a Claude Code
session on your laptop, working in an unrelated repository, reads and writes the
same vault the dispatched agents use.

- **Local-first & private** — runs on hardware you control. Point it at local
  models (vLLM, llama.cpp) or APIs you hold the keys to.
- **Bring your own models** — OpenAI-compatible, Anthropic Messages, and Google
  Gemini, mixed freely per role.
- **Interactive sessions** — terminal CLI, browser UI, or Discord/Slack, with
  parallel workstreams.
- **Cluster dashboard** — every node, workstream, coding job and note, live.
- **Intent validation** — an LLM judge grades every tool call with a risk
  assessment and evidence before it runs.
- **Per-user authority** — which models, which personas, whether they may
  dispatch agents at all, and whose git credentials a push spends.

## Quickstart

Pebble is not published to PyPI; install it from this repository.

```bash
git clone https://github.com/RinRin-32/pebble.git
cd pebble
docker compose up -d
```

That builds one image and brings up a local cluster — PostgreSQL, console,
Caddy, channel gateway and 6 server nodes (10 with `--profile extra`) — with no
`.env` required; it ships insecure dev defaults. Open the dashboard at
<https://localhost:8443> (Caddy serves TLS with its own local CA — trust it
once). Nodes boot without an LLM; add model backends from the console.

Or let the installer do it — it autodetects Ubuntu/Debian, Fedora/RHEL, Arch and
WSL, installs git + Docker if missing, generates secrets, and starts the stack:

```bash
curl -fsSL https://raw.githubusercontent.com/RinRin-32/pebble/main/run.sh | bash
```

Without Docker:

```bash
pip install -e .            # add [discord,slack] for channel gateways
pebble --base-url http://localhost:8000/v1        # terminal REPL
pebble-server --port 8080 --base-url http://localhost:8000/v1   # web UI + API
pebble-console --port 8090                        # cluster dashboard
```

See [QUICKSTART.md](QUICKSTART.md) for the walkthrough and
[docs/docker.md](docs/docker.md) for Docker configuration.

### Configuration

Environment variables are `PEBBLE_*`; the pre-rename `TURNSTONE_*` spellings are
still honoured, so an existing deployment keeps working. Copy
[`.env.example`](.env.example) for the annotated list. Two worth knowing:

| Variable | Why |
|---|---|
| `PEBBLE_SECRET_KEY` | Encrypts per-user secrets (git tokens). Without it, linking a token is refused rather than stored in the clear. |
| `PEBBLE_GIT_TOKEN` | Instance-level git push credential. Prefer per-user tokens linked in the console — see [Coding dispatch](docs/coding-dispatch.md). |
| `PEBBLE_MCP_ALLOWED_HOSTS` | Hostnames the `/mcp` endpoint answers to. Unset means localhost only, and a remote client is refused with a bare `421`. |

### Connecting a laptop over MCP

A session on another machine — working in a repository pebble has never seen —
can read and write the same vault the dispatched agents use.

```bash
claude mcp add --transport http pebble https://<your-console>:<port>/mcp \
  --header "Authorization: Bearer ts_..."
```

Reading: `kb_search` (scope with `repo`), `kb_read`, `kb_graph`, `kb_repos`,
`kb_plans`, `kb_janitor`, `kb_skills_pull`, `kb_skills_deletion_review`.
Writing: `kb_write`, `kb_record_experiment`, `kb_delete`, `kb_rename`,
`kb_skills_archive`, `kb_skills_hook`, and the `kb_interview*` / `kb_plan*`
conversations.

It **records** results; it never executes commands remotely — your session runs
its own, then writes down what happened.

A `read` token can read; the writing tools require `write`, checked per tool.
That check lives in the tools rather than on the route because one path serves
the whole mount, so the route-level rule resolves everything here to `read` —
which for a while meant a read-only token could write and delete. Fixed, and
pinned by a test that asserts every tool is in exactly one of the two lists
above.

Two things bite when the console is behind a proxy, and neither error says so:

- **`421 Misdirected Request`** — the MCP SDK trusts only `localhost` by
  default, so list the name you actually connect to:
  `PEBBLE_MCP_ALLOWED_HOSTS=box.tailnet.ts.net:9443`. The check is worth
  keeping; see [`.env.example`](.env.example) for why and for the `*` escape
  hatch.
- **Get the port right.** The bundled Caddy publishes TLS on **8443**, not 443,
  and anything else on the host may already own those. With
  [Tailscale](https://tailscale.com), the tidiest route is a port nothing else
  claims, which also gets you a real certificate rather than Caddy's local CA:

  ```bash
  sudo tailscale serve --bg --https=9443 http://127.0.0.1:8090
  ```

  That points at the console directly, bypassing Caddy entirely.

### Programmatic (SDK)

```python
from pebble.sdk import TurnstoneServer

with TurnstoneServer("http://localhost:8080", token="ts_xxx") as client:
    ws = client.create_workstream(name="demo")
    result = client.send_and_wait("Analyze the error logs", ws.ws_id, auto_approve=True)
    print(result.content)
```

## Tools

37 built-in tools — shell, files, search, web, memory, notifications,
sub-agents, plus the coding chain (`bind_repo`, `setup_env`, `dispatch_agent`,
`publish_work`) and the knowledge base (`kb`) — and external tools via
[MCP](https://modelcontextprotocol.io/). See [docs/tools.md](docs/tools.md).

## Architecture

**Single-node**: Client → Server (direct HTTP + SSE). No dependencies beyond the
database.

**Multi-node**: Client → Console (rendezvous routing proxy) → Server nodes. The
console picks the target node per workstream via rendezvous (HRW) hashing over
the live service registry — a pure function of `(ws_id, live_nodes)`, no stored
bucket state, deterministic across readers. A node joining or dropping re-routes
only the keys that score highest on it.

| Component | Purpose |
|-----------|---------|
| `pebble` | Terminal CLI (REPL) |
| `pebble-server` | Web UI + REST API + SSE events |
| `pebble-console` | Cluster dashboard, routing proxy, admin panel, MCP server |
| `pebble-client` | Drive a remote cluster from your own machine |
| `pebble-channel` | Channel gateway (Discord and Slack adapters) |
| `pebble-admin` | User/token management CLI |
| `pebble-eval` | Headless measurement — scores tool-use against expected actions |
| `pebble-optimizer` | Prompt/tool optimizer (UCB self-modify loop over the eval substrate) |
| `pebble-doctor` | LLM-backed cluster diagnostics |

The pre-rename `turnstone-*` command names still work.

### Diagrams

UML diagrams in [`docs/diagrams/`](docs/diagrams/):

| Diagram | Description |
|---------|-------------|
| [System Context](docs/diagrams/png/01-system-context.png) | Components and external dependencies |
| [Package Structure](docs/diagrams/png/02-package-structure.png) | Python modules and dependency graph |
| [Core Engine](docs/diagrams/png/03-core-engine-classes.png) | SessionUI, ChatSession, LLMProvider |
| [Conversation Turn](docs/diagrams/png/04-conversation-turn.png) | Message lifecycle through the engine |
| [Tool Pipeline](docs/diagrams/png/05-tool-pipeline.png) | Prepare / approve / execute |
| [Workstream States](docs/diagrams/png/09-workstream-states.png) | State machine transitions |
| [Console Data Flow](docs/diagrams/png/11-console-data-flow.png) | Dashboard data collection |
| [Deployment](docs/diagrams/png/12-deployment.png) | Docker Compose topology |
| [Auth](docs/diagrams/png/15-auth-architecture.png) | JWT, scopes, login flows |
| [Channels](docs/diagrams/png/16-channel-architecture.png) | Discord / Slack adapters + routing |
| [Judge](docs/diagrams/png/22-judge-architecture.png) | Intent validation pipeline |
| [OIDC](docs/diagrams/png/25-oidc-architecture.png) | SSO authorization code flow |

## Documentation

| Topic | Link |
|-------|------|
| Coding dispatch, worktrees, Nix envs, git credentials | [docs/coding-dispatch.md](docs/coding-dispatch.md) |
| Configuration reference | [docs/settings.md](docs/settings.md) |
| API reference | [docs/api-reference.md](docs/api-reference.md) |
| Docker deployment | [docs/docker.md](docs/docker.md) |
| Intent validation (judge) | [docs/judge.md](docs/judge.md) |
| Governance & RBAC | [docs/governance.md](docs/governance.md) |
| Personas | [docs/personas.md](docs/personas.md) |
| OIDC SSO | [docs/oidc.md](docs/oidc.md) |
| TLS / mTLS | [docs/tls.md](docs/tls.md) |
| Channel integrations | [docs/channels.md](docs/channels.md) |
| Console dashboard | [docs/console.md](docs/console.md) |
| Eval harness | [docs/eval.md](docs/eval.md) |
| Tools reference | [docs/tools.md](docs/tools.md) |
| MCP integration | [docs/mcp-registry.md](docs/mcp-registry.md) |

## Requirements

- Python 3.11+
- An OpenAI-compatible API endpoint, Anthropic API key, or Google Gemini API key
- For agentic coding: Docker (the image carries `git`, `gh`, Nix and the agent
  CLIs), plus credentials for whichever agent you dispatch
- Optional: Discord / Slack integrations (`pip install -e ".[discord,slack]"`)
- [Git LFS](https://git-lfs.com/) for cloning (diagram PNGs)

## Upstream

Pebble is a fork of [turnstonelabs/turnstone](https://github.com/turnstonelabs/turnstone),
created by Patrick Buckley and licensed Apache-2.0. The orchestration core,
console, judge, channels, RBAC and SSO are upstream's work; see
[NOTICE](NOTICE) and [LICENSE](LICENSE).

If you are evaluating this for real use, use upstream instead — it is the
maintained project, and questions, issues and community belong there.

## License

Apache-2.0 — see [LICENSE](LICENSE).
