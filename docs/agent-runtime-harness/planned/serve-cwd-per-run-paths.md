# Planned — retire the process-global `chdir` before turn concurrency widens

**Status:** NOT IMPLEMENTED. Designed, unscheduled. The status quo is deliberate and is
guarded by a test that routes whoever changes it here.
**Owning doc:** [`../04-boot-and-lifecycle.md`](../04-boot-and-lifecycle.md), Invariant 10.
**Source:** `archive/2026-08-22-pre-consolidation/env-determinism-audit.md` §4.

## The invariant, and why it is load-bearing

> The `hermes harness serve` process's working directory is a **per-turn** value. It is
> safe to mutate it process-globally **only while harness turns are strictly
> serialized.**

That serialization is `agent_runtime/profile_runner.py::_WORKDIR_LOCK` — an `RLock`
(`profile_runner.py:1434`) held for the WHOLE run by `_execute_agent_run` and re-entered
around the `chdir` pair by `_agent_workdir` (`profile_runner.py:1398`). **Nothing else
enforces it.**

Serve is one process for every request, and `DEFAULT_POOL_SIZE = 4`
(`hermes_cli/harness_parts/serve.py:236`). The lock is what stops the pool from being a
correctness problem.

## What the current shape costs

Serialized turns: correct, and no tool needs to know anything. Concurrent turns: turn
A's relative paths silently resolve inside turn B's repo, non-deterministically, **with
no error anywhere** — strictly worse than the env-variable class the audit was about,
because an env split at least leaves a diagnosable trace and this leaves none.

The lock is therefore a **product constraint on lane throughput**, not an implementation
detail. Stating it is this document's main job.

## The guard that already exists

`tests/agent_runtime/test_serve_cwd_serialization_invariant.py` — AST-based, so it
cannot be satisfied by a mock. Three assertions:

| test | locks |
|---|---|
| `test_every_chdir_in_profile_runner_is_guarded_by_the_workdir_lock` | Every `os.chdir` in `profile_runner` is lexically inside a `with` holding `_WORKDIR_LOCK`. Also reds if the `chdir`s vanish entirely — that means the mechanism changed shape and this document must change with it. |
| `test_the_run_chokepoint_holds_the_workdir_lock_for_the_whole_run` | `_execute_agent_run` holds it for the whole run. Guarding only the `chdir` instant would still let a second turn chdir away MID-RUN, which is the actual hazard. |
| `test_the_workdir_lock_is_reentrant` | `_WORKDIR_LOCK` is an `RLock`. A plain `Lock` satisfies both AST checks and deadlocks the runner on the first grounded turn. |

**Widening concurrency starts by failing this test.** That is the design, not an
obstacle.

## Options, in preference order (from the audit, unchanged)

1. **Per-run resolved absolute paths — the target shape.** Stop `chdir`-ing. Resolve the
   workdir once per run and thread it to the tools explicitly; the `terminal` tool
   already accepts an explicit `workdir=` that overrides everything, and `TERMINAL_CWD`
   becomes a per-run value on the run context rather than in `os.environ`. Retires the
   shared mutable entirely.
2. **Per-turn subprocess isolation.** Each turn gets its own process, so the
   process-global cwd IS per-turn. Heavier, but it also retires the `os.environ`
   mutations in `persona_profile_context` at the same time.
3. **Keep the lock and accept the serialization.** The status quo — legitimate, provided
   it is stated as a product constraint.

## Gate

1. The AST guard is REPLACED, not deleted — by an assertion that no `os.chdir` reaches
   the runner at all, plus a test that two concurrent grounded turns resolve relative
   paths in their own workdirs.
2. `TERMINAL_CWD` is proven to be per-run rather than process-global, by a test that
   runs two turns concurrently and reads it from both.
3. This document and Invariant 10 in the owning doc are updated in the same change. An
   invariant that has quietly stopped being true is worse than one that was never
   written.
