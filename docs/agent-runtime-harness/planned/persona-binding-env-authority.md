# Planned — the persona binding as the env authority

**Status:** B-1 shipped; B-2 / B-3 / B-4 not started. **Owner doc:**
[`../05-chat-turn-lane.md`](../05-chat-turn-lane.md).
**Source:**
[`../archive/2026-08-22-pre-consolidation/PERSONA_PROFILE_BINDING_AUTHORITY_PLAN_2026-08-16.md`](../archive/2026-08-22-pre-consolidation/PERSONA_PROFILE_BINDING_AUTHORITY_PLAN_2026-08-16.md),
re-checked against HEAD 2026-08-22.

Operator ruling this implements (relayed, not re-measured): *"let the per persona
binding resolve it — we need to make it the standard so there is less problems with the
env being used."*

---

## What is already true

**Chat turns already obey the persona binding, not the launcher env.** Every persona run
enters `persona_profile_context(binding)` inside the serve child
(`agent_runtime/profile_runner.py:798`), which redirects `HERMES_HOME` / `HOME` /
`HERMES_AUTH_HOME` to the persona's bound profile home
(`agent_runtime/profile_context.py:176-233`). The launcher's global `HERMES_HOME` is
already only the ambient default those turns swap away from. What the ruling asks for is
not a new mechanism; it is closing the lanes that still bypass this one.

**B-1 landed.** A process that legitimately knows the operator head publishes it, and a
process that names no head of its own reads the declaration instead of degrading to its
own profile home — `declared_chat_head_home` (`agent_runtime/chat_session_scope.py:659`),
written at `harness serve` boot by `root_anchor.publish_store_root_anchor` into the
platform-default `config.yaml`, sitting on the `CONFIG_DECLARED` rung of the resolution
ladder (`chat_session_scope.py:43-75`). It earns that rung by being reachable where the shared-root pointer is
not: the pointer needs a resolvable store root, which is exactly how the 2026-08-12
shadow runtime hid.

One related audit correction also landed: exporting the runtime root is now
UNCONDITIONAL in `persona_profile_context`, split from profile-home redirection, because
`paths.store_root()` answers for every persona while the old `profile_home is None`
early-`yield` skipped both (`profile_context.py:177-183`).

---

## The gap that remains

**The bypass lane is child processes that inherit a profile-shaped env.**
`hermes_cli/main.py`'s profile pre-parse still resolves in ladder order: explicit
`--profile` flag → **trust an inherited `HERMES_HOME` whenever its parent directory is
named `profiles`** (`:652-669`, the `Path(hermes_home_env).parent.name == "profiles"`
heuristic, issue #22502) → the sticky `active_profile` marker.

Step 1.5 is the trap. Any hermes child spawned from the launcher's env
(`HERMES_HOME=…\profiles\base`) without an explicit flag stays on whatever profile the
parent happened to hold, rather than on the binding of the persona whose turn spawned it.
The heuristic is correct for what it was written for — a systemd unit hardcoding the
hermes ROOT must still honour `hermes profile use` — and removing it is not the fix.

**B-4 is also absent.** `agent_runtime/agent_create.py` does not handle `hermes_profile`
at all (grep: no matches), so a created agent gets no explicit binding at creation and
the null-binding backfill has nothing to run against.

---

## Stages, as the plan left them

| stage | what | state at HEAD |
| --- | --- | --- |
| B-1 | a gateway (and any profile-homed service) declares its home, typed, at boot | **SHIPPED** — `chat_session_scope.declared_chat_head_home:659` |
| B-2 | every child spawned inside a persona turn carries the binding | not started |
| B-3 | the launcher's global setting is demoted to a default, in title, copy and docs | not started (launcher repo) |
| B-4 | explicit binding at creation; dry-run backfill for the nulls | not started — no `hermes_profile` in `agent_create.py` |
| B-5 | verification + ledger, no code | pending B-2/B-4 |

---

## Gates

- **B-2 must not be a second answer to "which home".** `chat_session_scope` is the ONE
  authority for the chat SessionDB (`:1-6`) and its ladder already ranks relay context
  above env above recorded above pointer above declaration. A child-spawn fix threads the
  binding INTO that ladder; it does not open a parallel one.
- **Env and relay context must keep winning.** The recorded/declared rungs may only ever
  NARROW the ambient fallback (`chat_session_scope.py:36-38`). A B-2 change that lets a
  binding override an explicit `HERMES_HEAD_HOME` inverts that and lets a nested relay
  turn escape the operator that started it.
- **B-4 needs the refusal vocabulary that already exists.** `agent_create` distinguishes
  `persona_not_found` from `persona_roster_unavailable` on purpose — one means "send a
  different id", the other "send the same id when the runtime is healthy"
  (`agent_create.py:207-217`). An invalid `hermes_profile` at creation needs its own
  third spelling rather than being folded into either.
- **Measure under the right home.** The running Launcher's serve spawns with
  `HERMES_HOME=profiles/base`. Verifying B-2 under any other profile measures a different
  runtime — which is the same class of mistake the whole plan is about.

---

## Carry-forward (not re-measured here)

- The five live persona bindings the plan recorded (`backend_dev→backend-dev`,
  `base→base`, `dev→launcher-dev`, `neko_supervisor→neko`, `qa→launcher-qa`), all
  targeting profiles that exist on disk. Live-root observation from 2026-08-16.
- The 2026-08-12 shadow-runtime incident, cited as the reason `CONFIG_DECLARED` outranks
  ambient resolution.
