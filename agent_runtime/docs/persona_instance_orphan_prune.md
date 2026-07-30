# Persona-instance orphan prune + probe isolation (2026-07-12)

## Problem

Orphaned `persona_instances/*.json` tombstones projected into Mission Control's
runtime HUD as phantom "on level" agents. Two classes:

- **`orphan-no-profile`** — a `profile:<name>` operator-channel instance whose
  backing profile template was deleted from disk (live evidence: three Stage-C
  QA probes `codex_{create,display,no_display}_probe` created against the LIVE
  store instead of an isolated temp home).
- **`legacy-role`** — an instance under a mothballed role (the retired `pm`
  slot; see `MOTHBALLED_ROLES` in `personas.py`).

The snapshot builds the instance list from `PersonaInstanceStore.list_all()`
(a fresh glob), and the only cleanup lanes were duplicate-id fold
(`reconcile_persona_instances`) and the task-bound sweep
(`sweep_orphaned_task_bound_instances`) — neither had an orphan/legacy lane, so
these rows lingered forever with no prune and no accounting.

## Fix (all fork-owned: `agent_runtime/` + `hermes_cli/`)

- **Pure classifier** `classify_orphan_persona_instances(rows, *,
  backed_persona_ids, backed_profile_names, now, profile_catalog_authoritative)`
  in `persona_instance_identity.py` → `{"prunable": [...], "held": [...]}`, each
  entry a typed `reason`. A row is an orphan candidate only when its backing
  persona/profile is absent from the **backed universe** OR its role/persona is
  mothballed; a real product agent (backed and not mothballed) is skipped
  entirely.
- **Backed universe** = `backed_persona_identity()`: persisted persona records
  plus live profile templates. No persona ids are synthesized by code.
- **Blind-catalog guard**: when the profile template catalog can't be positively
  enumerated (empty/failed read), `profile:*` rows are NEVER classified
  `orphan-no-profile` (a missing template is indistinguishable from an
  unreadable catalog — a blind catalog must not reap real profile channels).
- **Protection (held, never pruned)**: active worker/run/assignment/task binding,
  `mode == "task_bound"`, fresh heartbeat (<24h), recent `updated_at` (<48h), or
  a still-seeded mothballed persona. The reconcile prune re-verifies with the
  cross-store `_has_live_binding` as a belt.
- **Mothballed set** single-sourced in `personas.py`: `MOTHBALLED_ROLES` /
  `MOTHBALLED_ROLE_TOKENS` / `MOTHBALLED_PERSONA_IDS` (start = `pm`). Consumed
  only by the classifier — no hand-rolled `role == "pm"` checks. (Scattered
  `"pm"` **routing aliases** in `blueprints/resolve.py`, `mission_goal.py`, etc.
  are alias maps, not liveness checks — left as debt.)
- **Prune lane** appended to `reconcile_persona_instances` (phase 2 after the
  duplicate fold): archives prunable rows to
  `persona_instances_archive/<ts>_prune/` (never deletes), emits
  `persona_instance.pruned` (registered in `decision_contract_registry.py`:
  required `persona_instance_id, reason`; optional `persona_id, role,
  profile_id, updated_at`). Idempotent; `--dry-run` writes nothing.
- **Snapshot accounting** in `snapshot.py::_parity_warnings`, mirroring the
  `duplicate_persona_instance` lane: `orphaned_persona_instance` (would-prune) +
  `held_orphan_persona_instance` (protected), so nothing is silently dropped.
- **CLI**: the existing `harness persona-instance reconcile [--dry-run] [--json]`
  verb now prints `pruned=N held=N` and per-row reasons.

## Probe isolation (`HERMES_REQUIRE_ISOLATED_ROOT`)

New env contract so Stage-C / QA probe runs can never persist into live again.
When `HERMES_REQUIRE_ISOLATED_ROOT` is truthy, `paths.store_root()` (the single
root chokepoint) calls `resolution.assert_probe_isolation`, which raises
`ProbeIsolationViolation` unless the runtime root was won by the **env** layer
AND its basename starts with `agent-runtime-probe`. Probe recipes must export:

```
HERMES_REQUIRE_ISOLATED_ROOT=1
HERMES_AGENT_RUNTIME_ROOT=%TEMP%\agent-runtime-probe-<stamp>
```

**Residual gap**: a rogue run that sets neither the marker nor a root is still
unguarded — mitigated by the loud snapshot warning + the reversible reconcile
prune. Deeper MCP-side mandatory-root enforcement is a follow-up.

## Steering referential integrity (2026-07-20)

A second stale-instance class can remain even when every persona-instance row
is backed: a child row may retain a shape-valid `spawned_by` / `steered_by` id
after the parent placement row has been retired or reaped. JSON decoding cannot
detect this; the fields are foreign keys and must be checked against the live
`persona_instances` key set.

- `PersonaInstanceStore` releases child backlinks before every sanctioned
  retire/reap row removal. Remaining parents and child mission context are
  preserved; the normal `persona_instance.steered` event records the change.
- `reconcile_persona_instances` has a final referential-integrity phase that
  removes already-stranded parent ids and reports `steering_repaired_count`.
- Snapshot parity emits typed `fk_miss` warnings for unresolved
  `persona_instance.spawned_by` and `persona_instance.steered_by` targets, with
  the existing reconcile command as the repair instruction.

This complements schema/contract-version checks: schema validity proves the
JSON shape; parity FK checks prove that its live object references resolve.

## Proof (2026-07-12)

- 1730 `tests/agent_runtime` passed; new: classifier truth table + legacy-seeded
  hold + reconcile dry/apply/idempotent + snapshot warning lanes +
  `test_probe_isolation.py`.
- Live `reconcile --dry-run --json` against `X:\Eternia\.hermes\agent-runtime`
  (already manually cleaned): `pruned_count 0`, 6 real agents preserved, no
  writes.
- Same dry-run against a temp copy with the 4 archived tombstones restored:
  `pruned_count 4` (pm → `legacy-role`; 3 codex → `orphan-no-profile`), all 6
  real agents untouched, no archive dir created.
- Isolation guard: marker set + live/default root → `ProbeIsolationViolation`
  raised at `store_root()` before any I/O.

Deploy note: pushed on `feat/persona-instance-orphan-prune`; live effect needs a
`hermes serve` restart (operator-gated).
