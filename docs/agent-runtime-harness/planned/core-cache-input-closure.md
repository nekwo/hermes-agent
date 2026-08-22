# Planned — widen the core cache's fingerprint input closure

**Status:** not implemented. **Domain:** runtime data and shapes.
**Opened:** 2026-08-22, from live receipts.

## The problem

`agent_runtime/core_cache.py` validates a persisted snapshot core by re-stat'ing
every input the build read and comparing `(path, mtime_ns, size)` triples. When
consecutive write-backs each produce a key that disagrees with the one before
it, no later process can ever be served the cache: the lane costs a write per
build and buys nothing. That state emits
`snapshot_core_cache never_converged` (`core_cache.py:322`).

It is currently the steady state on the operator's machine.

## Evidence

`X:\Eternia\.hermes\profiles\base\logs\agent.log`, 10 firings between
2026-08-20 18:21 and 2026-08-22 13:42:

| When | `diff_scope` | `changed` | Named paths |
| --- | --- | --- | --- |
| 08-20 18:21 | `every_pass` | 60 | `realm_sync/<realm>/.git/index`, `.git/logs/HEAD`, `.git/objects/pack/*.pack`, … |
| 08-20 18:30 | `last_pair` | 2 | `events_archive/events.81417412.jsonl`, `profiles/alice/config.yaml` |
| 08-20 18:38 | `last_pair` | 2 | same pair |
| 08-20 20:37 | `every_pass` | 2 | live events slice, `persona_instances/personainst_neko_supervisor_agent_f6f7a51b.json` |
| 08-21 13:51 | `last_pair` | 1 | `profiles/alice/config.yaml` |
| 08-21 17:18 | `every_pass` | 1 | `profiles/base/state.db` |
| 08-21 17:22 | `every_pass` | 1 | `profiles/base/state.db-wal` |
| 08-21 22:00 | `last_pair` | 1 | live events slice |
| 08-22 10:48 | `last_pair` | 1 | live events slice |
| 08-22 13:42 | `every_pass` | 1 | `profiles/base/state.db-wal` |

Receipt format, verbatim:

```
snapshot_core_cache never_converged builds=3 diff_scope=every_pass changed=1
  diff=X:\Eternia\.hermes\profiles\base\state.db-wal — 3 consecutive write-backs
  each wrote a key that disagreed with the one before it …
```

Demote histogram over the same log: 57 `build_stamp_mismatch`, 24
`fingerprint_mismatch`, 8 `home_mismatch`. Serve outcomes: 50 `core_source=cache`,
41 `core_source=cache stale=true`, 89 `core_source=rebuilt`.

## Reading the evidence

The module's own census rule (`core_cache.py:181`) is the interpretation
authority: `diff_scope=every_pass` means the inputs oscillate — self-perturbation,
the defect worth acting on. `diff_scope=last_pair` means the store is simply
moving and the receipt is true without naming a defect.

Five of the ten firings are `every_pass` (08-20 18:21, 08-20 20:37, 08-21 17:18,
08-21 17:22, 08-22 13:42). Their named paths split two ways:

- **Runtime-authored** — `state.db`, `state.db-wal`, `persona_instances/*.json`.
  The runtime writes these while a build stats them. This is the actionable
  class.
- **`realm_sync/<realm>/.git/**`** — a git worktree the runtime syncs into,
  under the store root. 60 changed entries in one pass. Git's own index, reflog
  and packfiles are not runtime state the projection reads; they are inside the
  fingerprint walk because the walk is directory-level over the store root.

`state.db-wal` is the subtlest: `core_cache.py:856` already masks a frameless
WAL to `(path, 0, 0)` so that *reading* the database does not look like writing
it. The 08-21 17:22 and 08-22 13:42 firings say the mask is not sufficient for
the way this runtime commits during a build.

## The sanctioned direction

The module docstring states it and this plan must not deviate:

> If a shadow receipt ever shows divergence, the fix is WIDENING the stat set —
> never trusting the cache harder.

Widening means making the closure a function of the store rather than of the
instant, over the *named* inputs. Two candidate moves, neither yet ruled:

1. **Exclude `realm_sync/*/.git/`** from the walk. The projection reads
   `realm_sync_state/<realm>.json`, not the worktree's git internals — so the
   `.git` subtree is arguably outside the input closure entirely, which makes
   this an exclusion rather than a widening. Verify against every reader before
   ruling: an excluded input that *is* read is exactly the failure class
   (unlabeled stale served as authoritative) the lane exists to end.
2. **Extend the SQLite mask** so a WAL that gains and loses frames within a
   build keys identically. `_wal_without_frames_is_content_free`
   (`core_cache.py:856`) is the existing precedent for a content-free WAL state.

## The gate to open this

- Every candidate exclusion has a named reader audit proving nothing in
  `build_snapshot()` reads it.
- The equivalence golden stays green —
  `test_the_cache_served_core_equals_the_rebuilt_core_field_for_field`
  (`tests/agent_runtime/test_core_fingerprint_cache.py:1501`, filed under
  "7. The equivalence golden — THE authority guard").
- A shadow-validation window over the live store shows zero divergence.
- `never_converged` with `diff_scope=every_pass` drops to zero over a week of
  operator boots, measured by `scripts/core_cache_demote_census.py`.

## Why it matters

Cold boot currently costs 11,235 ms of re-projection
(`snapshot_build_core role=led caller=prewarm generation=1 build_ms=11235`,
2026-08-22 15:46). The cache exists to replace that reconstruction with
validation. While it never converges, every boot pays the full build *and* a
write-back.
