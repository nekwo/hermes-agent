# Mission-chat terminal-envelope grants (2026-07-26)

Status: **implemented.** Code: `agent_runtime/terminal_envelope.py` (policy),
`agent_runtime/runtime_config.py::TerminalEnvelopeConfig` +
`agent_runtime/config.py::_terminal_envelope_config` (root config),
`agent_runtime/profile_runner.py` (scope binding),
`agent_runtime/persona_runtime.py::mission_chat_reply` (the one lane that binds
a scope), `tools/terminal_tool.py::_harness_envelope_block` (the seam).
Tests: `tests/agent_runtime/test_terminal_envelope_grants.py`.

Parent audit:
[`mission-chat-lane-gap-audit.md`](mission-chat-lane-gap-audit.md) — this is the
G4 + G5b slice. Sibling precedent (deliberately mirrored):
[`mission-chat-mcp-admission.md`](mission-chat-mcp-admission.md).

**Activation is operator-pending.** Enforcement is live the moment this ships;
*grants* require the operator to write the stanza in §3 into the ROOT
`config.yaml`. Until then every envelope-gated class on mission-chat is a typed
refusal — which is strictly better than today's coin flip, but it is not "an
agent can push".

---

## 1. What was broken

The envelope had **two opposite behaviors for the same command on the same
lane**, and both were observed live on mission-chat Dev turns on 2026-07-26:

| Branch | Precondition | Outcome |
|---|---|---|
| fail-**CLOSED** | persona binds a `hermes_profile` ⇒ `persona_profile_context` exports `HERMES_AGENT_RUNTIME_ROOT` | `_harness_safety_block` fires ⇒ `git_push_requires_operator_approval`, and there is **no approval channel on this lane** to satisfy it |
| fail-**OPEN** | persona binds **no** profile ⇒ `profile_context.py:82-84` early-`yield`, the variable is never exported | envelope inert ⇒ `tools/approval.py` non-interactive default auto-approves ⇒ `git push origin main` **runs, ungated and unrecorded** |

The deciding variable was never a policy statement. The envelope activated on
ambient process-environment state that nothing in the policy layer controls,
and there are at least two independent paths to "unset":

* **Profile binding** — a persona with no `hermes_profile` takes the
  `profile_context.py:82-84` early-`yield`, so nothing exports the variable for
  that run. This is the clearest path and the one the table above names.
* **Process history** — `HERMES_AGENT_RUNTIME_ROOT` is
  `os.environ.setdefault`-ed by *some* harness command handlers
  (`_cmd_persona_instance_run_once`, `_run_free_floating_assignment_once`,
  `_cmd_persona_diagnose`, `_cmd_goal_run`) and **not** by
  `_cmd_mission_chat_message`. A long-lived `hermes harness serve` process
  re-dispatches every request through those same handlers, so whether the
  variable happens to be set when a mission-chat turn runs also depends on what
  ran in that process *before* it.

Either way: same lane, same role, same command, outcome decided by ambient
state. That is not a policy, it is a coin flip — and the fail-open side of it
executed a real `git push origin main` with no receipt anywhere.

Compounding it, the lane's operative rules told the model:

> "The operator's current permission grant is the only gate on what you can do"

which was false in both directions.

## 2. What it is now

`agent_runtime/terminal_envelope.py` is the **one** decision point for
envelope-gated commands on a governed lane (today: `mission_chat` only):

| Situation | Result |
|---|---|
| gated class + explicit grant | **runs**, grant provenance recorded |
| gated class + no grant | **typed refusal** `envelope_command_requires_grant` — carries the class, the exact ROOT-config key, the lane and the role |
| gated class outside the stage floor | **typed refusal** `envelope_command_not_grantable` — a hard floor; no config lifts it, and the refusal says so rather than sending the agent to ask for a grant that cannot exist |
| not a gated class | allowed, exactly as before |
| **no scope bound** (every other lane) | `envelope_decision()` returns `None` ⇒ the legacy pattern table decides, byte-for-byte |

The decision keys on a bound `TerminalEnvelopeScope`, **not** on
`HERMES_AGENT_RUNTIME_ROOT`. That is what closes the fail-open branch: a
mission-chat turn is governed whether or not its persona binds a profile.

Fail-closed is preserved at every degradation: a config load fault, a malformed
stanza, an unknown class, or an unimportable policy module all resolve to
"refuse", never "allow".

### Command classes

Not invented — a re-keying of the existing
`tools/terminal_tool.py::_HARNESS_BLOCK_PATTERNS` table from reason code to
class. Every class maps back to the exact legacy reason code, so
`blocked_tool_attempts.jsonl` readers and the existing envelope tests keep
their vocabulary. `test_class_patterns_mirror_the_legacy_envelope_table` guards
the mirror against drift.

| Class | Covers | Legacy reason code | Grantable? |
|---|---|---|---|
| `git_push` | `git push` | `git_push_requires_operator_approval` | **yes** |
| `destructive_git` | `git reset --hard\|--merge\|--keep`, `git clean -xdf`, `git checkout --force\|-f\|-- .`, `git switch -f`, `git restore`, `git stash drop\|clear` | `tree_wipe_blocked` | **yes** |
| `recursive_delete` | `rm -rf`, `Remove-Item -Recurse` | `tree_wipe_blocked` | **yes** |
| `network_egress` | `curl`/`wget`/`iwr`/`Invoke-WebRequest`/`Invoke-RestMethod` to a host outside `{localhost, 127.0.0.1, ::1, host.docker.internal}` | `network_command_requires_allowlist` | **yes** |
| `credential_read` | `cat`/`type`/`Get-Content` of `.env`/`credentials`/`.netrc`/`.pgpass`/`.npmrc`/`.pypirc` | `credential_read_blocked` | **no — hard floor** |
| `credential_exfil` | loopback network call carrying credential-shaped material | `credential_exfil_blocked` | **no — hard floor** |
| `prod_operation` | `kubectl`/`helm apply\|delete\|rollout\|scale\|patch`, `terraform apply\|destroy` | `prod_operation_requires_operator_approval` | **no — hard floor** |

`destructive_git` and `recursive_delete` share one legacy reason code because
the old table did not distinguish them. They are separate **classes** so a
grant can name one without granting the other.

The three non-grantable classes are the **stage floor**
(`GRANTABLE_COMMAND_CLASSES`). The historical MCP role floor was independent
and has since been removed; MCP admission now follows profile declarations.
Naming a non-grantable class under `grants` is a typed config error, not a
grant. S12 separately removes these terminal floors in a revertable commit.

## 3. The config stanza — OPERATOR WRITES THIS

**ROOT `config.yaml` only** (`<hermes root>/config.yaml`, resolved by
`config.harness_root_config_path()`). A sticky-active profile's own
`config.yaml` **cannot** grant — that is pinned by
`test_grants_read_the_root_config_not_the_active_profile`.

```yaml
agent_runtime:
  terminal_envelope:
    grants:
      dev:
        mission_chat: [git_push]
```

Config key referenced in refusals:
`agent_runtime.terminal_envelope.grants.<role>.<lane>`.

* **Roles**: `pm`, `dev`, `qa`, `alice_supervisor` (spell it `neko_supervisor`
  or `neko` if you prefer — the alias resolves to `alice_supervisor`; the
  canonical spelling wins when both are present, and the two are never unioned).
* **Lanes**: `mission_chat` is the only governed lane. A grant on any other
  lane key is inert.
* **Deny by default**: no `*` role, no `*` lane, no inheritance. A role with no
  entry, or a lane with no entry under that role, grants nothing.
* **No kill switch, on purpose**: enforcement is unconditional and the
  deny-by-default property comes from the table being *empty*, not from a flag.
  One authority, no double negative — revoke by deleting the class.
* A running `hermes harness serve` picks up an edit on its **next turn** (the
  grant is read at decision time, not cached across the process).

### Suggested starting grant

Given the ruling that mission-chat is the primary work lane and this repo's own
git discipline ("work is not done until it is pushed to `origin/main`"),
`dev.mission_chat: [git_push]` is the minimum that makes the lane able to land
its own work. `destructive_git` is the natural second (an agent that cannot
`git restore` a file it broke has no undo), but it is also the class most
likely to destroy an operator's in-flight work in a shared checkout — grant it
deliberately, not reflexively.

## 4. What the agent sees

A refusal is self-explanatory by construction — it names the class, the exact
config key, the stanza, and the fact that the agent cannot grant it:

```
BLOCKED by Harness execution safety envelope: git_push_requires_operator_approval
This command publishes commits to a remote ('git_push'), and role 'dev' holds no
grant for it on the 'mission_chat' lane.
An OPERATOR can allow it by adding this to the ROOT config.yaml (not a profile
config — a profile cannot grant itself this):

  agent_runtime:
    terminal_envelope:
      grants:
        dev:
          mission_chat: [git_push]

Config key: agent_runtime.terminal_envelope.grants.dev.mission_chat
The grant is read at turn time from the root config; a running
`hermes harness serve` picks it up on its next turn.
Do NOT retry, reword, or split this command — every form of it resolves to the
same 'git_push' class and will be refused identically. Report the refusal to the
operator and continue with the work you can do.
```

The JSON result keeps every legacy key (`status`, `blocked_by`,
`block_reason`) and adds `failure_class`, `command_class`, `lane`, `role`,
`config_key`, and a `requirement_failure` row in the shared
`{code, subject, entry_point_lane, summary, fix_hint}` shape that `mcp_lane` /
`mcp_admission` / `machine_roots` already emit — so operator surfaces need no
new case.

The mission-chat operative rules were also corrected: they no longer claim the
permission grant is the only gate. They name both gates and tell the agent to
relay an envelope refusal rather than retry it.

## 5. Receipts

| File (under the runtime root) | Written when | Contents |
|---|---|---|
| `terminal_envelope_decisions.jsonl` | every governed refusal **and** every granted execution | `decision`, `lane`, `role`, `persona_id`, `session_id`, `command_class`, `reason`, `failure_class`, `granted_by` (the config key, for grants), `command_preview`, `grant_issues` |
| `blocked_tool_attempts.jsonl` | refusals only | the pre-existing shape (`reason`, `command_preview`) plus the typed fields — operators watching this file lose nothing |

Allowed (ungated) commands write **nothing**: the log records decisions that
changed an outcome, not a firehose.

Both writes resolve the root from the bound scope first and the environment
second, so the former fail-open construction — which never exported
`HERMES_AGENT_RUNTIME_ROOT` and therefore produced **no receipts at all** —
now produces them.

## 6. Non-goals of this slice

* **No launcher / Mission Control approval UI.** An operator approving a
  specific command in-flight from Mission Control is a separate launcher slice.
  This slice makes the decision deterministic and config-governed; it does not
  add an interactive channel.
* **No change to any other lane.** Worker ticks, free-chat, `hermes chat`,
  `cron`, `gateway` and `acp` bind no scope and keep the exact behavior they
  have today. Pinned by `test_worker_lane_keeps_the_legacy_hard_block` and
  `test_chat_lane_keeps_no_envelope_at_all`.
* **No weakening for non-granted classes.** The envelope is not relaxed
  anywhere. The only new "allow" is an explicit operator grant, bounded by the
  stage floor.
* **`tools/approval.py`'s general fail-open default is untouched.** It is
  upstream, and it is out of scope here. What changed is that envelope-gated
  commands on mission-chat no longer *reach* it undecided — the fail-open
  branch is closed for this class of command on this lane, which is the branch
  the live evidence exercised.

## 7. Follow-ups (not in this slice)

1. **The rest of G5.** The envelope refusal is now typed, but the other
   lane-capability drops (G1/G2/G7/G11/G15) still emit no row. The generalized
   `lane_capability_drops(...)` producer the audit proposes is still owed;
   `TerminalEnvelopeDecision.row()` is deliberately in that row shape so it
   drops straight in.
2. **Turn-start visibility.** — **CLOSED 2026-07-26.** `requirement_failures`
   is resolved before any command runs, so an envelope refusal cannot appear
   there. `explain_terminal_envelope(role=…, lane=…)` — side-effect free — is
   now read once per mission-chat turn by
   `runtime_hud.capability_block_for_persona` and rendered into the situational
   HUD's **capability block** on the runtime-context envelope's volatile tail,
   beside the typed chat-lane drops. The agent is told up front what it holds
   (`granted`), what an operator could grant it and with which exact config key
   (`refused_grantable`), and what no config lifts (`refused_hard_floor`) — so
   it plans around the gate instead of discovering it by running the command.
   The block is volatile by contract (its `runtime_hud.HUD_FIELDS` row carries
   `volatile=True`, the `turn_budget` precedent): never hashed into the HUD
   revision, always emitted, and silent when there is nothing to report. It
   rides the tail as a registered `volatile_tail` contributor with its own byte
   budget, so a widened policy is truncated with an in-band note plus a typed
   accounting row rather than silently. Tests:
   `tests/agent_runtime/test_runtime_hud_capability_visibility.py`,
   `tests/agent_runtime/test_runtime_hud_field_contract.py`,
   `tests/agent_runtime/test_mission_chat_turn_context.py`,
   `tests/agent_runtime/test_volatile_tail.py`.

   The operator half of the same posture is
   `hermes harness persona tool-diff <persona> --explain-envelope`
   (`agent_runtime/terminal_envelope_explain.py`): scope bound or not, the
   grantable/hard-floor split, the live grants and the exact ROOT-config key,
   rendered from `explain_terminal_envelope` + `hard_floor_command_classes`
   with no parallel derivation.
3. **`profile_context` early-yield.** The `binding.profile_home is None`
   branch that produced the fail-open case still skips every environment export
   for profile-less personas. The envelope no longer depends on it, but
   anything else keyed on `HERMES_AGENT_RUNTIME_ROOT` inherits the same
   nondeterminism. Worth an explicit audit of that variable's other readers.

   **The audit is DONE** —
   [`env-determinism-audit.md`](env-determinism-audit.md). All 19 readers of
   that variable plus every ambient env read in `agent_runtime/` are classified
   there. Three were nondeterministic and safely fixable and are fixed —
   including **this module's own `_audit_root`**, where scope → env → *nothing*
   silently dropped the receipt for a decision this module had already made
   deterministically. The early-yield itself is **Q1**, one of six operator
   questions written up decision-ready; it is coupled to **Q2** (the legacy
   `_harness_safety_block` presence gate, still live on every ungoverned lane)
   and must be decided with it.
