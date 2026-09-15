# w12/l3 — field notes, 2026-09-04

Lane `w12/l3`, hermes, three LARGE rows. Base main `dcba382f0a`. Plan:
[w12-l3-remote-carriage-conflict-list-and-toolset-manifest.md](w12-l3-remote-carriage-conflict-list-and-toolset-manifest.md),
committed before any code as `d9966fb248`.

Everything below was run in this lane's worktree against the canonical test env
(`AGENTS.md` §Testing; `scripts/run_tests.sh` finds it). No full-suite run; never pushed.

---

## Row 11 — a remote-aimed cockpit refuses every office/agent method lane

**What the row asked.** Office writes, agent create/retire and prewarm are typed-refused for a
paired install; the carriage that would replace the refusal is R10/§8-future.

**What I measured, before touching anything.** The row's sentence "no gateway carriage exists" is
true of the LAUNCHER and false of hermes. At base:

- `agent_runtime/serve_rpc.py` registers all seven names the launcher refuses —
  `runtime.office.upsert`, `.remove`, `.surface.update`, `.resolve_conflict`,
  `runtime.agent.create`, `runtime.agent.retire` (all `tier=TIER_CONSOLE`) and
  `runtime.persona.prewarm` (`TIER_READ`) — and all seven ride `manifest()`'s `methods`/`tiers`
  blocks in `ready` / `hello_ok`.
- `agent_runtime/call_authorization.py`'s `LOCAL_CONSOLE_METHODS`, the only arm that can refuse a
  credential that is otherwise strong enough, holds five names and NONE of the seven.
  `test_serve_rpc_device_scopes.py` already proves a `console`-tier device may run a console verb.

So the hermes carriage landed with S2/S2d and the remaining work is entirely launcher-side: the
aim resolves to a refusal instead of binding the remote connector's `call`. `LanSocketConnector`
already implements `HermesConnector.call`, so the transport is there too.

**What changed.** Nothing in production. The fact the launcher carriage will rest on was pinned
nowhere, and one line added to `LOCAL_CONSOLE_METHODS` would retract it with every hermes test
still green. `tests/agent_runtime/test_remote_cockpit_method_carriage.py` now holds it as a table
with four arms per name: registered, tier declared, not kind-restricted, and `authorize_call`
answers `ok` for a console-tier gateway device.

**Red-first proof.** Added `"runtime.agent.create"` to `LOCAL_CONSOLE_METHODS` in the worktree:
27 passed / 2 failed, exactly arms 3 and 4 for that name
(`test_no_lane_is_kind_restricted_to_this_installs_own_console[runtime.agent.create]`,
`test_a_console_tier_paired_device_is_authorized_for_every_lane[runtime.agent.create-console]`).
Reverted; 29 passed.

**Commands.**

| command | exit |
|---|---|
| `scripts/run_tests.sh tests/agent_runtime/test_remote_cockpit_method_carriage.py` | 0 — 29 passed |
| `scripts/run_tests.sh tests/agent_runtime/test_serve_rpc_device_scopes.py tests/agent_runtime/test_scope_use_methods.py` | 0 — 58 passed |

**Commit.** `bc3710bfc8`.

**What is left.** The launcher half, described verbatim in the lane's hand-back. It is real work
and it is not hermes work.

---

## Row 132 — `office.actor.conflict_resolved` needs a producer for the conflict list

**What the row asked.** The event's fold state is the office row's `conflict_actor_keys`, moved by
the sidecar archive on every arm and carried by no patch. Closing it needs a new patch entity or a
widened office row, plus the matching launcher fold.

**What I measured.** The premise held exactly as written. `patch_coverage.py` still listed the
event as must-stay-absent with the comment block that had carried two retired reasons; no conflict
patch entity existed; `resolve_conflict` ended every arm with `_archive_conflict_sidecar` and the
domain event.

**What changed — the new entity.** `OFFICE_CONFLICT_ENTITY = "office_conflict"` with one op
(`remove`), id `office_actor_patch_id`'s `"<workspace>/<actor_key>"`, emitted by
`state_patches.emit_office_conflict_resolved_patch`. `office_patch_scope` routes it by sharing the
actor arm's split — an office patch that function cannot place is one the subscribe lane silently
DROPS, which is the WV-H3 failure its own docstring records.

Chose the new entity over the widened office row because widening would put a second writer on the
row `OFFICE_SURFACE_PATCH_FIELDS` exists to keep to one, and the launcher's `_officeSurfaceFields`
comment names `conflict_actor_keys` among the keys no patch may move. No new capability string:
WS1's `scope` argument applies verbatim — one entity with one op makes "can you fold the row" and
"may the event free-ride" the same question, so the entity name is its own token in
`TOKEN_GATED_DOMAIN_EVENT_TYPES`.

**What changed — the producer.** `OfficeStore._emit_conflict_resolved_patch`, called from inside
`office_lock` after the sidecar archive and before the domain event, on ALL THREE arms. Pairing it
with the ARCHIVE rather than with the actor write is the load-bearing choice: `take="local"` writes
no actor at all, and a producer hung off the write would have fired on two arms of three and left
that arm's conflict pill lit forever — the failure the must-stay-absent note named, arriving
through the fix meant to close it.

**Landing safety.** The token gate is why the hermes half could land alone: a client that has not
declared `office_conflict` is sent no such row and may not promote the event, so its wire is
byte-for-byte what it was. Asserted from both sides rather than argued.

**Red-first proof.**

- Stage 1: restored `agent_runtime/state_patches.py` to its pre-lane content — 41 passed, 4 failed
  (the emitter's shape, the flag-off silence, the scope routing, the entity gate). Restored: 45
  passed.
- Stage 2: restored `agent_runtime/office_store.py` and `agent_runtime/patch_coverage.py` — 47
  passed, 5 failed, including all three arms of
  `test_every_resolve_arm_emits_the_conflict_ledger_row` and the token-gate test. Restored: 52
  passed.

**Existing tests whose SPEC moved, and why each is a correction rather than a relaxation.**

- `test_restore_stays_uncovered_and_remove_no_longer_does` — third split. `.conflict_resolved`
  leaves the must-stay-absent assertion because its fact now has a producer; `.restored` and
  `office.surface.created` stay, and that is what the test still holds.
- `test_a_conflict_resolution_archive_still_emits_the_remove_even_with_emit_false` — the batch now
  carries TWO removes and the test asserts both.
- `test_take_local_writes_no_actor_and_therefore_emits_no_patch` → renamed
  `…_but_still_moves_the_conflict_ledger`. The old name became false; renaming beats relaxing.
- `test_the_batch_a_resolve_lands_in_still_demotes_for_every_client` → renamed
  `test_a_resolve_batch_demotes_for_todays_client_and_promotes_for_a_declaring_one`. The
  assertion SPLIT rather than inverting: the widest yesterday-client still demotes byte-for-byte,
  and only a client declaring the new entity promotes. That split IS the compatibility claim.
- `test_resolve_conflict_dry_run_leaves_sidecar_and_is_eventless` — `+1` event becomes `+2`.

**The cross-stack golden.** `tests/fixtures/stream_frames/patch_coverage_manifest.json` is a
hand-maintained pin the launcher folds the same bytes of (`PINNED_ONLY_FILES` in
`scripts/generate_agent_runtime_stream_fixtures.py` says why it cannot be generated). Hand-edited:
the event added to `covered_domain_events` in sorted position, and a case row
`{"chokepoint":"office.actor.conflict_resolved","entity":"office_conflict","op":"remove","foldable":true}`
appended. `MANIFEST.sha256`'s line for that one file recomputed
(`d35b428f8c7cb165b1c74a6ad06bf96e95bd97b8563c31ab5ec87b02bfcad227`); no other frame regenerated.

**Commands.**

| command | exit |
|---|---|
| `scripts/run_tests.sh tests/agent_runtime/test_office_state_patches.py` | 0 — 52 passed |
| `scripts/run_tests.sh` over the 9-file patch/fold neighbourhood | 0 — 246 passed |
| `scripts/run_tests.sh` over the 9 further resolve-touching suites | 0 — 244 passed |
| `scripts/doc_cite_adjacency.py --exclude archive --exclude planned` | 0 — unwaived failures 0 |
| `scripts/dump_cli_contract.py --check` | 0 — fresh, 191 command paths |

The cite probe initially reported TWO unwaived failures, both mine: the canon cites
`state_patches.py` by line and stage 1 moved them. Repointed `emit_office_surface_patch`
`:1132`→`:1218` and `emit_office_surface_refresh` `:1199`→`:1285`. No baseline entry added.

**Commits.** `8e07a5ca5c` (entity), `626ffb81e1` (producer + coverage + golden), `2b42f00090`
(canon).

**What is left.** The launcher fold and the declaration — handed back verbatim. Until they land
every fielded client keeps taking the full core on a resolve, which is today's behaviour.

---

## Row 135 — toolset names without the 38 imports

**What I measured, at base.** `discover_builtin_tools()` = **3161 ms** with the AST verdict cache
warm; `import tools.registry` alone = 110 ms. The static read of the same tree finds **38** modules
with a top-level `registry.register(...)`, **90** register calls, of which **79** carry two string
literals and **11** (`flux3_video_tool.py`, `yuanbao_tools.py`) name a module-level `_TOOLSET`
constant. Folding module-level string assignments makes the extraction complete at **90/90** across
**31** toolsets — nothing has to be guessed.

The chain that pays it on a create is `perform_agent_create` → snapshot / `persona_assignments` →
`resolve_tool_visibility` → `tool_visibility._ensure_tool_registry_populated()` → `import
model_tools`. Importing `agent_create` itself does NOT pay it (`-X importtime` shows only
`tools.registry`, 4 ms) — the cost is on the call path, not the import path.

**What changed.**

1. `tools.registry.scan_registered_tools()` — the same AST walk `_module_registers_tools` already
   performs, extended to read the call's `name`/`toolset` (keyword or positional) and to fold
   module-level string constants. A registration it cannot resolve lands in a third `unresolved`
   channel rather than being dropped; the generator refuses to write while that list is non-empty.
2. `tools/toolset_manifest.json` — a committed, deterministic in-tree artifact (no timestamps, no
   machine identity, sorted throughout), with `scripts/dump_toolset_manifest.py --check|--write`
   shaped like `dump_cli_contract.py`.
3. `tools/toolset_manifest.py` — the reader. `builtin_toolset_for_tool`,
   `builtin_tool_names`, `builtin_tool_names_for_toolsets`, `builtin_toolset_names`,
   `builtin_modules`, behind one `lru_cache`.

**The payoff, measured.** A fresh interpreter that imports the reader and answers a toolset:
**31.9 ms**, against **3161 ms** for the import that answers it today — the row's title, as a
number.

**Red-first proof.** Restored `tools/registry.py` to its pre-lane content: the scan tests fail at
COLLECTION (`ImportError: cannot import name 'scan_registered_tools'`). Poisoned one toolset value
in the committed artifact (`read_terminal`: `terminal`→`web`): 6 passed, 4 failed — the static-scan
arm, the byte-for-byte generator arm, the LIVE-registry arm, and the reader's own answer. Restored;
10 passed.

**The two-armed gate, and why one arm is not enough.** Arm 1 compares the artifact to a fresh
static scan and runs in a second; it can only prove the artifact matches this reader. Arm 2
compares it to the LIVE registry after `discover_builtin_tools()` and pays the 3.16 s ONCE, which
is the whole point of not paying it elsewhere — a static reader that mis-parsed one call satisfies
arm 1 forever. Arm 2 asserts the manifest is a superset and names any live tool it does not carry;
it does not demand equality, because a registrar module whose import fails is logged and skipped
(see below).

**The row's title is delivered as a CAPABILITY, and the switch is NOT made.** Pointing
`agent_runtime.tool_visibility` at the reader is where the 3.16 s is actually saved on a create,
and it changes two answers. Both are stated in the plan's §R135.4 and left for an operator ruling:

1. **Plugin tools.** `hermes_cli/plugins.py:455` registers into the SAME `tools.registry`
   singleton, and they are in `get_all_tool_names()` today only because `model_tools`' module scope
   runs `discover_plugins()` right after `discover_builtin_tools()`. The union `manifest ∪ (registry
   after an explicit, idempotent `discover_plugins()`)` restores completeness while skipping the 38
   imports — but it moves plugin discovery from a side effect of an import to a thing this reader
   does, which is a lifecycle decision.
2. **A registrar module whose import FAILS.** Measured here: 11 of the 38 fail under this
   checkout's live home on missing optional dependencies (`chardet` was one). They are logged at
   `warning` and skipped, so their tools are absent from the registry today, while the manifest
   names them — they are in the tree. After a switch, a persona would be told it has a tool whose
   handler cannot be looked up. That is arguably MORE honest for a name question and arguably a
   regression for a capability question, and which one it is decides whether the reader must
   intersect against an import-health probe.

The row also names the `--timeout=180` question on the nine doctor/census claims as downstream of
this; that is unblocked by the artifact either way and was not touched.

**Commands.**

| command | exit |
|---|---|
| `scripts/run_tests.sh tests/tools/test_registry_static_scan.py tests/tools/test_registry.py` | 0 — 34 passed |
| `scripts/run_tests.sh tests/tools/test_toolset_manifest.py` | 0 — 10 passed |
| `python scripts/dump_toolset_manifest.py --check` | 0 — 90 tools, 31 toolsets, 38 modules |
| `scripts/dump_cli_contract.py --check` | 0 |

**Commits.** `cc944cff72` (scan), `a9b74fcec0` (artifact + generator), `d098accf04` (reader +
gate).

---

## Reds that are NOT this lane's

- `tests/test_toolsets.py::TestHarnessCoreToolset::test_harness_core_resolves_the_declared_43_with_the_registry`
  — `assert 44 == 43`. Proven to predate this lane: reproduced with `tools/registry.py` restored to
  base `dcba382f0a`. A tool joined `harness_core` and the declared count was not moved with it.
- `tests/test_coverage_claims_resolve.py::test_every_coverage_claim_names_a_test_that_exists` — the
  five known claims in `planned/s2-introduce-directory-push*.md` and nothing else. Re-run at the end
  of this lane: still exactly those five. Nothing here added a sixth.

## An environmental note worth one line

Under the live `HERMES_HOME`, 11 of the 38 registrar modules fail to import — most on missing
optional dependencies, and once, transiently, on a path inside ANOTHER agent's worktree read out of
shared home state. `discover_builtin_tools` logs each at `warning` and continues, so a tool can be
absent from the registry with nothing louder than a log line saying so. That is the second open
question on R135.4 and it is not hypothetical.
