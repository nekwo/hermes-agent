# Ambient-environment determinism audit (2026-07-26)

Status (wave 4, 2026-07-26): **audit complete; every class-2 fix landed; all six
operator questions RULED AND IMPLEMENTED on the fork side.** What remains is a
short, exact list of `tools/`-side one-liners that only an owner of that
boundary can apply — §7. Scope: every reader of `HERMES_AGENT_RUNTIME_ROOT` in
the repo, plus sibling ambient environment keys in `agent_runtime/` that gate
behavior on **presence** rather than **validated content**.

Wave-4 disposition at a glance:

| Q | Ruling | Where it landed |
|---|---|---|
| **Q1** | export the runtime root unconditionally | `agent_runtime/profile_context.py` — landed |
| **Q2** | key on a bound scope, every harness lane | `terminal_envelope.py` + the 4 lane sites — landed; `tools/` half retired **by construction** |
| **Q3** | one import, not a re-derivation | fork-owned `record_legacy_block` landed; **one-line upstream delegation owed** (§7.1) |
| **Q4** | assert something real | `agent_runtime/preflight.py` — landed |
| **Q5** | a smoke run is always synthetic | `agent_runtime/smoke.py` — landed; **flag registration removal owed** (§7.3) |
| **Q6** | no handler seeds; every reader resolves | 7 `setdefault` sites removed — landed |
| **§3** | call-time home, not an import-time freeze | drift tests landed; **`tools/skills_sync.py` diff owed** (§7.2) |

Q2 and Q1 were landed **together in one change, Q2 first**, because Q1 alone
arms the legacy env-keyed envelope on every lane that previously ran with the
variable unset. See Q1's blast radius.

Parent: [`mission-chat-terminal-envelope-grants.md`](mission-chat-terminal-envelope-grants.md)
§7.3 *of that document* — *"anything else keyed on `HERMES_AGENT_RUNTIME_ROOT`
inherits the same nondeterminism. Worth an explicit audit of that variable's
other readers."*
This is that audit. Precedent instance:
[`mission-chat-lane-gap-audit.md`](mission-chat-lane-gap-audit.md) G4/G5b.

---

## 1. The bug class, stated once

A reader is **nondeterministic** when its behavior is decided by whether an
environment variable *happens to be set in this process*, rather than by a
declared resolution ladder over validated content.

Two independent paths put `HERMES_AGENT_RUNTIME_ROOT` in either state, neither
of them a policy statement:

* **Profile binding.** `agent_runtime/profile_context.py:84-86` — a persona
  with no `hermes_profile` takes the `binding.profile_home is None`
  early-`yield`, so `persona_profile_context` exports *nothing* for that run.
* **Process history.** The variable is `os.environ.setdefault`-ed by *some*
  harness command handlers and not others (§2 #13-19). A long-lived `hermes harness
  serve` re-dispatches every request through those same handlers in one
  process, so whether the variable is set when a turn runs depends on what ran
  in that process **before** it.

Same lane, same role, same request, different outcome — decided by ancestry.
That is a coin flip, not a policy. K's wave-2 fix retired it for the terminal
envelope's *decision* by keying on a bound `TerminalEnvelopeScope` instead. The
table below is every other reader.

**The deterministic shape** (K's one-gate pattern, applied): read the **value**,
walk an **explicit ladder** whose rungs are each validated, and produce a
**typed** outcome — including a typed "unresolvable" — so the degradation is
accounted for instead of silent. `agent_runtime/resolution.py` is the reference
implementation and the single source of truth for this variable; every rung it
skips is recorded in `RuntimeResolution.trace`.

### Classification

| Class | Meaning |
|---|---|
| **1** | Deterministic already — reads the value through a validated ladder, or is a pure report of it. Documented here so nobody re-derives it. |
| **2** | Nondeterministic and **safely fixable** — fixed in this slice, with unit tests proving env-present and env-absent produce the same typed outcome. |
| **3** | Nondeterministic but the fix changes behavior visible to live flows — **not fixed**. Written up decision-ready in §5. |

---

## 2. Reader-by-reader — `HERMES_AGENT_RUNTIME_ROOT`

Line numbers are against `a3316572d` (the state audited). "W" marks a *writer*
rather than a reader; writers are the source of the ancestry, so they are
inventoried too.

| # | Site | Role | Class | Finding / action |
|---|---|---|---|---|
| 1 | `agent_runtime/resolution.py:35-42` `resolve_runtime` | resolver rung 1 | **1** | THE ladder: env → `agent_runtime.store_root` in the root config → platform default. Takes an injectable `Mapping` (pure, unit-testable with no process env), always answers, records every skipped rung in `trace`. This is the shape everything else should delegate to. |
| 2 | `agent_runtime/resolution.py:104` `resolution_table` | operator report | **1** | Reports the env layer's value and whether it won. Pure projection of #1. |
| 3 | `agent_runtime/paths.py:8-13` `store_root` | the one accessor | **1** | `resolve_runtime()` + `assert_probe_isolation`. Every store path in the repo derives from it. Not an env reader itself — which is exactly why it is safe. |
| 4 | `agent_runtime/terminal_envelope.py:725-736` `_audit_root` | receipt root | **2** | **FIXED.** Scope → env → *nothing*. With no scope-carried root (`scope_for_persona`'s `runtime_root` argument defaults to `None`) and no exported variable, a governed refusal wrote **no receipt, silently**. K made the decision deterministic; whether anyone could later *prove* it happened was still decided by ancestry. Added the canonical resolver as rung 3, and `audit_root_source` on every row so a receipt says which rung answered. Never raises; `None` now means "genuinely nowhere to write". |
| 5 | `agent_runtime/smoke.py:55` `run_smoke` | `--no-temp-root` root | **2** | **FIXED.** Was `os.environ.get(RUNTIME_ROOT_ENV, ".hermes-agent-runtime")` — **two** nondeterminisms in one expression: presence-keyed, and the fallback is **relative to the process cwd**, which mission-chat workdir grounding now `chdir`s per turn (§4). Replaced with `paths.store_root()`, the one definition of "the configured root". Env-set behavior byte-identical. See operator question **Q5** for the residual policy issue this exposed. |
| 6 | `agent_runtime/profile_context.py:110` (W) | the exporter | **3 → FIXED (Q1)** | The `profile_home is None` early-`yield` at `:84-86` was path 1 of the split. The runtime root is now exported for **every** persona (resolved through `paths.store_root()` when no argument is passed, and resolved BEFORE the `HERMES_HOME` override so it is the head resolution); `HERMES_HOME`/`HOME`/`HERMES_AUTH_HOME` stay gated on the binding; both degradations emit a typed `ProfileContextRow`. |
| 7 | `agent_runtime/profile_context.py:88-91` (W) | save/restore | **1** | Snapshots the prior value and restores it in `finally`, including the `None` case (pops rather than writing `"None"`). Correct scoping; not a gate. |
| 8 | `agent_runtime/smoke.py:24-39` (W) `_runtime_root` | save/restore | **1** | Same correct save/restore shape as #7. |
| 9 | `agent_runtime/preflight.py:389` `_runtime_root_check` | preflight check | **1 (weak) → FIXED (Q4)** | Was **vacuous**: the resolver always returns a non-empty path (the default rung), so `ok = bool(str(root).strip())` could only be false if `store_root()` *raised*. It reported `runtime_root=present` for a root that does not exist, with a fix hint naming a variable it never checked. Now reports the winning layer, the resolved path, `exists` and `store` (tasks/ present), and fails on exactly `suspect_default_root` — default layer AND not a store. |
| 10 | `agent_runtime/snapshot.py:314` `_runtime_paths_diagnostic` | diagnostic | **1** | Reports the raw value or `<unset>` alongside the resolved paths. A report of the ambient state is the correct use of a read — it is how an operator *sees* the split rather than being subject to it. |
| 11 | `tools/terminal_tool.py:2047` `_harness_safety_block` | legacy envelope gate | **3 → RETIRED BY CONSTRUCTION (Q2)** | `if not os.getenv(RUNTIME_ROOT_ENV, "").strip(): return None` — the entire safety envelope is inert when the variable is unset. **No harness run reaches this line any more.** `envelope_decision` now answers for every *bound scope*, and `_harness_envelope_block` returns before consulting the legacy table whenever it gets a non-`None` answer. The line still exists (it is upstream's) but its remaining reachable callers are runs that bind no scope — i.e. not harness runs — which is the audit's target semantics. Residual + exact diff: §7.1. |
| 12 | `tools/terminal_tool.py:2138` `_log_harness_blocked_attempt` | legacy receipt | **3 → MIRROR-SEAM (Q3)** | Same silent-drop shape as #4: env unset ⇒ the block happened and nothing recorded it. `tools/` is outside the edit boundary, so the ladder ships as the fork-owned `terminal_envelope.record_legacy_block` and the upstream change is a **one-line delegation** (§7.1) rather than a re-derivation. Harness runs are already covered — they never reach this writer (row #11). |
| 13 | `hermes_cli/harness.py:4509` `_cmd_goal_run` (W) | `setdefault` | **3 → REMOVED (Q6)** | One of seven handlers that seeded the variable. Which handlers did and did not is the *process-history* half of the split. All seven removed. |
| 14 | `hermes_cli/harness_parts/persona_commands.py:3581` `_cmd_persona_instance_run_once` (W) | `setdefault` | **3 → REMOVED (Q6)** | ″ |
| 15 | `hermes_cli/harness_parts/persona_commands.py:5068` `_run_free_floating_assignment_once` (W) | `setdefault` | **3 → REMOVED (Q6)** | ″ |
| 16 | `hermes_cli/harness_parts/persona_commands.py:5539` `_cmd_persona_diagnose` (W) | `setdefault` | **3 → REMOVED (Q6)** | ″ |
| 17 | `hermes_cli/harness_parts/runtime_commands.py:579` `_cmd_tick` (W) | `setdefault` | **3 → REMOVED (Q6)** | ″ |
| 18 | `hermes_cli/harness_parts/runtime_commands.py:590` `_cmd_run_until_settled` (W) | `setdefault` | **3 → REMOVED (Q6)** | ″ |
| 19 | `hermes_cli/harness_parts/runtime_commands.py:613` `_cmd_burn_in_run` (W) | `setdefault` | **3 → REMOVED (Q6)** | ″ |

Notably **absent** from this list: `_cmd_mission_chat_message`. The primary work
lane is the one handler that never seeds the variable — which is why the split
surfaced there first. After Q6 that is no longer an anomaly: **no** handler
seeds it, and `tests/agent_runtime/test_runtime_root_request_ordering.py` fails
CI if one starts again.

## 3. Sibling ambient keys in `agent_runtime/`

Every `os.getenv` / `os.environ.get` in `agent_runtime/` was read and
classified. Only one gated on presence.

| Site | Key(s) | Class | Why |
|---|---|---|---|
| `stagec_mcp_visual_provider.py:435` `_marionette_preflight_enabled_for_config` | `HERMES_STAGEC_LAUNCHER_REPO`, `HERMES_LAUNCHER_REPO`, `ETERNIA_LAUNCHER_ROOT` | **2** | **FIXED.** `any(os.getenv(key, "").strip() for key in …)` — bare presence, no validation — while the *consumer* of the same three keys (`_launcher_repo_from_metadata:544`) required `path.is_dir()`. Two readers, one question, different answers: a stale value inherited from process ancestry switched on a preflight that then rebuilt a **different** repo than the variable named, or none at all. The enabled path runs `flutter build`. Both readers now share one helper, `_env_launcher_repo()`, which validates content. |
| `mcp_lane.py:96` `current_entry_point_lane` | `HERMES_ENTRY_POINT_LANE` | **1** | Explicit precedence (pin > env > argv inference > `"unknown"`), value-keyed, never raises. |
| `relay_policy.py:48` `max_relay_depth` | `HERMES_AGENT_CHAT_MAX_DEPTH` | **1** | Value parsed, clamped to `[floor, ceiling]`, typed default on garbage. |
| `events.py:34` `_rotation_cap_bytes` | rotation cap | **1** | Value parsed with an explicit env > config > default ladder. |
| `events.py:86` | `HERMES_EVENT_CONTRACT_STRICT` | **1** | Value compared against an explicit truthy set. |
| `config.py:133` | `HERMES_REDACTION_MODE` | **1** | Value normalized through `normalize_redaction_mode`. |
| `resolution.py:15,68` | `HERMES_REQUIRE_ISOLATED_ROOT` | **1** | Value compared against an explicit falsey set; the *stricter* reading (unset ⇒ off) is the safe default. |
| `resolution.py:145,151` | `HERMES_HOME` | **1** | Value read; empty falls to a platform default. |
| `profile_context.py:38` | `HERMES_PROFILE` | **1** | Value read, then `profile_exists` validated. |
| `profile_readiness.py:189-190` | `HERMES_HOME`, `HERMES_AUTH_HOME` | **1** | Values folded into a cache-key fingerprint — deliberately ancestry-sensitive, because the cached answer genuinely differs per home. Correct use. |
| `preflight.py:456,461,490,494` | docker autostart / exe / `ProgramFiles` … | **1** | Values parsed or joined into candidate paths, each then existence-checked. |
| `burn_in.py:410` | `HERMES_HOME` | **1** | Value or a resolved default, for a report field. |
| `realm_membership.py:118` | credential file | **1** | Explicit argument > env value precedence. |
| `stagec_mcp_visual_provider.py:424` `_auto_rebuild_enabled` | `HERMES_STAGEC_AUTOREBUILD_MARIONETTE` | **1** | Metadata > env > `"1"`, compared against an explicit falsey set. |
| `stagec_mcp_visual_provider.py:450-465,545` | Stage C repo/helper pins | **1** | `_first_nonempty` precedence chains; the repo resolver validates `is_dir()`. |
| `snapshot.py:313,315` | `HERMES_HOME`, `LOCALAPPDATA` | **1** | Diagnostic report of the ambient state. |

**`HERMES_HOME` readers outside `agent_runtime/` — all three now mitigated.**
`tools/skills_sync.py:39`, `tools/skills_tool.py:100` and
`tools/skill_manager_tool.py:151` capture `HERMES_HOME` **at import time** into
module-level constants. That is the same ancestry dependence in a different
shape: in a long-lived serve process the frozen value is whatever home the
*first* import saw, and every later profile switch is invisible to it.

Wave 4 sharpened this. The three are **not** in the same state, and lumping them
hid the one that bites:

| Module | Call-time accessor | Frozen constant used directly | State |
|---|---|---|---|
| `tools/skills_tool.py` | `_skills_dir()` ✔ | only inside the accessor | **mitigated** |
| `tools/skill_manager_tool.py` | `_skills_dir()` ✔ | only inside the accessor | **mitigated** |
| `tools/skills_sync.py` | `_skills_dir()` / `_hermes_home()` / `_manifest_file()` ✔ | only inside the accessors | **mitigated (§7.2, 2026-07-27)** |

The staleness was reproducible, not theoretical — and the guard that reproduced
it is still there, now inverted: `test_env_determinism_audit.py` switches
`HERMES_HOME` after import and asserts each module's accessor **follows** the
switch while the module-level constants stay put (they are the override seam,
not the reader). An AST sweep fails if any function body in `skills_sync` goes
back to reading a frozen constant directly.

**No fork-owned accessor was added, on purpose.** `agent_runtime/` is already
immune by construction: `skill_publishability.py` imports only the two *pure*
helpers (`_dir_hash`, `_read_skill_name`) and derives every skills root itself
(`:200` documents exactly this). A new fork module here would have zero callers
— dead code standing in for a fix. What shipped instead is the drift guard plus
the exact diff (§7.2, applied 2026-07-27 under operator approval), and an AST
test that fails if any `agent_runtime` module ever imports one of the frozen
names.

---

## 4. The serve-cwd concurrency invariant

**Statement of the invariant.**

> The `hermes harness serve` process's working directory is a **per-turn**
> value. It is safe to mutate it process-globally **only while harness turns
> are strictly serialized**. That serialization is currently provided by
> `agent_runtime/profile_runner.py::_WORKDIR_LOCK`, held for the **whole run**
> by `_execute_agent_run` and re-entered around the `chdir` pair by
> `_agent_workdir`. Nothing else enforces it.

**Why the mechanism is a process-global `chdir`.** Mission-chat workdir
grounding (G6, `agent_runtime/mission_chat_workdir.py`, commit `7b8c68942`)
deliberately reused the existing seam rather than inventing a parallel one: the
resolved directory is handed to `AgentRunRequest.workdir`, which
`profile_runner` already honored by calling `os.chdir` and exporting
`TERMINAL_CWD`. That was the right call for the slice — it put the grounding in
front of the `terminal` and `file` tools without touching a single tool — and it
is why the module is pure policy with no side effects of its own. But the seam
it reused mutates **process-global** state, and `serve` is **one process for
every request**.

**What that buys and what it costs.** Serialized turns: correct, and no tool
needs to know anything. Concurrent turns: turn A's relative paths silently
resolve inside turn B's repo, non-deterministically, with **no error anywhere**
— a strictly worse version of the very bug this audit is about, because the
env-variable split at least leaves a diagnosable trace and this leaves none.

**What must change before turn concurrency widens** (any of these, in
preference order):

1. **Per-run resolved absolute paths.** Stop `chdir`-ing. Resolve the workdir
   once per run and thread it to the tools as an explicit parameter — the
   `terminal` tool already accepts an explicit `workdir=` that overrides
   everything (`tools/terminal_tool.py:2036`), and `TERMINAL_CWD` would become
   a per-run value carried on the run context rather than in `os.environ`.
   This retires the shared mutable entirely and is the target shape.
2. **Per-turn subprocess/interpreter isolation.** Each turn gets its own
   process, so the process-global cwd *is* per-turn. Heavier, but it also
   retires the `os.environ` mutations in `persona_profile_context` and every
   module-level `HERMES_HOME` freeze in §3 at the same time.
3. **Keep the lock and accept the serialization.** Legitimate — but then the
   lock is a *product* constraint on lane throughput and must be stated as one,
   not left as an implementation detail nobody knows is load-bearing.

Option 3 is the status quo. This audit's contribution is to make the status quo
**visible**: the invariant is now a test, not a comment.

**The guard.** `tests/agent_runtime/test_serve_cwd_serialization_invariant.py`,
AST-based, mirroring the `test_store_event_invariant.py` precedent (a source
scan cannot be satisfied by a mock and needs no harness in the loop). Three
assertions:

| Test | Locks |
|---|---|
| `test_every_chdir_in_profile_runner_is_guarded_by_the_workdir_lock` | Every `os.chdir` in `profile_runner` is lexically inside a `with` holding `_WORKDIR_LOCK`. A new unguarded `chdir` fails CI. Also fails if the `chdir`s vanish entirely — that means the mechanism changed shape and this doc must change with it. |
| `test_the_run_chokepoint_holds_the_workdir_lock_for_the_whole_run` | `_execute_agent_run` acquires the lock for the **whole run**. Guarding only the `chdir` instant would still let a second turn chdir away *mid-run* — the actual hazard. |
| `test_the_workdir_lock_is_reentrant` | `_WORKDIR_LOCK` is an `RLock`, proven by re-acquiring it. The two guards nest; a plain `Lock` satisfies both AST checks and deadlocks the runner on the first grounded turn. |

Widening concurrency therefore *starts* by failing this test — which is the
point: it routes whoever does it to this section before they ship.

---

## 5. Operator questions (class 3)

Each is decision-ready: what is weak, why it recurs, target shape, blast
radius, test plan. **Recommend clearly; the operator decides scope.**

### Q1 — Should `persona_profile_context` export the runtime root for profile-less personas?

* **What is weak.** `profile_context.py:84-86` early-`yield`s when
  `binding.profile_home is None`, skipping **every** environment export for a
  persona that binds no Hermes profile. That is path 1 of the split (§1) and the
  direct cause of the live fail-open `git push`.
* **Why it recurs.** It is a *silent* difference in the run environment keyed on
  a config field most personas never set. Every future reader of any variable
  exported in that block inherits it, and inherits it invisibly — reader #4 in
  this audit is the second instance in two weeks, in the module written to fix
  the first.
* **Target shape.** Split the two responsibilities the block conflates.
  Profile-home redirection genuinely requires a profile; **runtime-root export
  does not** — `paths.store_root()` answers for every persona. Export the
  runtime root unconditionally, keep `HERMES_HOME`/`HOME`/`HERMES_AUTH_HOME`
  gated on the binding, and emit a typed row when a persona binds no profile so
  the degradation is accounted for rather than inferred.
* **Blast radius.** Every profile-less persona on every harness lane. Anything
  currently relying on "unset" as an implicit signal changes behavior —
  including reader #11, which would become **active** where it is inert today.
  That coupling is why this is a ruling and not a fix: Q1 and Q2 must be decided
  **together**.
* **Test plan.** Extend `tests/agent_runtime/test_profile_context.py` with the
  profile-less case asserting the variable is exported and restored; assert the
  typed no-profile row; re-run `test_terminal_envelope_grants.py` (its
  fail-open/fail-closed reproductions pin both branches by construction).
* **Recommendation.** Do it, jointly with Q2 — but only after Q2 is answered,
  because Q1 alone silently arms the legacy envelope on every lane.
* **RULED + LANDED (wave 4).** `persona_profile_context` now resolves the root
  through `paths.store_root()` when no argument is passed, exports it for every
  persona, and keeps profile-home redirection gated on the binding. Two typed
  rows: `persona_profile_context_no_profile_binding` and
  `persona_profile_context_runtime_root_unresolved` (the resolver refusing under
  `HERMES_REQUIRE_ISOLATED_ROOT` is now *named* rather than silently skipped),
  readable via `current_profile_context_rows()`. The resolution happens BEFORE
  the `HERMES_HOME` override so the exported value is the head resolution, not
  one re-derived through the profile home this context just diverted to. Landed
  in the same change as Q2, after it, exactly as this bullet required.

### Q2 — Should the legacy terminal envelope stop keying on the variable?

* **What is weak.** `tools/terminal_tool.py:2047`: the whole safety envelope is
  inert when `HERMES_AGENT_RUNTIME_ROOT` is unset. On ungoverned lanes (worker
  ticks, free-chat, `hermes chat`, cron, gateway, acp) whether `rm -rf` is
  blocked is decided by process ancestry.
* **Why it recurs.** The variable is being used as a proxy for "am I running
  under the harness?" — a question it does not answer and was never meant to.
  Any new lane inherits the proxy.
* **Target shape.** Key on the same thing the governed path keys on: a bound
  scope. Extend `TerminalEnvelopeScope` to every harness-constructed run
  (`profile_runner` already binds it — the worker lane would simply pass a
  worker lane label), and let "no scope bound" mean "not a harness run" *by
  construction*. The envelope then activates on a fact the policy layer owns.
* **Blast radius.** Potentially large and in the **dangerous direction**:
  turning it on where it is inert today can start blocking commands in flows
  that work now. Conversely leaving it means the harness's headline safety
  control is a coin flip everywhere except mission-chat. Also crosses the
  `tools/` boundary.
* **Test plan.** Per-lane matrix: for each lane × {env set, env unset} assert
  one identical typed outcome. `test_terminal_envelope_grants.py` already has
  the harness for this (`test_worker_lane_keeps_the_legacy_hard_block`,
  `test_chat_lane_keeps_no_envelope_at_all`) — the matrix is an extension, not
  new scaffolding.
* **Recommendation.** Yes, scope-keyed, staged lane by lane starting with the
  worker lane (harness-constructed, so a scope is free) and explicitly **never**
  for `hermes chat` (the operator's own shell — an envelope there is wrong).
* **HISTORICAL — ruled and landed in wave 4, then retired in S61.** Wave 4 made
  every harness-constructed lane scope-keyed. S61 subsequently removed every
  non-mission runtime lane and its ungoverned-lane refusal vocabulary. The live
  guarantee is narrower and simpler: `mission_chat` is the only runtime lane
  that binds a terminal-envelope scope, and an unknown/stale lane name has no
  authority.
  * At wave 4, `envelope_decision` answered for any bound scope and returned a
    typed refusal for a bound but ungoverned lane. That behavior is historical;
    the refusal class and non-mission lane registry were deleted in S61.
  * `_harness_envelope_block` returns as soon as it gets a non-`None` decision,
    so `_harness_safety_block`'s env-presence gate is **unreachable from any
    harness run**. No upstream edit was needed to close it there.
  * At wave 4, scopes were bound at four now-retired construction sites in
    addition to mission chat. Those worker/free-chat/node/root paths and their
    registry were removed in S61; they are not current extension points.
  * **`hermes chat` is excluded structurally, not by convention.** It never
    constructs an `AgentRunRequest`, so no site exists that could bind it, and
    no lane spelling for it exists to bind. A test pins that.
  * No lane-construction site inside `profile_runner.py` needed a new label —
    it consumes `AgentRunRequest.terminal_envelope_scope` and binds it for the
    whole run, which is already the right shape. It was not edited.
* **Test plan, executed.** The per-lane × {env set, env unset} matrix is
  `test_every_harness_lane_answers_identically_with_and_without_the_env_var`
  (5 lanes × gated/ungated × both env states). One existing assertion moved:
  `test_a_grant_does_not_leak_off_the_governed_lane` used to assert `None`
  ("no opinion, fall through to the env-keyed table") and now asserts a typed
  refusal that is explicitly *not* granted — same property, strictly stronger,
  and no longer dependent on the ambient environment to hold.

### Q3 — Do the `tools/` receipt writers get the resolver fallback that `agent_runtime/` just got?

* **What is weak.** `tools/terminal_tool.py:2138` `_log_harness_blocked_attempt`
  returns early when the variable is unset: the command was blocked and
  **nothing recorded it**. Identical shape to reader #4, fixed there this slice.
* **Why it recurs.** "Resolve the runtime root" is spelled independently in
  every module that needs it instead of being imported. Three spellings found;
  two now delegate to `paths.store_root()`, this one does not.
* **Target shape.** One import. `tools/terminal_tool.py` already imports from
  `agent_runtime.terminal_envelope` in `_harness_envelope_block`, so the
  dependency exists — reuse `_audit_root`'s ladder rather than re-deriving it.
* **Blast radius.** Small and one-directional: strictly more receipts, no
  decision changes. The only real question is the `tools/` edit boundary.
* **Test plan.** Mirror `test_env_determinism_audit.py`'s receipt tests against
  the legacy path: env present and env absent, same row, different root.
* **Recommendation.** Yes — this is the lowest-risk item in this list. It needs
  a boundary approval, not a design decision.
* **RULED; fork side LANDED, one upstream line OWED (§7.1).** The ladder is now
  `terminal_envelope.record_legacy_block(command, reason)` — `_audit_root`'s
  three rungs, the legacy row shape verbatim (`ts`/`tool`/`reason`/
  `command_preview`, deliberately **no** `failure_class`, which existing
  `blocked_tool_attempts.jsonl` readers distinguish on) plus
  `audit_root_source`. It returns whether a row was written; `False` means
  "genuinely nowhere to write", never "nobody exported a variable".
* **Scope of what is still owed is much smaller than it was.** Q2 means no
  harness run reaches `_log_harness_blocked_attempt` at all, so the silent drop
  now only affects runs that bind no scope — `hermes chat`, cron, gateway, acp,
  plain CLI. Those are exactly the runs where an envelope block is rare, but
  "rare" is not "recorded", so the diff is still worth applying.
* **Drift is guarded.** `test_the_upstream_receipt_writer_still_has_the_shape_the_doc_s_diff_targets`
  asserts the upstream function still has the `os.getenv(...)` / `if not root:`
  / early-`return` shape §7.1 patches. If upstream reshapes it, that test fails
  and routes the reader back here instead of letting §7.1 rot into a lie.

### Q4 — Should the `runtime_root` preflight check assert something real?

* **What is weak.** `preflight.py:389` can essentially never fail:
  `store_root()` always returns a path (default rung), so the check reports
  `runtime_root=present` for a root that does not exist on this machine. Its
  fix hint tells the operator to configure a variable the check never read.
* **Why it recurs.** A check written against "did the resolver answer?" instead
  of "is the answer usable?". Vacuous checks are worse than absent ones — they
  spend operator trust and pay nothing.
* **Target shape.** Report the resolution honestly: which layer won, the
  resolved path, whether it exists, and whether it looks like a store
  (`tasks/` present — `resolution.suspect_default_root` already computes exactly
  this). Fail on "resolved via the *default* layer AND does not look like a
  store"; pass otherwise.
* **Blast radius.** Preflight starts **failing** on machines that have never
  initialized a store — arguably correct, definitely visible, and it can gate
  flows that pass today. Detail-string consumers would also see richer text.
* **Test plan.** Unit-test the check against a tmp root in each of: env layer
  present + exists, config layer + exists, default layer + missing (the new
  failure), default layer + populated (still passes).
* **Recommendation.** Fix it, but ship it behind the same review as any other
  gate change — a newly-failing preflight in an automated flow is a stop-the-line
  event and should not arrive as a surprise.
* **RULED + LANDED (wave 4).** The check now reports
  `runtime_root=<ok|uninitialized> layer=… path=… exists=… store=…` and fails on
  exactly `resolution.suspect_default_root` — default layer AND no `tasks/`. A
  resolver that *raises* (probe isolation) is its own typed token,
  `runtime_root=unresolvable`, instead of being folded into the same `False` as
  a missing store. The fix hint names what the check actually read.
* **Deliberately NOT a failure: an explicit root that does not exist yet.** An
  env- or config-resolved root is an operator statement; a first run legitimately
  creates it. The check reports `exists=false` honestly and passes. Failure is
  reserved for "nobody said anything **and** nothing is there".
* **Operator-visible change (the stop-the-line warning above, made concrete).**
  `hermes harness preflight` will now FAIL on a machine that has never
  initialized a store and has no `agent_runtime.store_root` configured. That is
  the intended new signal, and it is the one behavior in this wave that can gate
  a flow which passed yesterday.

### Q5 — Should `harness smoke --no-temp-root` write into the LIVE store at all?

* **What is weak.** Surfaced by fix #5. With the variable set — the normal case,
  because seven handlers seed it (§2) — `--no-temp-root` already writes
  `task_smoke`, its runs and its proofs into the **live** store. The old
  cwd-relative fallback masked this on exactly one branch by scattering a stray
  `.hermes-agent-runtime/` directory instead. Making the root deterministic made
  the behavior consistent, which makes the policy question unavoidable.
* **Why it recurs.** "Which root does this write to?" was answered ad hoc per
  call site. Fix #5 removes the ad-hoc answer; it does not decide whether a
  smoke run *should* be writing to the operator's real store.
* **Target shape.** Either (a) a smoke run is always synthetic — drop
  `--no-temp-root` and always use a temp root — or (b) `--no-temp-root` is an
  explicit "yes, pollute the live store" affordance and says so in its help
  text, plus a distinguishable task-id prefix so smoke artifacts can be reaped.
* **Blast radius.** Limited to `hermes harness smoke`, human/CI-invoked.
* **Test plan.** Assert the resolved root and, under (b), the task-id prefix and
  that a reap command removes exactly the smoke artifacts.
* **Recommendation.** (a). The flag's only stated purpose is exercising the
  *configured* root, and `hermes harness preflight` already covers "is the real
  store reachable" without writing to it.
* **Correction to this question's premise.** The flag is not spelled
  `--no-temp-root`. It is `smoke.add_argument("--temp-root",
  action="store_true", default=False)` — so the DEFAULT is the configured root
  and `--temp-root` opts *into* the temp one. Plain `hermes harness smoke` was
  therefore already writing `task_smoke` into the operator's live store. Worse
  than stated, same fix.
* **RULED + LANDED (wave 4), ruling (a).** `_runtime_root()` always creates a
  fresh temp root; the configured-root branch is gone. `run_smoke` keeps its
  `temp_root=` parameter (the CLI still passes it — §7.3) and, when asked for
  the old behavior, ignores it **out loud**: a typed `deprecations` row,
  `smoke_runtime_root_always_temp`. Silently ignoring a flag would be its own
  small lie. The payload also reports `configured_runtime_root` — the store the
  run deliberately did **not** touch — which is what keeps
  `_configured_smoke_root` (and its cwd-independence guarantee) live rather than
  dead code.

### Q6 — Should the seven `setdefault` sites be replaced by one entry-point seam?

* **What is weak.** Seven handlers seed `HERMES_AGENT_RUNTIME_ROOT` (§2 #13-19)
  and the rest do not — including `_cmd_mission_chat_message`, the primary work
  lane. In `serve`, one process runs all of them, so the variable's state at any
  moment is a function of request history.
* **Why it recurs.** `setdefault` at a call site is what you write when there is
  no entry-point seam. Every new handler faces the same choice with no guidance,
  and the majority answer ("don't") is the invisible one.
* **Target shape.** One seam. Either the serve dispatcher and CLI entry
  normalize the runtime environment once per **request** (set *and* restore, the
  `persona_profile_context` shape, so requests cannot leak into each other), or
  no handler sets it at all and every reader goes through `paths.store_root()`.
  **The second is strictly better** and this audit shows it is nearly reachable:
  after fixes #4 and #5, the only production readers that still need the
  variable to be *set* are the two `tools/` sites in Q2 and Q3.
* **Blast radius.** Subprocesses. A harness handler that spawns a child today
  passes the root implicitly through the inherited environment; removing the
  `setdefault`s means those children resolve it themselves (fine — same
  resolver, same config) *unless* a child is launched with a scrubbed
  environment or a different `HERMES_HOME`. That set must be enumerated first.
  Touches `hermes_cli/`.
* **Test plan.** A request-ordering test: run handler A (a seeder) then handler
  B (a non-seeder) in **one** process and assert B's observable outcome is
  identical to running B alone in a fresh process. That test is the direct
  executable statement of this whole audit, and no such test exists today.
* **Recommendation.** Yes, target the second shape — but sequence it **after**
  Q2/Q3, since those are the two readers that still depend on the variable being
  exported.
* **RULED + LANDED (wave 4), the second shape.** All seven `setdefault` sites
  removed, and nothing else in those three files touched — they were under
  concurrent structural refactor, so the diff is exactly seven deletions.
  Sequenced after Q2/Q3 as required: neither `tools/` reader depends on the
  variable being *set* any more — #11 is unreachable from a harness run, and
  #12's fork-owned replacement resolves its own root.
* **Blast radius, enumerated rather than assumed.** The stated risk was children
  launched with a scrubbed environment or a different `HERMES_HOME`. Every
  spawn site in `agent_runtime/` and `hermes_cli/harness*` was read:
  * **No site anywhere in the repo constructs an env containing
    `HERMES_AGENT_RUNTIME_ROOT`.** The only spawns that pass `env=` at all are
    in `stagec_mcp_visual_provider.py`, and they build it as
    `os.environ.copy()` + the server's declared `env` — inherit, never scrub.
  * The only child that is itself a `HERMES_AGENT_RUNTIME_ROOT` reader is
    `runtime_commands.py::_cmd_verify`, which spawns
    `python -m hermes_cli.main harness …` (`:877-893`) with no `env=`.
    `_cmd_verify` was **never a seeder**, so pre-Q6 its children inherited
    whatever ancestry the process had — the bug itself. Post-Q6 they resolve
    through the same ladder off the same inherited `HERMES_HOME` and land on the
    same root, deterministically.
  * Every other spawn runs `git`, `docker`, `flutter` or a proof command
    (`proof_runner._run_bounded_process`, no `env=`) — none reads the variable,
    and a proof command that *is* a hermes invocation resolves it the same way.
  * The one case where a child could see a **different** `HERMES_HOME` is a
    spawn from inside `persona_profile_context` (which diverts it). That case is
    covered by Q1, which exports an explicitly-resolved root there — so the
    child inherits a value that was resolved from the HEAD home rather than
    re-deriving one through the diverted profile home. Q1 and Q6 close each
    other's edge here; that is a second reason they belong in one wave.
* **The request-ordering test exists now** —
  `tests/agent_runtime/test_runtime_root_request_ordering.py`. It runs a REAL
  former seeder (`_cmd_goal_run`, failing fast on a malformed `--bind`, but only
  *after* `load_agent_runtime_config()` — the exact line the `setdefault` sat
  behind), then observes `store_root` / `_audit_root` / `audit_root_source` /
  `_configured_smoke_root` in the same process, and asserts the observation is
  identical to the same observation made alone in a **fresh subprocess**. A
  companion test pins that the handler still reaches that point, so the ordering
  test cannot be silently hollowed out by a restructure. Two AST guards make the
  removal permanent: no module may `setdefault` the variable, and no module
  outside the two save/restore context managers may assign it.

**Executed order:** Q3 (fork seam) → Q2 (semantics) → Q1 (with Q2, never before)
→ Q6 (ancestry removed at the source) → Q4, Q5.

---

## 6. What landed

### 6.1 Wave 3 — the class-2 fixes

| Fix | Site | Guarantee |
|---|---|---|
| Receipt-root ladder | `agent_runtime/terminal_envelope.py` `_audit_root` + `audit_root_source` | A governed decision always leaves a receipt, and the receipt names which rung resolved its root. |
| Configured smoke root | `agent_runtime/smoke.py` `_configured_smoke_root` | Resolves through the one canonical ladder; no cwd dependence. (Wave 4 re-purposed it from "where smoke writes" to "what smoke reports".) |
| Validated launcher-repo env rung | `agent_runtime/stagec_mcp_visual_provider.py` `_env_launcher_repo` + `LAUNCHER_REPO_ENV_KEYS` | The enable predicate and the resolver answer "does the environment name a launcher repo?" identically. A stale value cannot trigger a `flutter build`. |
| Serve-cwd invariant guard | `tests/agent_runtime/test_serve_cwd_serialization_invariant.py` | Turn serialization cannot be removed silently. |

### 6.2 Wave 4 — the six operator rulings

| Q | Fix | Site | Guarantee |
|---|---|---|---|
| **Q1** | Unconditional runtime-root export + typed rows | `agent_runtime/profile_context.py` — `ProfileContextRow`, `current_profile_context_rows`, `_resolved_runtime_root` | Every persona runs with a resolved runtime root, profile-bound or not. The two ways it can degrade are named, not inferred from an absent variable. |
| **Q2** | Historical wave-4 scope-keying; narrowed in S61 | Former non-mission lane sites were deleted; live authority is `agent_runtime/terminal_envelope.py` on `mission_chat` only | A runtime terminal envelope exists only for the persisted mission-chat path. Unknown or retired lane names do not acquire policy authority. `hermes chat` remains excluded structurally. |
| **Q3** | Fork-owned legacy receipt ladder | `agent_runtime/terminal_envelope.py` `record_legacy_block` | A legacy block can leave a receipt with no variable exported. Upstream adoption is one line (§7.1), guarded against drift. |
| **Q4** | An honest preflight assertion | `agent_runtime/preflight.py` `_runtime_root_check` | Reports winning layer / path / exists / store-shape; fails only on an uninitialized default root; a refusing resolver is its own typed token. |
| **Q5** | A smoke run is always synthetic | `agent_runtime/smoke.py` `_runtime_root`, `SMOKE_RUNTIME_ROOT_ALWAYS_TEMP` | `hermes harness smoke` cannot write into the operator's live store, and says so when a caller asks it to. |
| **Q6** | No handler seeds the variable | 7 `setdefault` sites removed across `hermes_cli/harness.py`, `harness_parts/persona_commands.py`, `harness_parts/runtime_commands.py` | A request's runtime root is a function of configuration, never of request history. |

Tests:

| File | Cases | Covers |
|---|---|---|
| `tests/agent_runtime/test_env_determinism_audit.py` | 32 | wave-3 fixes + Q4 + Q5 + §3 freeze drift, each asserted with the variable **present** and **absent** |
| `tests/agent_runtime/test_terminal_envelope_grants.py` | 72 | Q2 per-lane × env matrix, ungoverned-lane refusals, Q3 receipt ladder + upstream drift guard |
| `tests/agent_runtime/test_profile_context.py` | 10 | Q1 profile-less export/restore, typed rows, refusing resolver |
| `tests/agent_runtime/test_runtime_root_request_ordering.py` | 5 | Q6 real-handler request ordering + AST seeding/assignment invariants |
| `tests/agent_runtime/test_serve_cwd_serialization_invariant.py` | 3 | §4 turn serialization (unchanged, still green) |

---

## 7. Operator-owed upstream changes

Everything below is outside the fork's edit boundary (`tools/`, and one argparse
registration contended by a parallel refactor). Each is decision-ready and
exact. None of them gates the wave-4 fixes — they close residuals.

| § | Status | Landed |
|---|---|---|
| 7.1 `tools/terminal_tool.py` receipt delegation | **APPLIED** — operator-approved, wave 5 | 2026-07-27 |
| 7.2 `tools/skills_sync.py` call-time accessors | **APPLIED** — operator-approved, wave 5 | 2026-07-27 |
| 7.3 `smoke --temp-root` retirement | **APPLIED** — operator-approved, wave 6 (one prose instruction refused, see §7.3) | 2026-07-27 |

The diffs are kept verbatim below as the record of what was applied. Each
applied section carries an **Applied** note naming the guard that now watches
the NEW shape: those guards were inverted in the same change, so a silent revert
fails a test instead of quietly rotting this doc back into a lie.

### 7.1 `tools/terminal_tool.py::_log_harness_blocked_attempt` → delegate (Q3)

**Applied 2026-07-27.** The live body is now exactly the `+` side below.
Guards: `tests/agent_runtime/test_terminal_envelope_grants.py::test_the_upstream_receipt_writer_delegates_instead_of_resolving_its_own_root`
(upstream must keep delegating — no re-derived root, no locally-built row, no
silent early return) and `::test_legacy_block_receipt_keeps_the_row_keys_the_upstream_writer_emits`,
which moved from reading upstream's source text — the delegation deleted it —
to asserting the four legacy row keys on the row the fork-owned writer emits.

Replace the whole body. The fork-owned function already exists and is tested.

```diff
 def _log_harness_blocked_attempt(command: str, reason: str) -> None:
-    root = os.getenv("HERMES_AGENT_RUNTIME_ROOT", "").strip()
-    if not root:
-        return
-    try:
-        path = Path(root).expanduser() / "blocked_tool_attempts.jsonl"
-        path.parent.mkdir(parents=True, exist_ok=True)
-        event = {
-            "ts": time.time(),
-            "tool": "terminal",
-            "reason": reason,
-            "command_preview": command[:500],
-        }
-        with path.open("a", encoding="utf-8", newline="\n") as handle:
-            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
-    except Exception:
-        logger.warning("Failed to write Harness blocked-command audit", exc_info=True)
+    try:
+        from agent_runtime.terminal_envelope import record_legacy_block
+
+        record_legacy_block(command, reason)
+    except Exception:
+        logger.warning("Failed to write Harness blocked-command audit", exc_info=True)
```

Row shape is preserved verbatim, plus `audit_root_source`. Blast radius: one
direction only — strictly more receipts, no decision changes.

### 7.2 `tools/skills_sync.py` → call-time home (§3)

**Applied 2026-07-27**, with one deliberate extension noted below. Guards:
`tests/agent_runtime/test_env_determinism_audit.py::test_skills_sync_keeps_the_constants_as_seams_and_reads_them_at_call_time`
(the constants survive AND an accessor exists for each),
`::test_no_skills_sync_function_body_reads_a_frozen_constant_directly` (an AST
sweep — one surviving in-body `SKILLS_DIR` is the whole bug back),
`::test_the_constants_are_still_the_override_seam_the_doc_promised` (a patched
constant still wins; an unpatched module follows a live profile switch), and
`::test_every_skills_module_resolves_its_root_at_call_time`, whose parametrize
list grew from two modules to three.

**Extension applied on top of the literal diff.** The diff below defines only
`_skills_dir()`, and the prose then says to replace the `MANIFEST_FILE` /
`HERMES_HOME` uses with `_skills_dir() / ".bundled_manifest"` /
`get_hermes_home()`. Taken literally that contradicts this section's own opening
sentence: it would leave `MANIFEST_FILE` and `HERMES_HOME` as module-level names
that nothing reads, so patching either would silently do nothing — and nine
existing `tests/tools/test_skills_sync.py` cases patch exactly those two. Both
therefore got the SAME accessor pattern (`_manifest_file()` / `_hermes_home()`,
each with its own `_AT_IMPORT` snapshot), which keeps every documented override
seam load-bearing and still makes a profile switch visible. Zero test churn:
the file's failure set is unchanged (one pre-existing Windows path-separator
failure in `TestComputeRelativeDest`).

Give it the accessor its two sibling modules already have, keeping the
module-level names (they are load-bearing for tests and external patchers).

```diff
 HERMES_HOME = get_hermes_home()
 SKILLS_DIR = HERMES_HOME / "skills"
 MANIFEST_FILE = SKILLS_DIR / ".bundled_manifest"
+_SKILLS_DIR_AT_IMPORT = SKILLS_DIR
+
+
+def _skills_dir() -> Path:
+    """The ACTIVE profile's skills dir, resolved at call time.
+
+    Mirrors tools/skills_tool.py and tools/skill_manager_tool.py: honor an
+    explicitly patched module-level SKILLS_DIR (tests), otherwise re-resolve
+    from the live profile-scoped HERMES_HOME on every call.
+    """
+    configured = Path(SKILLS_DIR)
+    if configured != _SKILLS_DIR_AT_IMPORT:
+        return configured
+    return get_hermes_home() / "skills"
```

Then replace the ~25 direct uses of `SKILLS_DIR` / `MANIFEST_FILE` /
`HERMES_HOME` in that module with `_skills_dir()` / `_skills_dir() /
".bundled_manifest"` / `get_hermes_home()`. **Read the wipe guard at `:733-750`
before touching it** — it requires a strict-child relationship precisely to stop
a bad `SKILLS_DIR` from deleting a home, and it must keep comparing against the
same value the deletion uses.

Blast radius: every profile switch in a long-lived serve becomes visible to
skills sync — which is the point, and is the behavior the other two modules
already have.

**Wipe guard, as landed.** `_rmtree_writable` now resolves `skills_root`
through `_skills_dir()` — the same accessor every `dest` in the five call sites
is computed from (`_compute_relative_dest` and the two
`_skills_dir() / Path(*install_path.split("/"))` sites; the `.bak` paths are
`dest.with_suffix()` siblings). That symmetry is the requirement: a guard
reading a frozen root while the deletions followed a live switch would compare
two different homes and either refuse every legitimate removal or wave through
the one it exists to stop. The comment at that site now says so.

### 7.3 the `smoke` subparser → retire the `--temp-root` flag (Q5)

**Applied 2026-07-27** (wave 6). Both literal hunks below are in. Guards:
`tests/hermes_cli/test_harness_cli.py::test_the_smoke_subparser_no_longer_registers_temp_root`
(the flag is gone from the parser AND `--temp-root` is now a parse error) and
the existing `::test_harness_parser_exposes_smoke`, which moved off the flag.
`run_smoke`'s `temp_root=` parameter was deliberately **kept**: it is still the
seam six Q5 tests drive directly (`test_env_determinism_audit.py` §"a smoke run
is synthetic, always"), and the CLI no longer being able to set it is exactly
the point — the handler keeps enforcing the ruling for any programmatic caller
that still passes `temp_root=False`.

**One prose instruction below was REFUSED, and it was wrong, not merely
risky.** The paragraph after the diffs says `hermes_cli/harness.py`'s
`import os` "became unused … Remove it with this diff." It is not unused.
`harness._load_command_parts()` `exec`s `harness_parts/*.py` into harness.py's
**own globals**, so those files carry no import block and every name they use
must be imported by harness.py — and two of them read `os`
(`persona_commands._persona_chat_fault_injection`, and `runtime_commands`'s
`HERMES_HOME` report row). Grepping harness.py's own source says "unused",
which is how the audit reached that conclusion; deleting the import raises
`NameError` on a **live chat turn** and on nothing a test run would notice.
Guard: `tests/agent_runtime/test_env_determinism_audit.py::test_the_harness_keeps_the_import_its_execd_command_parts_depend_on`
— it asserts the import survives *and* that the exec'd parts still read it, so
the day they genuinely stop, the guard says so instead of silently permitting a
removal that was only ever accidentally safe.

In `hermes_cli/harness.py`, where the `smoke` subparser is built:

```diff
-    smoke.add_argument("--temp-root", action="store_true", default=False)
```

…and drop the argument where `_cmd_smoke` calls the handler in
`harness_parts/runtime_commands.py`:

```diff
-    data = run_smoke(temp_root=args.temp_root, no_model=args.no_model)
+    data = run_smoke(no_model=args.no_model)
```

~~While in that file: `hermes_cli/harness.py`'s `import os` became unused when
the Q6 seeder was removed from `_cmd_goal_run` (it was that module's only `os`
reference)… Remove it with this diff.~~ **Struck 2026-07-27 — see the refusal
note at the top of this section. `import os` is load-bearing for the exec'd
command parts; it stays.**

None of this was landed in wave 4 because both files were under concurrent
structural refactor by a parallel agent, and a two-file signature change is
exactly the kind of edit that loses a race. The behavior was already correct
without it: the fork-owned handler ignored the flag and emitted the typed
`smoke_runtime_root_always_temp` deprecation, so this was cleanup, not a fix —
which is why `run_smoke`'s `temp_root=` parameter did **not** go with it (it is
the seam the Q5 tests drive, and the ruling still needs enforcing there).
