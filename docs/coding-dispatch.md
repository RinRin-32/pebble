# Coding-agent dispatch — setup and operation

Turnstone can hand a coding task to an external agent CLI (Claude Code,
opencode, Codex) running in an isolated git worktree, stream its progress into
whatever channel started it, and return a reviewable diff.

It does not reimplement an agent loop. All three CLIs converge on the same
shape — `(prompt, worktree, model, session) → JSON event stream → (text, diff,
cost)` — so turnstone normalizes them and stays agent-agnostic.

---

## 1. How it fits together

```
Discord / console
      │  dispatch_agent(task)
      ▼
turnstone node ──► agent CLI (subprocess)
      │                  │  edits files
      │                  ▼
      │           /workspace/ws/<ws_id>/     ← this workstream's git worktree
      │                  ▲
      │                  │  checked out from
      └──────────► /workspace/repos/<repo_id>.git   ← bare mirror
```

* **`/workspace` is a Docker volume every node mounts**, so a worktree created
  by one node is visible to whichever node later serves the session. Dispatch
  works cluster-wide with no node pinning.
* **A bare mirror plus `git worktree`** (not a clone per workstream) shares one
  object store, so the Nth workstream on a repo costs a checkout, not a clone.
* **Your local checkouts are never touched.** Turnstone clones its own mirror.

## 2. Where the agent runs: Docker, not your laptop

Dispatch runs **inside the node container**. This is deliberate: anyone who can
reach the bot (in a `/global-link` Discord server, that is every member) can
cause agent-written code to execute. In a container the blast radius is a
throwaway worktree; on your workstation it would be your real repos and
credentials. That containment is also what makes `--permission-mode acceptEdits`
and `--sandbox workspace-write` reasonable defaults — the worktree *is* the
sandbox.

The cost of that choice is the toolchain question in §6.

---

## 3. Install

The node image already ships `claude` and `opencode` (it has node + npm + git).
Verify:

```bash
docker compose exec -w /app node-1 /app/.venv/bin/python \
  -c "from turnstone.core.agents import available_agents; print(available_agents())"
# ['claude', 'opencode']
```

`codex` is **not** installed and its adapter is **unverified** — written to the
documented `codex exec --json` contract and parsing defensively, but never run
against a real binary. Add it to the `npm install -g` line in the `Dockerfile`
if you want it.

## 4. Authentication

You need credentials *inside the container*. Two options; you can mix them.

### Option A — your Claude subscription (no API key)

Claude Pro/Max/Team plans include a monthly Agent SDK credit that covers
`claude -p`. Mount your host login into the nodes via `compose.override.yaml`
(gitignored, auto-merged, the right home for machine-specific paths):

```yaml
services:
  node-1:
    volumes: &agent-volumes
      - turnstone-data:/data
      - ${WORKSPACE_MOUNT:-workspace}:/workspace
      - ${HOME}/.claude:/home/turnstone/.claude
      - ${HOME}/.claude.json:/home/turnstone/.claude.json
      - ${HOME}/.local/share/opencode/auth.json:/home/turnstone/.local/share/opencode/auth.json:ro
  node-2:
    volumes: *agent-volumes
  # ... repeat for every node you run
```

Notes that will save you an hour:

* `~/.claude` is mounted **read-write** because Claude Code refreshes its OAuth
  token in place. A read-only mount works until the token expires, then fails.
* The container runs as **uid 1000**, which matches a typical single-user Linux
  host. If your uid differs, the mount will be unreadable.
* Do **not** put these in `compose.yaml`. If a bind source doesn't exist Docker
  creates a **root-owned directory** in its place, silently breaking the login.
* Credits are per-account and can't be pooled. Anthropic recommends an API key
  for shared production automation.

### Option B — API keys

In `.env`:

```dotenv
ANTHROPIC_API_KEY=sk-ant-...     # Claude Code
OPENROUTER_API_KEY=sk-or-...     # opencode via OpenRouter
```

Leave a key **unset** rather than empty — turnstone drops a blank
`ANTHROPIC_API_KEY` before spawning precisely because an empty value can be
read as "use key auth" and shadow an OAuth login.

### `TURNSTONE_CLAUDE_BARE`

`--bare` skips discovery of hooks, plugins, MCP servers and `CLAUDE.md`, but it
**cannot read OAuth credentials**, so it forces an API key. It is therefore
opt-in, not default. Set `TURNSTONE_CLAUDE_BARE=1` only if strict
reproducibility matters more than subscription billing.

## 5. Use it

**Register a repo** (once, per repo):

```python
storage.create_repo({
    "repo_id": "myproj", "name": "myproj",
    "git_url": "https://github.com/you/myproj.git",
    "default_branch": "main",
})
```

**From a session** (Discord thread, console, coordinator):

```
bind_repo(repo="myproj")           # clones the mirror, checks out this
                                   # workstream's worktree, and points every
                                   # shell/file tool at it
dispatch_agent(task="Add retry with backoff to the HTTP client, with tests")
```

* `bind_repo()` with no argument reports the current binding and lists repos.
* `dispatch_agent(agent=…)` picks a CLI; default is server-configured, else
  whichever is installed.
* `dispatch_agent(continue_session=true)` resumes the agent's own session so a
  follow-up keeps its context.
* Both go through turnstone's **approval gate** — a dispatched agent writes code
  and runs commands.
* The **diff is returned even when the run fails or times out**; a partial
  result is usually still useful.

Cost is tracked per run and feeds the per-user limits. The accounting differs
per CLI and is not interchangeable: opencode reports **incremental per-step**
costs that must be summed (taking the last under-bills ~5×), Claude Code
reports a **whole-run** `total_cost_usd` that must not be summed.

## 6. Toolchains — how this scales

The container needs whatever your repo builds with. The image has
node/npm/git/python; a Go or Rust repo can't run its tests until that toolchain
is present. The 2026 consensus on agent dev environments is
[Dev Containers as the de-facto spec](https://markphelps.me/posts/running-ai-agents-in-devcontainers/)
over an OCI base, with [Devbox and Nix](https://www.devtoolreviews.com/reviews/devbox-vs-dev-containers-vs-nix-2026)
as the declarative alternatives. The recognized trap is that a workflow needing
a specific CUDA version or custom build tool ends up
[fighting the sandbox instead of using it](https://blog.arcjet.com/from-devcontainers-to-vms-parallel-dev-environments-for-ai-agents/).

A sane ladder, cheapest first:

1. **Fat base image** *(where turnstone is today)* — add runtimes to the
   `Dockerfile`. Fine for 1–3 languages; becomes a slow, bloated image beyond
   that, and every repo pays for every other repo's dependencies.
2. **Per-repo image** — add an `image` column to `repos` and run dispatch in
   that image. Each repo pays only for itself. This is the natural next step and
   the schema is already close.
3. **Honor the repo's `.devcontainer/devcontainer.json`** — the repo declares
   its own toolchain, which is what makes this scale across many repos without
   turnstone knowing anything about them. Requires running the agent in a
   sibling container (Docker socket) rather than in-process.
4. **Nix/Devbox per worktree** — declarative and no Docker-in-Docker, at the
   cost of every repo needing a Nix expression.

Steps 2–4 all require dispatch to run in a *separate container* from the node,
which is the real architectural fork. Until then, prefer repos whose tests run
on the base image, and treat the agent's ability to *verify* its work as the
constraint that decides whether a repo is dispatchable.

## 7. Troubleshooting

| Symptom | Cause |
|---|---|
| `Not logged in · Please run /login` | Usually **not** auth. The child's `PATH` lacked `/usr/bin`/`/bin` so the CLI couldn't spawn its helpers. Fixed in `run_agent`; if you see it again, check the mount's ownership (uid 1000). |
| `no repo bound` | Call `bind_repo` first. |
| `<agent> is not installed on this node` | The CLI isn't in the image; check `available_agents()`. |
| `could not lock config file` | Concurrent worktree creation. Fixed with `--no-track` + an flock on the mirror; if it recurs, a stale `turnstone-worktree.lock` in the mirror. |
| Diff full of `__pycache__` / `node_modules` | Build artifacts are excluded via the mirror's `info/exclude`; add patterns to `_LOCAL_EXCLUDES` in `workspace.py`. |
| Agent edits nothing | Check the diff *and* the stat — it may have only run commands. |

## 8. Knowledge base

`dispatch_agent`'s sibling is the `kb` tool: an Obsidian-compatible markdown
vault at `/workspace/kb` (YAML frontmatter + `[[wikilinks]]`), with the link
graph indexed in Postgres for traversal.

To browse it in Obsidian, point the workspace at a host directory in `.env`:

```dotenv
WORKSPACE_MOUNT=/home/you/turnstone-workspace
```

then open `…/turnstone-workspace/kb` as a vault. The files turnstone writes are
the same files Obsidian reads — no export step, no lock-in. Changing this starts
a **fresh volume**, so existing checkouts and notes don't carry over.

`kb` actions: `search`, `read`, `write`, `append` (grows a running note — the
iterative-research path), `links` (outgoing/backlinks/dangling), and `graph`
(hubs, orphans, and the **frontier**: notes that are linked to but not yet
written, i.e. where research should go next).
