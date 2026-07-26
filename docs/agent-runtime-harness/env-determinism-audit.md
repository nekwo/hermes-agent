# Ambient-environment determinism audit (2026-07-26)

Status: **audit complete; three class-2 fixes landed; six operator questions
open.** Scope: every reader of `HERMES_AGENT_RUNTIME_ROOT` in the repo, plus
sibling ambient environment keys in `agent_runtime/` that gate behavior on
**presence** rather than **validated content**.

Parent: [`mission-chat-terminal-envelope-grants.md`](mission-chat-terminal-envelope-grants.md)
§7.3 — *"anything else keyed on `HERMES_AGENT_RUNTIME_ROOT` inherits the same
nondeterminism. Worth an explicit audit of that variable's other readers."*
This is that audit. Precedent instance:
[`mission-chat-lane-gap-audit.md`](mission-chat-lane-gap-audit.md) G4/G5b.

---

## 1. The bug class, stated once

A reader is **nondeterministic** when its behavior is decided by whether an
environment variable *happens to be set in this process*, rather than by a
declared resolution ladder over validated content.

Two independent paths put `HERMES_AGENT_RUNTIME_ROOT` in either state, neither
of them a policy statement:

* **Profile binding.** `agent_runtime/profile_context.py:82-84` — a persona
  with no `hermes_profile` takes the `binding.profile_home is None`
  early-`yield`, so `persona_profile_context` exports *nothing* for that run.
* **Process history.** The variable is `os.environ.setdefault`-ed by *some*
  harness command handlers and not others (§4). A long-lived `hermes harness
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
| 5 | `agent_runtime/smoke.py:55` `run_smoke` | `--no-temp-root` root | **2** | **FIXED.** Was `os.environ.get(RUNTIME_ROOT_ENV, ".hermes-agent-runtime")` — **two** nondeterminisms in one expression: presence-keyed, and the fallback is **relative to the process cwd**, which mission-chat workdir grounding now `chdir`s per turn (§3). Replaced with `paths.store_root()`, the one definition of "the configured root". Env-set behavior byte-identical. See operator question **Q5** for the residual policy issue this exposed. |
| 6 | `agent_runtime/profile_context.py:110` (W) | the exporter | **3** | The `profile_home is None` early-`yield` at `:82-84` is path 1 of the split. Retiring it is live-visible on every profile-less persona. → **Q1**. |
| 7 | `agent_runtime/profile_context.py:88-91` (W) | save/restore | **1** | Snapshots the prior value and restores it in `finally`, including the `None` case (pops rather than writing `"None"`). Correct scoping; not a gate. |
| 8 | `agent_runtime/smoke.py:24-39` (W) `_runtime_root` | save/restore | **1** | Same correct save/restore shape as #7. |
| 9 | `agent_runtime/preflight.py:389` `_runtime_root_check` | preflight check | **1** (weak) | Deterministic — it calls `store_root()`, which reads the value through #1. But the assertion is **vacuous**: the resolver always returns a non-empty path (the default rung), so `ok = bool(str(root).strip())` can only be false if `store_root()` *raises*. It reports `runtime_root=present` for a root that does not exist, and its fix hint names a variable it never actually checked. → **Q4**. |
| 10 | `agent_runtime/snapshot.py:314` `_runtime_paths_diagnostic` | diagnostic | **1** | Reports the raw value or `<unset>` alongside the resolved paths. A report of the ambient state is the correct use of a read — it is how an operator *sees* the split rather than being subject to it. |
| 11 | `tools/terminal_tool.py:2047` `_harness_safety_block` | legacy envelope gate | **3** | The precedent instance, still live on every **ungoverned** lane. `if not os.getenv(RUNTIME_ROOT_ENV, "").strip(): return None` — the entire safety envelope is inert when the variable is unset. Governed mission-chat turns no longer reach it; worker ticks, free-chat, `hermes chat`, cron, gateway and acp all still do. → **Q2**. |
| 12 | `tools/terminal_tool.py:2138` `_log_harness_blocked_attempt` | legacy receipt | **3** | Same silent-drop shape as #4, on the legacy path: env unset ⇒ the block happened and nothing recorded it. The fix is #4's, applied one file over — but `tools/` is outside this slice's edit boundary. → **Q3**. |
| 13 | `hermes_cli/harness.py:4509` `_cmd_goal_run` (W) | `setdefault` | **3** | One of six handlers that seed the variable. Which handlers do and do not is the *process-history* half of the split. → **Q6**. |
| 14 | `hermes_cli/harness_parts/persona_commands.py:3562` `_cmd_persona_instance_run_once` (W) | `setdefault` | **3** | ″ |
| 15 | `hermes_cli/harness_parts/persona_commands.py:5049` `_run_free_floating_assignment_once` (W) | `setdefault` | **3** | ″ |
| 16 | `hermes_cli/harness_parts/persona_commands.py:5520` `_cmd_persona_diagnose` (W) | `setdefault` | **3** | ″ |
| 17 | `hermes_cli/harness_parts/runtime_commands.py:579` `_cmd_tick` (W) | `setdefault` | **3** | ″ |
| 18 | `hermes_cli/harness_parts/runtime_commands.py:590` `_cmd_run_until_settled` (W) | `setdefault` | **3** | ″ |
| 19 | `hermes_cli/harness_parts/runtime_commands.py:613` `_cmd_burn_in_run` (W) | `setdefault` | **3** | ″ |

Notably **absent** from this list: `_cmd_mission_chat_message`. The primary work
lane is the one handler that never seeds the variable — which is why the split
surfaced there first.

## 3. Sibling ambient keys in `agent_runtime/`

Every `os.getenv` / `os.environ.get` in `agent_runtime/` was read and
classified. Only one gated on presence.

| Site | Key(s) | Class | Why |
|---|---|---|---|
| `stagec_mcp_visual_provider.py:435` `_marionette_preflight_enabled_for_config` | `HERMES_STAGEC_LAUNCHER_REPO`, `HERMES_LAUNCHER_REPO`, `ETERNIA_LAUNCHER_ROOT` | **2** | **FIXED.** `any(os.getenv(key, "").strip() for key in …)` — bare presence, no validation — while the *consumer* of the same three keys (`_launcher_repo_from_metadata:552`) required `path.is_dir()`. Two readers, one question, different answers: a stale value inherited from process ancestry switched on a preflight that then rebuilt a **different** repo than the variable named, or none at all. The enabled path runs `flutter build`. Both readers now share one helper, `_env_launcher_repo()`, which validates content. |
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

**`HERMES_HOME` readers outside `agent_runtime/` — flagged, not fixed.**
`tools/skills_sync.py:39`, `tools/skills_tool.py:100` and
`tools/skill_manager_tool.py:151` capture `HERMES_HOME` **at import time** into
a module-level constant. That is the same ancestry dependence in a different
shape: in a long-lived serve process the frozen value is whatever home the
*first* import saw, and every later profile switch is invisible to it. Already
known — `agent_runtime/skill_publishability.py:200` documents it — and the
call-time accessors in those modules are the mitigation. Out of this slice's
boundary (`tools/`), and the module-level names are load-bearing for tests. → **Q3**
covers the `tools/` boundary generally.

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
   everything (`tools/terminal_tool.py:2037`), and `TERMINAL_CWD` would become
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

* **What is weak.** `profile_context.py:82-84` early-`yield`s when
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

### Q5 — Should `harness smoke --no-temp-root` write into the LIVE store at all?

* **What is weak.** Surfaced by fix #5. With the variable set — the normal case,
  because six handlers seed it (§2) — `--no-temp-root` already writes
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

### Q6 — Should the six `setdefault` sites be replaced by one entry-point seam?

* **What is weak.** Six handlers seed `HERMES_AGENT_RUNTIME_ROOT` (§2 #13-19)
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

**Suggested order:** Q3 (trivial, one-directional) → Q2 (decides the semantics)
→ Q1 (safe once Q2 is answered) → Q6 (removes the ancestry at the source) →
Q4, Q5 (independent, small).

---

## 6. What landed in this slice

| Fix | Site | Guarantee |
|---|---|---|
| Receipt-root ladder | `agent_runtime/terminal_envelope.py` `_audit_root` + `audit_root_source` | A governed decision always leaves a receipt, and the receipt names which rung resolved its root. |
| Configured smoke root | `agent_runtime/smoke.py` `_configured_smoke_root` | `--no-temp-root` resolves through the one canonical ladder; no cwd dependence. |
| Validated launcher-repo env rung | `agent_runtime/stagec_mcp_visual_provider.py` `_env_launcher_repo` + `LAUNCHER_REPO_ENV_KEYS` | The enable predicate and the resolver answer "does the environment name a launcher repo?" identically. A stale value cannot trigger a `flutter build`. |
| Serve-cwd invariant guard | `tests/agent_runtime/test_serve_cwd_serialization_invariant.py` | Turn serialization cannot be removed silently. |

Tests: `tests/agent_runtime/test_env_determinism_audit.py` (16 cases — every
fix asserted with the variable **present** and **absent**),
`tests/agent_runtime/test_serve_cwd_serialization_invariant.py` (3 cases).
