# Coding-agent dispatch — setup and operation

Turnstone can hand a coding task to an external agent CLI (Claude Code,
opencode, Codex) running in an isolated git worktree, stream its progress into
whatever channel started it, and return a reviewable diff.

It does not reimplement an agent loop. All three CLIs converge on the same
shape — `(prompt, worktree, model, session) → JSON event stream → (text, diff,
cost)` — so turnstone normalizes them and stays agent-agnostic.

---

## 0. Setup (start here)

`docker compose up -d` brings up the cluster, but dispatch has extra runtime
requirements that a bare clone does not satisfy. **Two setup files are
gitignored**, so a fresh clone has neither:

```bash
cp compose.override.yaml.example compose.override.yaml   # host agent logins
cp .env.example .env 2>/dev/null || touch .env           # keys and settings
docker compose up -d
```

Then check what you actually got — every capability reports itself, with the
fix attached:

```bash
docker compose exec node-1 /app/.venv/bin/python -m turnstone.core.preflight
```

```
  ✓ workspace          /workspace writable and mounted
  ✓ agent CLIs         claude, opencode (absent: codex)
  ✓ claude auth        subscription login at /home/turnstone/.claude/.credentials.json
  ✓ opencode auth      login at /home/turnstone/.local/share/opencode/auth.json
  ✓ nix                /nix/var/nix/profiles/default/bin/nix
  ✓ codegraph          installed
  ✓ PATH               system directories present
  ✓ repos registered   bobthesumo, kokoro-go
  ✓ models configured  deepseek-v4-or
```

A `!` line is an optional capability that is off — dispatch degrades rather than
breaking (no Nix means the base image's runtimes; no codegraph means the agent
greps). A `✗` line means dispatch will not work, and names the fix.

**What the stack needs, and why each one bites:**

| requirement | provided by | symptom when missing |
|---|---|---|
| shared `/workspace` volume | `compose.yaml` | worktrees are node-local; dispatch silently stops being clustered |
| seeded `/nix` store | the `nix-init` one-shot service | `nix is not installed on this node` |
| agent CLIs | the image (`npm i -g`) | `<agent> is not installed on this node` |
| codegraph | the image, wired via `codegraph install` | agents grep instead of querying the graph |
| agent credentials | `compose.override.yaml` **or** `.env` keys | `EACCES` from Bun, or `Not logged in` |
| registered repos | `storage.create_repo(...)` | `no repo named X` |
| a model backend | console UI → Models | dispatch has nothing to call |

`nix-init` seeds the Nix store volume once and exits; nodes wait for it via
`depends_on: service_completed_successfully`. That ordering is not cosmetic — a
shared volume cannot be seeded by mounting it over an image's `/nix`, because
Docker copies image content into a fresh volume and several nodes doing that
concurrently collide with `mkdir ... file exists`.

**To browse the knowledge vault in Obsidian**, point the workspace at a host
directory in `.env` and open `<that dir>/kb` as a vault:

```dotenv
WORKSPACE_MOUNT=/home/you/turnstone-workspace
```

Changing it starts a **fresh volume** — existing checkouts and notes do not
carry over.

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

### `PEBBLE_CLAUDE_BARE`

`--bare` skips discovery of hooks, plugins, MCP servers and `CLAUDE.md`, but it
**cannot read OAuth credentials**, so it forces an API key. It is therefore
opt-in, not default. Set `PEBBLE_CLAUDE_BARE=1` only if strict
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

**Turnstone uses Nix** (`setup_env`), because it is the one option that
materializes a toolchain *into the container the agent already runs in* — no
Docker socket, no docker-in-docker, no sibling container. Everything else on the
ladder (per-repo image, repo-declared devcontainer) requires re-plumbing how
dispatch runs; Nix is a tool call.

### Environments are named, shared, and curated

Not per-workstream throwaways. `/workspace/envs/` is a **git repository** of
plain flakes:

```
/workspace/envs/          <- git repo
    go-dev/flake.nix
    python-ml/flake.nix
    kokoro-go/flake.nix
```

```
bind_repo(repo="kokoro-go")
setup_env(action="use")                        # bootstraps a named env from the repo
setup_env(action="add", packages=["ffmpeg"])   # curate it; every dispatch gets it
dispatch_agent(task="fix the failing test")    # runs inside it
setup_env(action="use", name="go-dev")         # or share one across repos
```

Three properties follow from that shape:

* **Curation** — hit a missing library once, add it once. Every later dispatch,
  in any workstream on that env, has it.
* **Reuse** — repos on the same stack share one warm store and one eval.
* **Portability** — it is a git repo of ordinary flakes. Commit it, push it,
  clone it onto another machine, and `nix develop ./go-dev` reproduces the
  environment **with turnstone nowhere in the picture**. Every mutation is an
  auto-commit, so a bad edit is recoverable.

A flake whose `Generated by turnstone` marker has been removed is treated as
hand-tuned and is never overwritten — curation has to survive a re-provision.

Generated flakes are **multi-system** (`forAllSystems` over x86_64/aarch64,
Linux and Darwin). An environment that only builds on x86 isn't portable.

Detection reads repo markers (`go.mod`, `Cargo.toml`, `pyproject.toml`,
`package.json`, …) and every match contributes, because real repos are polyglot
— Kokoro-go carries `go.mod` *and* `pyproject.toml`. A repo's own `flake.nix`
wins outright. A repo with no markers still gets an interpreter, which is the
common case rather than an edge one.

**Nix evaluates the env directory, not the worktree.** A flake reference copies
its directory into the store and redoes that whenever the contents change:
evaluating from the worktree measured **16.9s per command** after each agent
edit, versus **0.27s** from the env dir. The agent edits constantly.

`path:` references are used rather than plain paths because a flake inside a git
repo is evaluated from the *git tree*, so an uncommitted file is invisible to it
(Nix fails and suggests `git add -N`). `path:` reads the filesystem directly.

### Where the store lives

`/nix` is a **persistent volume seeded once by a `nix-init` service**, not an
image layer. Two reasons:

* Baking it in made the image ~1GB heavier *and* ephemeral — every toolchain
  downloaded at runtime was lost on the next rebuild.
* The volume cannot be seeded by mounting it over an image's `/nix`: Docker
  copies image content into a fresh volume, and several nodes doing that
  concurrently collide with `mkdir ... file exists`. One writer that completes
  first (`depends_on: service_completed_successfully`) removes the race.

If the volume is absent, `setup_env` reports Nix as unavailable and dispatch
falls back to the base image's runtimes — nothing breaks.

Remaining rungs, if Nix ever stops being enough: per-repo image, then
repo-declared `.devcontainer/`. Both need dispatch in a **sibling container**,
which is the real architectural fork.

## 6b. Code graph (CodeGraph over MCP)

`bind_repo` indexes the worktree with [CodeGraph](https://github.com/colbymchenry/codegraph)
— tree-sitter ASTs into symbols and call edges in SQLite. Deterministic, local,
no embeddings, ~0.7s for a small repo. Dispatched agents reach it as an MCP tool,
so "who calls this?" is one call instead of a grep expedition.

Wiring differs per CLI and both paths are covered:

* **opencode / codex** read it from their global config, written into the image
  at build time (`codegraph install`). Verified with `opencode mcp list` →
  `✓ codegraph connected`.
* **Claude Code** gets it per-run via `--mcp-config`, because `~/.claude.json`
  is bind-mounted from the operator's host and would shadow anything the image
  configured. The runner writes a temp config and cleans it up.

The same plumbing carries any other MCP server: `run_agent(..., mcp_servers=...)`
and the adapter serializes it. Before this, a dispatched agent got **no** MCP
servers at all.

### Edge trust

CodeGraph resolves receiver types well — `self.viz.update(...)` and
`t.g2pPool.Close()` both land on the right method despite overloaded names. It
fails when the receiver is an expression it cannot type:

```python
shared_resource.data["epoch"].index(x)   # builtin list.index
# → matched to an unrelated user-defined index() in another file
```

`pebble/core/codegraph.py` prunes exactly that class after indexing. A false
edge is worse than a missing one: an agent asked "who calls this?" follows it and
reasons from a lie.

Two things worth knowing if you touch that resolver:

* An earlier version keyed on "ambiguous name that looks like a builtin method"
  and measured **20% precision** — four true edges deleted per false one caught.
  Verify any new rule against source before trusting it.
* Self-edges are reported, never pruned. One was checked and turned out to be
  genuine recursion; no static check separates that from the enclosing-method
  collision upstream tracks for TypeScript (#1496).

Inspect a graph by hand with `codegraph callers <symbol>`, `codegraph impact
<symbol>`, or `codegraph explore <query>` inside a worktree.

## 7. Troubleshooting

| Symptom | Cause |
|---|---|
| `Not logged in · Please run /login` | Usually **not** auth. The child's `PATH` lacked `/usr/bin`/`/bin` so the CLI couldn't spawn its helpers. Fixed in `run_agent`; if you see it again, check the mount's ownership (uid 1000). |
| `no repo bound` | Call `bind_repo` first. |
| `<agent> is not installed on this node` | The CLI isn't in the image; check `available_agents()`. |
| `could not lock config file` | Concurrent worktree creation. Fixed with `--no-track` + an flock on the mirror; if it recurs, a stale `turnstone-worktree.lock` in the mirror. |
| Diff full of `__pycache__` / `node_modules` | Build artifacts are excluded via the mirror's `info/exclude`; add patterns to `_LOCAL_EXCLUDES` in `workspace.py`. |
| Agent edits nothing | Check the diff *and* the stat — it may have only run commands. |
| Agent greps instead of using the graph | Check `opencode mcp list` inside the worktree; the index only exists after `bind_repo`. |
| `codegraph is not installed on this node` | Image built without it; rebuild. Dispatch still works, just without the graph. |
| `go: command not found` during dispatch | No toolchain provisioned. Run `setup_env(action="provision")`. |
| `invalid reference: main` | The repo's real default branch differs from the registered one. `create_worktree` now falls back to the mirror's HEAD automatically. |
| `nix is not installed on this node` | The `nix-store` volume wasn't seeded. Check `docker compose logs nix-init`. |
| `environment X was hand-edited` | Its `Generated by turnstone` marker is gone, so turnstone won't clobber it. Edit its `flake.nix` directly. |

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

## 9. Research loop (measured experiments)

The `kb` tool records **measured facts, not claimed ones**:

```
kb(action="search", query="worktree cost")        # always look first
kb(action="experiment",
   title="Flake eval cost from worktree",
   hypothesis="Evaluating from the worktree is slower than a small env dir",
   command="time nix develop path:. --command true",
   links=["Worktree isolation"])
kb(action="stale")                                # findings whose code moved
```

`experiment` **runs** the command in the bound worktree — inside the provisioned
Nix env, so it sees the same toolchain a dispatched agent would — and captures
the real output, exit code and duration. Because the tool does the running, a
recorded number cannot be one an agent invented after the fact. That is the
whole reason it is a tool action rather than prose written afterwards.

Each note records the **repo and exact commit** it was measured at, which gives
findings an expiry date. `kb(action="stale")` reads that back and lists notes
measured against code that has since moved, so the vault cannot quietly
accumulate confident claims about code that changed underneath them.

Notes render as ordinary Obsidian markdown — Hypothesis / Method / Result /
Verdict, linked with `[[wikilinks]]`. **The Verdict is deliberately left blank**:
the measurement is evidence, the verdict is knowledge, and only a reader can
supply it. `kb(action="graph")` surfaces hubs, orphans, and the *frontier* —
notes that are linked to but not yet written, i.e. where research should go next.

