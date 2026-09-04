# w12/l3 — remote method carriage, the conflict-list producer, and a toolset manifest

Wave 12, lane `l3`, hermes. Three LARGE rows from the launcher's
`mission-control-queue.md`, claimed 2026-09-04. Base: hermes main `dcba382f0a`.

This document is written BEFORE any code, per the wave brief's large-row rule. Each row gets
numbered stages; each stage names its files, its red-first falsification, the command that proves
it, and what it must NOT change.

Rows, short names used below:

| short | row | repos |
|---|---|---|
| R11 | a remote-aimed cockpit refuses every office/agent method lane, and no gateway carriage exists to replace them | launcher (+ hermes half to prove) |
| R132 | `office.actor.conflict_resolved` needs a producer for the CONFLICT LIST before it can ever be covered | hermes + launcher |
| R135 | make toolset NAMES answerable without importing the 38 modules that register them | hermes |

---

## R11 — the remote method carriage

### What was measured first

The row says "no gateway carriage exists to replace them". Re-derived at base, that sentence is
true of the LAUNCHER and false of hermes. Every method the cockpit refuses when the aim is remote
is already registered, already tiered, and already answerable to a paired console device:

- `agent_runtime/serve_rpc.py` registers `runtime.office.upsert`, `runtime.office.remove`,
  `runtime.office.surface.update`, `runtime.office.resolve_conflict`, `runtime.agent.create`,
  `runtime.agent.retire` at `tier=TIER_CONSOLE`, and `runtime.persona.prewarm` at `TIER_READ`.
  All seven ride `manifest()`'s `methods` + `tiers` blocks in `ready` / `hello_ok`.
- `agent_runtime/call_authorization.py`'s `LOCAL_CONSOLE_METHODS` — the one arm that can turn a
  strong-enough credential into a refusal — holds five names, and NONE of the seven is among them.
  A `console`-tier device is authorized for all seven.

So the hermes half of this row is not work; it is a fact that nothing currently pins as a
CONTRACT. The launcher carriage, when it is written, will depend on exactly that fact, and today
one line added to `LOCAL_CONSOLE_METHODS` would silently retract it with every hermes test still
green.

### Stage R11.1 — pin the seven as remote-answerable

- **Files**: new `tests/agent_runtime/test_remote_cockpit_method_carriage.py`.
- **What it asserts**, for each of the seven names, as one table in one place:
  1. the name is in `serve_rpc.method_names()` (the carriage exists),
  2. `serve_rpc.method_tiers()` declares it (the tier is answerable before the call),
  3. the name is NOT in `call_authorization.LOCAL_CONSOLE_METHODS` (no kind restriction), and
  4. `authorize_call(<its declared tier>, caller=<a console-tier gateway device>, method=<name>)`
     returns `ok`.
- **Red-first**: the falsification is mechanical — add any one of the seven to
  `LOCAL_CONSOLE_METHODS` in the worktree, run the file, watch arms 3 and 4 go red for that name,
  revert. Recorded in the field note with both exit codes.
- **Must NOT change**: no production file. This stage adds a test and nothing else. In particular
  it does not widen `LOCAL_CONSOLE_METHODS`, does not add a method, and does not touch tiers.
- **Check**: `scripts/run_tests.sh tests/agent_runtime/test_remote_cockpit_method_carriage.py`,
  plus the two files whose constants it reads
  (`tests/agent_runtime/test_serve_rpc_device_scopes.py`,
  `tests/agent_runtime/test_scope_use_methods.py`) to prove nothing was disturbed.

### The launcher half (verbatim, for the operator — this lane cannot edit that repo)

Described in the final message, not built here.

---

## R132 — a producer for the conflict list

### The defect, restated from the code

`OfficeStore.resolve_conflict` (`agent_runtime/office_store.py`) ends every arm with
`_archive_conflict_sidecar(wsid, actor_key)` and then emits
`office.actor.conflict_resolved`. The office projection reads those sidecars into the office row's
`conflict_actor_keys` (`snapshot.office_summary_row` off `OfficeStore.scan_conflicts`). No patch
entity carries that field: `office_actor` addresses the actor row, and `office_surface` carries
exactly `OFFICE_SURFACE_PATCH_FIELDS` (`folders`, `revision`, `updated_at`) and is documented as
refusing to move the two key ledgers. So the event stays on the must-stay-absent list in
`agent_runtime/patch_coverage.py`, and every resolve demotes its whole batch to a full core.

The row names the two closures. This plan takes the FIRST — a new patch entity for the conflict
list — for the reason WS1's `scope` entry already argues in `TOKEN_GATED_DOMAIN_EVENT_TYPES`: a
brand-new entity with exactly one op makes "can you fold the paired row" and "may this event
free-ride" the same question, so the entity name is its own capability token and no fourth
capability string is minted. Widening the office row instead would need a new token AND would put
a second writer on fields `_officeSurfaceFields` exists to freeze.

**Landing safety without the launcher half.** The token gate is what makes this a one-repo-at-a-
time change: a client that does not declare `office_conflict` is sent no such row and is not
allowed to promote the event, so its wire is byte-for-byte today's. The launcher fold and the
declaration land second.

### Stage R132.1 — the entity and its emitter

- **Files**: `agent_runtime/state_patches.py`.
- **Adds**: `OFFICE_CONFLICT_ENTITY = "office_conflict"` and
  `emit_office_conflict_resolved_patch(event_log, *, workspace_id, actor_key, correlation_id,
  config)` emitting `op=remove` with id `"{workspace_id}/{actor_key}"` — the same
  `workspace/key` id shape `office_actor` already uses, so `_office_scope_for_patch` can route it
  with the partition it already performs. `_office_scope_for_patch` gains the new entity beside
  `OFFICE_ACTOR_ENTITY`.
- **Why `remove` and not `upsert`**: the fact moved is a DEPARTURE from a list. A remove is the op
  whose fold semantics ("drop this id from the ledger") are already the launcher's, and it carries
  no payload that could go stale.
- **Red-first**: extend `tests/agent_runtime/test_office_state_patches.py` with a case asserting the
  emitter writes one `state.patched` whose entity/id/op are the three above, and a case asserting
  `_office_scope_for_patch` returns the workspace for it (red before the routing line: an unrouted
  patch is a patch the office subscribe lane silently drops).
- **Must NOT change**: `OFFICE_SURFACE_PATCH_FIELDS`, the `office_actor` emitters, and
  `HISTORICAL_FOLD_ENTITIES` — that last is the "what a client that declares NOTHING folds" set and
  widening it is the one edit that would break every fielded launcher.
- **Check**: `scripts/run_tests.sh tests/agent_runtime/test_office_state_patches.py`.

### Stage R132.2 — the producer, and the coverage entry

- **Files**: `agent_runtime/office_store.py`, `agent_runtime/patch_coverage.py`.
- **`office_store.resolve_conflict`**: call the new emitter inside the held `office_lock`,
  immediately after `_archive_conflict_sidecar` and before `self._emit(...)` — the ordering every
  other emitter in the class already uses, argued on `_emit_actor_patch`. It fires on ALL THREE
  arms, including `take="local"`, because all three archive the sidecar; that is precisely the
  second refusal the current comment block records, and it is what this row closes.
- **`patch_coverage`**: `office.actor.conflict_resolved` moves out of the must-stay-absent prose
  into `LIVE_COVERED_DOMAIN_EVENT_TYPES`, with `TOKEN_GATED_DOMAIN_EVENT_TYPES` mapping it to
  `OFFICE_CONFLICT_ENTITY`. The comment block — which has now carried two retired reasons and is
  about to retire its third — is rewritten to record the producer that discharged it, keeping the
  dead reasons beside the live one as the file's own convention requires. `office.actor.restored`
  and `office.surface.created` stay absent and their paragraph is untouched.
- **Red-first**: extend `tests/agent_runtime/test_office_state_patches.py` /
  `tests/agent_runtime/test_serve_rpc_office_resolve.py` with a case that resolves a conflict and
  asserts the batch carries an `office_conflict` `remove` for the resolved key on all three arms;
  red before the producer line. A second case asserts a client declaring no `office_conflict`
  still gets an UNPROMOTED batch (the token gate) — red if the coverage entry is added un-gated.
- **Must NOT change**: the `take="local"` / edit-vs-remove ACTOR behaviour, the domain event's own
  payload, `OFFICE_SURFACE_FOLD_CAPABILITY`, and the two siblings still on the absent list.
- **Check**: `scripts/run_tests.sh` on `tests/agent_runtime/test_office_state_patches.py`,
  `tests/agent_runtime/test_office_store.py`, `tests/agent_runtime/test_serve_rpc_office_resolve.py`,
  `tests/agent_runtime/test_patch_coverage.py`.

### Stage R132.3 — the canon

- **Files**: `docs/agent-runtime-harness/06-office-and-board.md` § the fold model.
- Records the third entity, why the resolve is now coverable, and that the promotion is gated on
  the client declaring `office_conflict`.
- **Must NOT change**: any coverage claim naming a test — this doc adds none.
- **Check**: `scripts/doc_cite_adjacency.py --exclude archive --exclude planned` and
  `scripts/run_tests.sh tests/test_coverage_claims_resolve.py` (already red on main by five claims
  in the S2 directory-push plans — this stage must not add a sixth).

### The launcher half

Described verbatim in the final message: the `office_conflict` fold, the declaration, and the
`_officeSurfaceFields` doc comment that names `conflict_actor_keys` as unmovable.

---

## R135 — toolset names without the 38 imports

### What was measured first

At base, in the canonical test env, `tools.registry.discover_builtin_tools()` costs **3161 ms**
with the AST verdict cache warm; `import tools.registry` alone costs 110 ms. The scan finds **38**
modules with a top-level `registry.register(...)` and imports them. The chain that pays it on a
create is `perform_agent_create` → snapshot / `persona_assignments` → `resolve_tool_visibility` →
`tool_visibility._ensure_tool_registry_populated()` → `import model_tools`, whose module scope runs
`discover_builtin_tools()`. Importing `agent_create` itself does NOT pay it — the cost is on the
call path, not the import path.

A static read of the same 38 modules finds **90** top-level `registry.register(...)` calls, of
which **79** carry a literal `name=` and a literal `toolset=` and **11** (in `flux3_video_tool.py`
and `yuanbao_tools.py`) name a module-level `_TOOLSET` string constant. Folding module-level string
assignments makes the extraction complete at 90/90 across 29 toolsets. So the names ARE statically
answerable; nothing needs to be guessed.

### Stage R135.1 — the static extractor

- **Files**: `tools/registry.py`, new `tests/tools/test_registry_static_scan.py`.
- **Adds**: `scan_registered_tools(tools_dir=None) -> dict[str, list[tuple[str, str]]]` — module
  stem → `[(tool_name, toolset)]`, derived from the SAME AST walk
  `_module_registers_tools` already performs, extended to read the `register` call's `name`/
  `toolset` (keyword or first two positional) and to fold module-level `NAME = "literal"`
  assignments. A call whose name or toolset cannot be resolved to a string is reported in a
  separate `unresolved` channel rather than dropped, so "the scan could not see it" is never
  indistinguishable from "there is nothing there".
- **Red-first**: the test builds a temp `tools/`-shaped directory containing a literal
  registration, a `_TOOLSET`-constant registration, a registration inside a function body (must be
  ignored), and one with a computed name (must land in `unresolved`), and asserts the four
  outcomes. Red before the extractor exists.
- **Must NOT change**: `discover_builtin_tools`' behaviour, the on-disk verdict cache format, or
  `_module_registers_tools`' verdict for any file.
- **Check**: `scripts/run_tests.sh tests/tools/test_registry_static_scan.py` and
  `scripts/run_tests.sh tests/tools/test_registry.py`.

### Stage R135.2 — the generated in-tree artifact and its gate

- **Files**: new `tools/toolset_manifest.json`, new `scripts/dump_toolset_manifest.py`, new
  `tests/tools/test_toolset_manifest.py`.
- **The artifact** is in-tree and committed — deliberately NOT under the home cache, which is the
  row's own reason the existing memo cannot help: `scripts/run_tests.sh` points the home at a fresh
  temp directory per file, so a home-cached manifest is cold in the suite by construction.
- **The script** mirrors `scripts/dump_cli_contract.py`'s shape exactly: `--check` (exit non-zero
  with a diff when the tree has moved) and `--write`.
- **The gate**, two arms:
  1. the committed artifact equals a fresh `scan_registered_tools` — the ratchet that makes a new
     tool file a visible manifest diff rather than a silent divergence, and
  2. the committed artifact equals the LIVE registry after `discover_builtin_tools()` — the arm
     that proves the static read agrees with what the imports actually register. This arm pays the
     3.1 s once, in one test, which is the whole point of paying it nowhere else.
- **Red-first**: hand-edit one toolset value in the artifact, watch both arms go red, revert.
- **Must NOT change**: no consumer is repointed in this stage. Nothing reads the artifact yet.
- **Check**: `scripts/run_tests.sh tests/tools/test_toolset_manifest.py`;
  `python scripts/dump_toolset_manifest.py --check`.

### Stage R135.3 — the reader

- **Files**: new `tools/toolset_manifest.py`, extending `tests/tools/test_toolset_manifest.py`.
- **Adds** a pure reader — `builtin_toolset_for_tool(name)`, `builtin_tool_names()`,
  `builtin_tool_names_for_toolsets(toolsets)` — loading the JSON once behind an `lru_cache`,
  importing nothing under `tools/` and nothing from `model_tools`.
- **Red-first**: a test that answers a toolset name in a subprocess and asserts
  `"model_tools" not in sys.modules` and that no `tools.<stem>` registrar module was imported. That
  assertion IS the row's title, and it is red until this file exists.
- **Must NOT change**: `tool_visibility`, `model_tools`, or any live answer. The reader ships
  unwired.
- **Check**: `scripts/run_tests.sh tests/tools/test_toolset_manifest.py`.

### Stage R135.4 — repointing `tool_visibility`. **GATED ON AN OPERATOR RULING; NOT BUILT HERE.**

Switching `tool_visibility._cached_tool_names_for_toolsets` / `get_toolset_for_tool` from
`_ensure_tool_registry_populated()` to the manifest is where the 3.1 s is actually saved, and it
changes two answers. Both are honest questions with no house precedent, so this lane stops here and
states them rather than choosing:

1. **Plugin tools.** `hermes_cli/plugins.py` registers plugin tools into the SAME `tools.registry`
   singleton, and today they are present in `get_all_tool_names()` only because `model_tools`'
   module scope runs `discover_plugins()` right after `discover_builtin_tools()`. A manifest is
   builtin-only. The union `manifest ∪ (registry after an explicit, idempotent
   discover_plugins())` restores completeness while skipping the 38 imports — but it moves plugin
   discovery from "a side effect of importing model_tools" to "a thing this reader does", which is
   a lifecycle decision, not a refactor.
2. **A registrar module whose import FAILS.** Measured at base in this checkout, 11 of the 38 fail
   to import under the live home (missing optional dependencies — `chardet` was one) and are logged
   at `warning` and skipped, so their tools are absent from the registry today. The manifest names
   them, because they are in the tree. After the switch a persona would be told it has a tool whose
   handler cannot be looked up. That is arguably MORE honest for a name question and arguably a
   regression for a capability question, and which one it is decides whether the reader must
   intersect against an import-health probe.

Until that is ruled, stages 1–3 stand on their own: the artifact and its gate are exactly what the
row asked for ("needs a generated in-tree artifact plus a gate"), the names ARE answerable without
the imports, and the `--timeout=180` question the row names as downstream is unblocked either way.

---

## Verification, for every stage

Focused pytest on the touched modules only, via `scripts/run_tests.sh <file>`; never the full
suite. After any doc or harness change: `scripts/doc_cite_adjacency.py --exclude archive --exclude
planned` and `scripts/dump_cli_contract.py --check`. A red that predates this lane's first commit
is recorded and left.

## What none of this may do

- Add a row to any baseline, allowlist or ledger to make a gate pass.
- Widen `HISTORICAL_FOLD_ENTITIES` or `OFFICE_SURFACE_PATCH_FIELDS`.
- Add a name to `LOCAL_CONSOLE_METHODS` or remove one.
- Add a coverage claim naming a test that does not exist.
- Commit an absolute machine path in a doc or a comment.
