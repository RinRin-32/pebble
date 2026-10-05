# Auto-approval and full access

Every tool call a model makes passes one gate,
`SessionUIBase.approve_tools` in
[`session_ui_base.py`](../pebble/core/session_ui_base.py). The gate either
approves the call without asking anyone, or it shows an approval card and
waits for a person. This page covers every way a call can be approved without
a person, and the newest of them: **full access**, which an operator arms on a
running session.

When a call is approved without a person, it is tagged with an
`auto_approve_reason`. The console shows that reason as a pill next to the
tool name (`auto: <reason>`). The same reason is recorded in the
`tool.auto_approved` audit row and in the workstream's dashboard ring buffer.
The tag is never left blank, so an auto-approved call always says what cleared
it.

---

## The reasons

The gate checks these in order. The first one that clears a batch decides the
reason.

| Reason | What cleared the call | Set when | Lives in |
|--------|-----------------------|----------|----------|
| `policy` | An admin `tool_policies` row with `action='allow'` matched the tool. A `deny` row matching the same tool refuses it instead, and wins over everything below. | An admin edits tool policies | Database, read on every batch |
| `skill` | The tool is in the skill template's `allowed_tools`. | The workstream is created from that skill | Memory (`ui.auto_approve_tools`) |
| `always` | Someone clicked **Approve + Always** on this tool earlier in this session. | At runtime, per tool | Memory |
| `auto_approve_tools` | The tool is in `auto_approve_tools`, but no source was recorded for it (legacy writers, CLI `/always`). | Varies | Memory |
| `blanket` | The workstream-level `auto_approve` flag is on. | Creation only: the `--skip-permissions` server flag, `tools.skip_permissions`, a skill with `auto_approve`, the create request's `auto_approve`, or watch restore | Memory (`ui.auto_approve`), never persisted |
| `full_access` | An operator **armed** this workstream. | At runtime, by a person holding the `full_access` capability | Database (`workstream_config`), read on every batch |
| `smart_approval` | Smart Approvals is on and the LLM judge recommended `approve` with confidence at or above `judge.confidence_threshold`. | Settings (`judge.smart_approvals`) | Settings, pushed on each turn |

`policy`, `skill`, `always` and `auto_approve_tools` clear individual tools.
`blanket` and `full_access` clear **whatever is still pending**, whatever the
tool is. `smart_approval` is the last automatic step before a person is asked.
It is skipped when `blanket` or `full_access` has already decided.

---

## Full access, and how it differs from blanket

`blanket` already exists and is not changed. Full access was added alongside
it as a separate mode, because what `blanket` cannot do is exactly what an
unattended session needs.

| | `blanket` (`auto_approve`) | `full_access` |
|---|---|---|
| Who turns it on | Whoever creates the session, or the server operator | A person holding the `full_access` capability, on a session they own |
| When | At creation only | At any time, on a live session |
| Can be turned off at runtime | No | Yes, by the owner or any `full_access` holder |
| Stored | Memory only. A restart forgets it unless the creation path sets it again. | `workstream_config` (`full_access`, `full_access_by`, `full_access_at`). It survives a restart. |
| Audited when turned on/off | No | Yes: `workstream.full_access.arm` / `.disarm`, with the actor |
| Re-checks the grant | No | Yes. Every batch checks that the user who armed it still holds `full_access`. |
| Drains a card already waiting | No | Yes, within about two seconds |
| Clears `__budget_override__` | **No** | **No** |
| Overrides a `deny` policy | No | No |
| Reason on the pill | `blanket` | `full_access`, styled as danger |

If a session has both, `blanket` wins and the call is tagged `blanket`. The
full-access row is not even read in that case. Behaviour for every existing
`auto_approve` session is unchanged.

### What full access does NOT bypass

- **`__budget_override__`.** When a skill's token budget runs out, the session
  injects this synthetic item so that a person decides whether to spend past
  the cap. A batch that contains it waits for a person, armed or not, and the
  waiting gate does not poll for full access. An unattended session is the one
  most likely to run past a cap without anyone noticing, so this is the wrong
  check to waive.
- **A `deny` tool policy.** Policy is evaluated before anything else.
- **Other workstreams.** Arming a coordinator does not arm its children. Each
  workstream is armed on its own `ws_id`.
- **Tool errors, output guards, the judge's verdicts.** Full access answers
  only one question, "may this run without asking?" Everything that happens
  around a tool call still happens, and the judge still records a verdict.
  By default that verdict is heuristic only. The LLM judge can't change what
  happens to an armed or blanket batch, and on a shared local model it would
  compete with the session's own main loop. Set
  `judge.llm_when_auto_approved` to run the LLM tier anyway.

### Dispatched coding agents

`dispatch_agent` runs a coding-agent CLI headless, so nobody can answer that
CLI's own permission prompts. Under Claude Code's `acceptEdits` mode every
shell command is refused, and the agent can write code but never run it or
its tests. When the dispatching workstream is armed, it is checked once per
dispatch and treated as not armed on any error. While armed, Claude Code is
started with `--permission-mode auto`, where Claude Code's safety classifier
lets routine commands through and still blocks risky ones. It is never
started with `bypassPermissions`. Unarmed workstreams keep `acceptEdits`.

---

## Propagation: when arming and disarming take effect

The armed state has **one copy**: the `workstream_config` row in the shared
database. The gate reads it at the moment it decides a batch. It is read only
when something would otherwise prompt and `blanket` has not already decided.
Nothing is cached on the UI object or the `ChatSession`.

- **Arm → the next tool batch.** The next batch that would have prompted is
  approved instead. The arming call does not have to reach the node: the
  console and edge routes write the shared row, and the node reads it.
- **Arm while a card is already waiting → within about two seconds.** A gate
  parked on an approval card re-reads the row every
  `_FULL_ACCESS_POLL_SECONDS` (2s). Once the row says armed, the gate resolves
  its own card as approved, tags the calls `full_access` and records them.
  This is the case that motivated the feature: an approval that would
  otherwise sit for an hour and then be denied.
- **Disarm → the next tool batch after the write commits.** There is no
  in-memory flag to drain, so nothing can keep approving on a stale copy.
  `test_disarm_stops_auto_approval_on_the_very_next_batch` pins this.
- **Not recalled:** a batch the gate approved *before* the disarm committed.
  That batch has already been decided and its tools may already be running.
  Disarming does not cancel a running turn; use Stop for that.

The extra cost: one indexed read per batch that would have prompted, plus one
read per two seconds per waiting card. Sessions that are never armed pay only
the first.

---

## Who may arm, disarm and see it

The rule is one function, `full_access.authorize()`, and the node, console and
edge surfaces all call it.

| Action | Requires |
|--------|----------|
| **Arm** | `write` scope, **and** the `full_access` capability, **and** ownership of the workstream. Refused for coordinator-minted tokens. |
| **Disarm** | `write` scope, **and** ownership **or** the `full_access` capability |
| **See the state** | `read` scope (console/node). On the edge API, `write` + `full_access`. |

Why it is gated this way:

- **Sending a message is not arming.** `write` lets a token talk to a session.
  If `write` were enough to arm, the next message could switch the approval
  gate off and then do anything without anyone being asked. `full_access` is a
  separate capability, off by default. An admin grants it per user under
  *Users → Capabilities → Arm full access*.
- **Only the owner arms.** Holding the capability does not let you arm
  someone else's session.
- **A model cannot arm.** A coordinator driving a child session on its owner's
  behalf carries the owner's identity and scopes. Tokens it mints are refused
  for arming, so a model cannot unlock its own gate.
- **Disarming is easier than arming.** An owner whose capability was revoked
  can still turn their own session off. Any `full_access` holder can stop a
  session they did not arm.
- **Fails closed.** `can_grant_full_access` returns `False` for an empty user
  or for any storage error, the same as `can_publish_skills`. The gate treats
  an unreadable row, a value other than exactly `"1"`, a missing armer, or an
  armer without the capability as **not armed**. Any doubt means a person is
  asked.

---

## How to see it

- **The armed banner.** The interactive pane, in both the console and the
  standalone UI, shows a danger-styled bar at the top when the session is
  armed. It names who armed it and when, and has a **Disarm** button. The pane
  re-reads the state from the node every time its event stream connects, so a
  page reload shows the stored state instead of losing it. It also polls every
  15 seconds, because the session can be armed from another tab, the console
  API or an edge client.
- **"Unknown" is shown, not hidden.** If the state cannot be read, the bar says
  so in the same danger styling. A session that may be approving its own calls
  must never look like one that is not.
- **Suspended.** If the armer lost the capability, the bar says the session is
  armed but suspended and that calls are asking again.
- **Per call.** Each call that full access cleared carries an
  `auto: full_access` pill, styled as danger.
- **Audit.** Look for `workstream.full_access.arm` and
  `workstream.full_access.disarm` (actor, `surface`: node / console / edge,
  `was_armed`). The calls themselves appear under `tool.auto_approved` with
  `reason: "full_access"`.
- **API.** `GET /v1/api/workstreams/{ws_id}/full-access` (node),
  `GET /v1/api/route/workstreams/{ws_id}/full-access` (console), or
  `GET /v1/api/edge/capabilities?ws_id=...` (edge). See
  [api-reference.md](api-reference.md).

## How to revoke it

- **One session:** click **Disarm** in the pane, or
  `POST .../full-access {"armed": false}`, or
  `POST /v1/api/edge/sessions/disarm-full-access`. The console route works even
  when the owning node is down, because it writes the shared row directly.
- **Everything one user armed:** remove their `full_access` capability. Their
  armed sessions stop at their next tool batch and show as suspended.
- **Mid-turn:** disarming does not stop calls that were already approved. Press
  Stop to cancel the turn.

---

## Failure modes

| Failure | What happens |
|---------|--------------|
| Database unreachable while the gate decides | Not armed. The batch prompts. |
| Database unreachable while a card waits | The poll reads "not armed" and the card keeps waiting for a person or the timeout. |
| Disarm write fails | `503`, `"... was NOT recorded ... may still be armed; retry the disarm."` It is not audited as a disarm. The pane shows **Disarm FAILED** and re-reads the state. |
| Arm write fails | `503`, `"... NOT recorded ... Nothing changed."` |
| Write commits but the confirming read fails | `503`, `"... was recorded, but the new state could not be re-read"`. A guessed success is never reported. |
| Capability table unreadable | Arming is refused (`403`, `missing_capability`). The gate treats the session as not armed. |
| Armer's capability revoked | The gate stops approving at the next batch. Status shows `suspended: true`. |
| Status unreadable | `503` from the API. The pane shows "state could not be read" in danger styling. |
| Session armed, then the node restarts | Still armed. The row is in the database, and a fresh session reads it on its first batch. |

## What this is not

- **Not a replacement for `--skip-permissions`.** That flag exists for local
  and dev setups and turns `blanket` on for every session on a node. Full
  access is per session, named, audited and revocable.
- **Not a sandbox.** Full access removes the person from the loop. It does not
  make the tools any safer. Use it when you would have approved everything the
  session is about to do.
- **Not visible on the coordinator page yet.** Coordinator workstreams can be
  armed through the console and edge APIs, and the coordinator tree shows the
  `full_access` pill on calls. The arm/disarm bar is only in the interactive
  pane.
