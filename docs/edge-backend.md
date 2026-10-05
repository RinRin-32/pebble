# The edge backend: skills and the vault over HTTP

An **edge client** runs on a laptop and uses pebble as its central server.
`sediment` is the first one: a standalone app and CLI that wants pebble's
skills and knowledge-vault backend, with a UI of its own. It is not an MCP
client.

Until now that backend was reachable only as MCP tools on `/mcp`
([`pebble/core/kb_mcp.py`](../pebble/core/kb_mcp.py)), plus one HTTP route,
`POST /v1/api/skills/report`. That route exists for a hook to call, not for a
person or an app. So an edge client that did not speak MCP had nothing it could
use.

`/v1/api/edge/*` closes that gap. It is implemented in
[`pebble/core/edge_api.py`](../pebble/core/edge_api.py) and registered inside
the console's `/v1` mount, next to the report route.

**This is not a second backend.** Every route calls the function its MCP tool
calls:

- `skills/pull` calls `skill_transfer.build_bundle`.
- `skills/publish` calls `skill_publish.publish`, including its policy gate.
- `skills/hook` calls `skill_transfer.mint_report_token` and `hook_config`,
  through the same `kb_mcp.skills_hook_payload` that `kb_skills_hook` uses.
- The `kb/*` routes call the shared bodies in `kb_mcp` (`search_vault`,
  `read_vault_note`, `write_vault_note`, `record_experiment_note`). These are
  the same functions the `kb_search`, `kb_read`, `kb_write` and
  `kb_record_experiment` tools call.

There is one selection rule, one policy gate and one write path. A fix to any
of them reaches both surfaces. Re-implementing them would give two copies that
drift apart, and the first sign would be an edge client that disagrees with an
MCP session about which skills a repo has.

The edge API can also arm a session to run unattended; see
[Arming a session](#arming-a-session-full-access).

**This is not a UI.** No JavaScript or console page ships with it. The edge
draws its own interface, and `GET /capabilities` exists so that interface can
show only the actions the caller is allowed to use.

---

## Endpoints

All routes live under `/v1/api/edge/`. They take a JSON body (except
`capabilities`, which is a GET) and return the envelope the core functions
already use: `{"ok": true, ...}` on success, and `{"ok": false, "error": "..."}`
with a non-2xx status on failure.

| Method | Path | Does | Requires | Returns |
|--------|------|------|----------|---------|
| `GET` | `/v1/api/edge/capabilities` | Who you are and what you can call | `read` | `user_id`, `scopes`, `capabilities`, `operations[]` (each with `available` and `missing`) |
| `POST` | `/v1/api/edge/skills/pull` | The skill bundle for a repo (`build_bundle`) | `read` | `skills[]`, `count`, `token_estimate`, `token_budget`, `truncated[]`, `not_found[]` |
| `POST` | `/v1/api/edge/skills/publish` | Publish one **repo-scoped** skill through the policy gate (`publish`) | `write` **and** the `skill_publish` capability | `published`, `repo`, `updated`, `verdict`, `policy_reason` |
| `POST` | `/v1/api/edge/skills/hook` | Mint a 12-hour `skills.report` token and return the hook config | `write` (same as `kb_skills_hook`) | `settings_json`, `expires_hours`, `note` |
| `POST` | `/v1/api/edge/kb/search` | Rank notes against a query | `read` | `query`, `repo`, `count`, `results[]` |
| `POST` | `/v1/api/edge/kb/read` | One note by exact title, with its body | `read` | the note, or `404` with `found: false` |
| `POST` | `/v1/api/edge/kb/write` | Write a note or append to one | `write` | `title`, `path`, `appended`, `links`, `color` |
| `POST` | `/v1/api/edge/kb/experiment` | Record a result the edge already measured | `write` | `title`, `path`, `verdict` |
| `POST` | `/v1/api/edge/sessions/arm-full-access` | Arm one of your own sessions to approve its own tool calls | `write` **and** the `full_access` capability | `armed`, `suspended`, `changed_by`, `changed_at`, `budget_override_prompts` |
| `POST` | `/v1/api/edge/sessions/disarm-full-access` | Disarm it | `write` **and** `full_access` | the same |
| `POST` | `/v1/api/edge/sessions/full-access-status` | Whether it is armed, by whom, since when | `write` **and** `full_access` | the same, plus `can_arm` |

Notes on request fields:

- `skills/pull` takes `repo`, `names` (a list of strings) and an optional
  `max_tokens`. An edge may ask for a smaller budget than the server default
  (`DEFAULT_BUNDLE_TOKENS`, 30,000), but not a larger one. The ceiling exists
  to protect the session, and `token_budget` in the response shows the budget
  that was actually applied.
- `kb/experiment` requires `exit_code`. Defaulting it to `0` would record a
  pass that nobody measured.
- Notes written through these routes are stamped `edge:<user_id>`. Notes
  written over MCP are stamped `mcp:<user_id>`. A note should say which hand
  wrote it.

Field-level detail is in [api-reference.md](api-reference.md#edge-api-console).

---

## Authorization: per operation, in the handler, failing closed

### The bug this shape has already caused once

The console's `AuthMiddleware` decides the required scope from the URL path,
using `required_scope()` in [`auth.py`](../pebble/core/auth.py). Over `/mcp`,
one path served every tool, so that rule resolved the whole mount to `read`. It
could not tell `kb_search` from `kb_delete`.

This was verified against the live console: a token minted with
`scopes="read"` created a note and then deleted it. The README said otherwise,
which is how the gap stayed invisible. The fix was per-tool enforcement where
the tools live (`_denied()` in `kb_mcp.py`), failing closed.
[`tests/test_kb_mcp_scopes.py`](../tests/test_kb_mcp_scopes.py) pins that fix.

`/v1/api/edge/*` has exactly the same shape: one prefix and many operations.
None of these paths are in `WRITE_PATHS`, so `required_scope()` resolves every
one of them to `read`.

### What is done instead

- **The middleware's answer is a floor, never a grant.** It still runs, so an
  unauthenticated request gets a `401`. A `skills.report` token fails its
  `read` check and gets a `403` before any handler runs. Neither of those
  outcomes is treated as permission for a particular operation.
- **One table defines every operation's requirement.** `OPERATIONS` in
  `edge_api.py` lists each operation's scope and capability. The `_edge()`
  wrapper looks the operation up by name and runs `authorize()` before the
  handler body does any work. Routes are built from the same table, so a route
  cannot be registered without a gate.
- **The check reads the scopes resolved for the token, never the path.**
  `authorize()` uses `request.state.auth_result.scopes`, which `parse_scopes()`
  has already expanded through `SCOPE_HIERARCHY`. A `write` token therefore
  holds `read` as well. A `skills.report` token holds `skills.report` and
  nothing else.
- **Capabilities reuse the access helpers.** The `skill_publish` capability is
  checked with `access.can_publish_skills`, which already fails closed when
  storage cannot be read. If a capability has no helper, it is refused.
  `publish()` checks the capability again on its own; that check is the
  authority, and the edge check exists so the refusal can be a `403` that
  names the missing capability.
- **Missing identity is a refusal.** If a request reaches a handler without an
  `AuthResult` or a `user_id`, it gets a `401`, never a guessed principal. This
  covers a middleware that did not run or a path accidentally made public.
- **Refusals are explicit.** A `403` body names the missing scope or
  capability and says what to mint or request: `missing_scope: "write"` or
  `missing_capability: "skill_publish"`. A bare `403` tells an operator that
  something is wrong but not what to fix.

The scope matrix in
[`tests/test_edge_api_scopes.py`](../tests/test_edge_api_scopes.py) covers
every operation, not a sample:

- No token is refused.
- A `read` token cannot reach publish, hook, write or experiment, and can reach
  every read operation.
- A `skills.report` token reaches nothing. Its user holds `skill_publish`, so
  the test shows that the token's scope is what limits it.
- The intended grant works.

The matrix runs twice: once through the real `AuthMiddleware` with real `ts_`
tokens, and once with the middleware removed. The second run is what shows the
handlers enforce authority themselves instead of inheriting it from the path.

The full-access operations add four more cases to the matrix: `write` without
the `full_access` capability is refused with `missing_capability`, an
unreadable capability table is refused, a coordinator-minted token cannot arm,
and an edge client cannot reach a session its user does not own.

### Why `hook` needs `write`

`skills/hook` **mints a credential**. That credential is weak by design: it
can only report invocations, and it expires in `REPORT_TOKEN_HOURS`. It is
still a credential, and `kb_skills_hook` requires `write` for it, so this
route does too. If the two surfaces disagreed, the weaker one would become the
real policy.

### Cookies

`AuthMiddleware` also accepts the console session cookie, so a signed-in
browser can call these routes. The cookie is `SameSite=Lax`, so a cross-site
`POST` does not carry it. This is the same protection every other console
`POST` relies on; nothing here weakens it or adds to it.

---

## Arming a session: full access

An autonomous edge session is the case full access was built for. Sediment
starts a session, the model calls a tool, and an approval card appears in a
console nobody is watching. After an hour the card times out and the call is
denied. **An armed session does not wait.** Every tool call that would have
prompted is approved and tagged `auto_approve_reason: "full_access"`.

**An armed edge session is unattended by design.** Nobody reviews its tool
calls before they run. That is the point, and it is also the risk: whatever
the model decides to run, runs. Arm a session only when you would have
approved everything it is about to do anyway, and disarm it when that stops
being true.

What arming does **not** do:

- **It does not clear a budget override.** A batch carrying
  `__budget_override__` still waits for a person. An unattended session is the
  one most likely to run past its cap without anyone noticing.
- **It does not override a `deny` tool policy.** Policy runs first, and a
  denied call stays denied.
- **It does not arm children.** Each workstream is armed on its own `ws_id`.
- **It does not outlive the grant.** Every tool batch re-checks that the user
  who armed the session still holds `full_access`. Revoking the capability
  stops all of that user's armed sessions at their next tool call. Status then
  reports `suspended: true` rather than silently showing "off".

How the edge routes differ from the console's:

- **Own sessions only.** An edge client can arm, disarm and read only sessions
  its user owns. Any other `ws_id` gets a `404`. The console route lets a
  `full_access` holder disarm someone else's session; a laptop does not get
  that reach.
- **`full_access` is required for all three, including status.** One table
  entry per operation keeps the gate in `OPERATIONS`, the same as every other
  edge route. An edge client that cannot arm has no use for polling. A person
  can always see the state in the console banner.
- **A coordinator token cannot arm.** A model driving a session on its owner's
  behalf holds the owner's identity and scopes, and it still must not be able
  to switch the approval gate off.

`GET /capabilities?ws_id=<id>` includes the named session's armed state in
`full_access.session`, so an edge UI can show it without a separate call.

**How it propagates.** The armed state lives in the shared `workstream_config`
table, and the node's approval gate reads it on every tool batch that would
otherwise prompt. Arming applies from the next tool batch. A card that is
already waiting is drained within about two seconds. Disarming applies to the
next tool batch after the write commits. There is no in-memory copy to drain
late. A batch the gate already approved is not recalled.

Every arm and disarm is audited (`workstream.full_access.arm` / `.disarm`)
with the actor and `surface: "edge"`. The token is never logged. The full
model, including how this differs from `auto_approve`, is in
[auto-approve.md](auto-approve.md).

---

## The bundle contract (unchanged, now reachable over HTTP)

These are properties of `build_bundle` and `publish`. The edge API passes them
through untouched, and the tests pin that they still hold over HTTP.

- **A bundle, not a synthesis.** Pull returns N skills verbatim, each with its
  own name, version and `allowed_tools`. Combining skills into one is a
  separate product with its own problems: which instruction wins in a
  conflict, how tool grants combine, and provenance when a source changes.
- **The server scopes; the edge filters.** Pebble decides what the caller may
  see: the repo's skills plus globals. The edge decides which of those apply,
  by matching each skill's `paths` globs against its own working tree. Pebble
  never asks for the file listing. It cannot see that directory, should not
  trust it, and it changes between requests.
- **Named requests bypass the glob and the budget.** Asking for a skill by
  name is deliberate, and silently leaving it out would make the skill look
  broken. Names that do not resolve come back in `not_found`.
- **Truncation is always reported.** Skills dropped by the token budget or by
  `MAX_BUNDLE_SKILLS` are listed by name in `truncated`. An edge that silently
  received half its skills would look like it had a skill that does not work.
  `truncated` is a list, not a boolean, so the edge can ask for those skills by
  name.
- **Archived skills are excluded.** If archived skills still shipped,
  archiving would only be a label. It would not be the reversible step between
  "suspicious" and "deleted" that the janitor relies on.
- **A repo's skill shadows a global with the same name.** This is the same
  resolution rule `get_prompt_template_by_name` uses for a single skill, and
  repo-scoped skills sort first.
- **Publishing is repo-scoped only.** A body with `"global": true` is refused
  with a `400` that says why. It is never quietly turned into a repo skill: a
  caller who asked for a global and got a repo skill would believe every
  machine now has it. A body with no `repo` gets `publish()`'s own refusal.
  Tool grants are always assigned on the server and never taken from the
  caller. A policy refusal returns `422` with `refused_by: "policy"`, so a
  client can tell "your request was malformed" from "your content was judged
  unsafe" without parsing the message.

### Pulls are not usage

`skills/pull` records a `pulled` event for each skill it ships. That means
only that the skill was offered. `invoked` events come **only** from the hook,
through the existing `POST /v1/api/skills/report`. No edge route writes one.

If the two were mixed, the janitor could delete a rare but critical skill for
being unpopular, and keep a useless skill because it is in every bundle. An
edge UI that wants to say "you used this" must therefore rely on the hook. It
must not treat having pulled a skill as evidence that the skill was used.

---

## Failure modes, and how each one shows up

| Failure | What the caller sees |
|---------|----------------------|
| No or invalid token | `401` from the middleware |
| Token with the wrong scope (including a hook token) | `403` from the middleware (`skills.report` lacks `read`) or from the handler, with `missing_scope` |
| Missing capability | `403` with `missing_capability: "skill_publish"` or `"full_access"` |
| Someone else's session (`sessions/*`) | `404`, as if it did not exist |
| Full-access write did not commit | `503`, `"... was NOT recorded ..."`. On a disarm this means the session **may still be armed**. |
| Malformed body or field | `400` naming the field (`'names' must be a list of strings`) |
| Policy gate refuses a skill | `422`, `refused_by: "policy"`, the model's reason. The gate fails closed: if the model is unreachable, publishing is refused. |
| Note not found | `404`, `found: false` |
| Storage unavailable | `503` |
| Anything unexpected | `500`, `"<operation> failed: <ExceptionType>"`. Logged with the operation name only, never the request body. |

No failure comes back with a success-shaped body. Tokens and note bodies are
never logged. The only log line for a minted token is
`skill_transfer.report_token_minted`, which records the user and the expiry.

---

## Deliberately out of scope

- **Running anything.** `kb/experiment` records a result the edge measured.
  Pebble does not become a remote code executor because an app on a laptop can
  reach it. This is the same reasoning as the MCP surface.
- **Archive, delete, rename, janitor and deletion review.** These are cleanup
  and curation actions on shared state. They stay on MCP and in the console
  until an edge client actually needs them and they get their own rows in the
  scope matrix.
- **Planning and interview conversations** (`kb_plan*`, `kb_interview*`).
  These are stateful, model-backed sessions. Exposing them is a separate
  decision.
- **Publishing globals from a device.** Globals ship to every machine, so they
  are created in the console.
- **Widening a skill's `allowed_tools`.** That is done by a person in the
  console.
- **Reporting invocations.** This already has its own route and its own
  token. See [Pulls are not usage](#pulls-are-not-usage).

## Open

- **Prompt injection against the policy gate.** The skill body is untrusted
  input to the model that judges it. This is not solved; see the
  `skill_publish` module docstring. What limits the damage is the same as
  before: repo scope only, no caller-supplied tool grants, no silent
  overwrite, and attribution on every row.
- **No staging queue for publishing.** If publishing is ever opened beyond an
  operator's own authorized device, a queue with a person reviewing it is the
  right design, and this API is not.
- **Per-repo authorization.** Any `read` token can pull any repo's skills and
  search the whole vault. This matches MCP today. The vault has no per-repo
  ACL to enforce, and inventing one at the HTTP layer would make the two
  surfaces disagree.
- **Rate limits.** The edge routes get whatever the console applies to every
  route, and nothing more.
