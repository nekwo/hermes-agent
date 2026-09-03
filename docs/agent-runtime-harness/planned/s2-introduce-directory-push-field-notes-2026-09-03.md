# S2 / S2b / S2c — field notes (hermes side), 2026-09-03

Running record for `s2-introduce-directory-push.md` (beside this file). The
planner's survey is the first section; the builder appends below the rule,
one dated entry per commit, deviations named as deviations.

Worktree `X:/wt/s2-hermes`, branch `feat/s2-introduce-directory-push`, base
hermes `55fdc99148` (`origin/main`). Nothing outside the worktree was written;
the live store (`HERMES_HOME=X:\Eternia\.hermes`) was read with three
read-only verbs only. No push.

## Planner's survey (Fable agent, 2026-09-03)

### What was read, and the shas

* Parent plan `EterniaLauncher/docs/mission_control/planned/same-account-instant-pairing.md`
  at launcher `791cc8030` — all 17 rulings ADOPTED (R-IP15 AMENDED), §2 flows
  + parity table, §3 S2/S2b/S2c with hardening bullets, §5 ledger (S0a
  `e94e022fb6`, S0b `b4a383a1e8`, blocker `368387ae0b` LANDED), §6 audit
  table, §7 order.
* S0a plan + field notes (`planned/s0a-atlas-cleanup*.md`): `harness_core`
  has FIFTEEN members incl. `browser-cdp`; ratchet 43 / 0 / 0 / 1149 per
  persona; the inventory emitter gates three artifacts; the stream goldens
  moved in S0a and the launcher mirror was closed at launcher `791cc8030`.
* S0b hermes notes (`planned/s0b-add-install-hermes-notes-2026-09-03.md`)
  and the launcher-side S0b plan: the trust/cache frozensets and their
  partition test; the launcher reads the `gateway` block by key; Unpair
  revokes the LOCAL peer first and defers the far row to S2c.
* Backend S1 packet §4 at backend `bb00ffd4`: grant id = correlation id;
  `introduce --for-install <requester install> --for-device <from_device_id>
  --correlation <grant_id>`; the recommended `fulfil` payload keys.

### Live measurements (read-only)

```
HERMES_HOME=X:/Eternia/.hermes C:/Python312/python.exe -m hermes_cli.main harness gateway id --json
  → install bbdb8120-575d-4890-85e7-ecdbd650cde0 "DESKTOP-QJ7DDV2", state loaded,
    store root X:\Eternia\.hermes\agent-runtime; row keys {install_id, display_name, state, created_at, path}
… harness gateway peers list --json    → items: []
… harness gateway devices list --json  → items: []
```

Test runner sanity from this worktree (no `.venv` here; the runner found the
shared canonical venv `~/.venvs/hermes-test` on its own):

```
bash scripts/run_tests.sh tests/agent_runtime/test_gateway_targets.py tests/agent_runtime/test_peer_authorization.py
=== Summary: 2 files, 42 tests passed, 0 failed (100% complete) in 5.3s (8 workers) ===
```

### Findings the plan is built on (each cited in the plan's §0)

1. **The `gateway` block's key set is pinned exactly** at
   `tests/agent_runtime/test_serve_gateway_lane.py:338-344` (and `:239` for
   `disabled`). `capabilities` moves both pins — named in the plan, not
   discovered at build time.
2. **`peered` carries no expiry**, so B's `record_peer` has nothing to store;
   R-IP15's 30-day rule needs `hello_ok.peered.expires_at` (additive) or the
   two ends of one edge expire at different times.
3. **`peers join` is trust-on-first-use** (`gateway_commands.py:603-609` pins
   the payload's own fingerprint). `--expect-fingerprint` is the attested
   path; the manual S0 path keeps TOFU and says so in the ack.
4. **`introduce` has no refusal precedent for "listener off"** — both mint
   verbs print a note instead. Recommended: `runtime_unavailable` with
   `peers pair`'s sentence, because `introduce`'s consumer is a machine.
5. **A code is a bearer.** The peer half can be scoped (the join hello names
   the redeemer's install id); the device half cannot (the device id is minted
   at redeem). The plan labels the device row with `account_device_id` and
   says plainly it is a label.
6. **Cross-process event emission already works** — `realm_sync._append_realm_sync_event`
   appends from the CLI process and the serve's stream advances. So R-IP12's
   "mtime check" has one job left (hand edits / pre-S2c writers), and the
   plan sizes it to that.
7. **`decision_contract_hash` rides the stream goldens** (`delta.json`,
   `hydrate.json`: `b6985ac4…`). Registering five events regenerates every
   golden that carries it → the launcher's `test/fixtures/harness_stream/`
   byte mirror is OWED at landing. `SURVIVING_EVENT_COUNT = 59` → 64.
8. **A transcript read inside the serve process has no precedent.**
   `persona_chat_session_messages` resolves scope through
   `resolve_chat_session_scope`; the ambient rung is env-gated
   (`HERMES_ALLOW_AMBIENT_CHAT_READS`). `peer.thread.read` must fail closed
   with its own word and be proven on a real serve child by the e2e; the
   argv-lane fallback is spelled in the plan's §5.
9. **The e2e roots hold no persona** (the chat e2e's proof IS `unknown
   persona dev`). S2b's parity e2e needs a seeded instance on B — the one
   detail the plan leaves the builder to confirm against
   `agent_runtime/config.py`.
10. **The peer surface pins live in four places**: `test_peer_authorization.py:139-160`
    and `:371`, `test_peer_chat_execute.py:79`, and a stale "two verbs wide"
    comment in the chat e2e. All named.
11. **The HUD field contract** (`test_runtime_hud_field_contract.py:77`)
    asserts every emitted key is declared — `installs` needs a `HudField`
    row. The goldens' `situational_hud` blocks have no peers, so an
    `installs` block that drops when empty leaves those bytes alone.
12. **The new tool moves the S0a ratchet** (43 → 44, token estimate
    re-measured) and all three inventory artifacts; the Atlas artifact
    regenerates launcher-side (OWED).
13. **`_self_endpoints` returns at most one row** and `[]` for a wildcard
    bind; `gateway id`'s `endpoints` needs interface enumeration
    (stdlib-only recipe in R-S2-2), and reusing it for the join payload
    makes hellos carry up to four endpoints for free.

### Corrections made while writing

* Test-file names cited for the CLI verb harness were corrected to the real
  files (`tests/hermes_cli/test_gateway_pairing_verbs.py`,
  `test_gateway_peer_verbs.py`); there is no `test_decision_contract_registry.py`
  — the S32 parity test is the registry's gate.

### Sub-rulings recorded for the orchestrator (all have recommendations in §1)

R-S2-1 capabilities vocabulary/placement · R-S2-2 `gateway id` endpoints ·
R-S2-3 `introduce` flags + envelope (`grant_payload` = the backend shape) ·
R-S2-4 requester scoping (peer half checked, device half labelled) · R-S2-5
`expires_at` on both rows, TRUST-classified, verifiers refuse after the proof,
wire stays collapsed · R-S2-6 `join --expect-fingerprint` · R-S2-7
`peers_cache.json` shape + writers; `last_seen` leaves the trust file ·
R-S2-8 mtime check = revision memo, external writes only · R-S2-9
`peer.announce` schema, caller-only, one-way `revoked_you` · R-S2-10 roster
projection + scope · R-S2-11 `peer.thread.read` needs `target`, same lane
guard, five refusal codes · R-S2-12 `@install/` on open/threads,
`call_peer_method`, timeouts · R-S2-13 HUD lines + residency note · R-S2-14
correlation stamping (pending entries + events, never a row) · R-S2-15
`revoked_you` first · R-S2-16 `usable_peers` · R-S2-17 ratchet/manual cost ·
R-S2-18 the "introduce unreachable" test.

---

## Builder's record (append below; one entry per commit)

_(empty — the builder fills this)_


---

## Builder's record (Opus, 2026-09-03)

Worktree `X:/wt/s2-hermes`, branch `feat/s2-introduce-directory-push`, rebased
onto `origin/main` at `71be9a183f` before the first commit. Nothing outside the
worktree was written; the live store was not touched at all this session (the
planner's three read-only measurements stand). No push.

**Build order was S2 -> S2c -> S2b -> S2d**, not the plan's S2 -> S2b -> S2c.
The orchestrator's dispatch set that order and it turned out to be the better
one: S2c lands `usable_peers`, so S2b's `agent_chat_installs` and the resolver
read the final predicate from the start, and the plan's "land the tool on
`list_peers` minus revoked, then swap the predicate in — one commit" (R-S2-16)
never had to happen. One consequence, recorded as a deviation below: the
allowlist grew in two steps rather than one.

### The one derived detail the plan left me (§3, "the seeding detail")

**How the S2b parity e2e seeds a persona on root B**, confirmed against
`agent_runtime/config.py`:

* `load_agent_runtime_config` (`config.py:112-172`) reads the YAML, takes
  `top["agent_runtime"]` as `raw`, and `raw.get("personas", {})` into
  `AgentRuntimeConfig.personas` (`:60`, `:169`).
* `persona_records_from_config` (`:532-541`) walks `cfg.personas.items()` —
  each KEY is the persona id, each VALUE a dict of overrides — and builds an
  `AgentPersona` through `_persona_from_overrides` (`:643-656`), where `role`
  defaults to `PROFILE_ROLE_SENTINEL` and `model`/`provider`/`api_mode` fall
  back to the config defaults.
* `ensure_persisted_personas` (`:631-640`) merges that catalog under the
  `AgentStore` rows, and `PersonaInstanceStore.ensure_for_personas` turns each
  into a canonical instance — which is what `addressable_roster` then projects.

So the fixture writes into B's sandbox `HERMES_HOME/config.yaml`:

```yaml
remote_gateway:
  listen: "127.0.0.1"
  port: 0
agent_runtime:
  personas:
    dev:
      display_name: Dev
      role: dev
```

beside the `remote_gateway` block `_sandbox_env` already writes
(`test_gateway_peer_two_roots_e2e.py:82-88`). No `AgentStore` write and no CLI
call is needed: the config catalog IS a persona for every reader in the chain
above. **Note for whoever runs the e2e**: the persona still needs a model/provider
the sandbox can resolve for a TURN to complete, but `peer.roster.list` and
`agent_chat_installs` need only that the instance EXISTS and resolves through
`_resolve_mission_chat_persona_id`, which the config catalog gives them.

### Per-step record

**S2 — `d379982759`.** `gateway_capabilities.py` (new, imports nothing so the
serve and a cold CLI read one tuple); `expires_at` on both credential rows,
classified TRUST, with both verifiers refusing AFTER the proof and the wire
still collapsed; requester scoping (peer half checked, device half labelled
`account_device_id`); `harness gateway introduce`; `peers join
--expect-fingerprint` / `--correlation`; `gateway id` gaining `endpoints` /
`endpoints_source` / `listener` / `capabilities`; `store_file_io.stamp_passed`
as the reader half of `iso_stamp`. Named pins moved: `test_serve_gateway_lane.py`
`:239` and `:338`. `tests/fixtures/hermes_cli_contract.json` regenerated
(sha256 `8cd52731…` at S2, `86837537…` after S2c's `--no-announce`).

**S2c — `36a9be9b32`.** `peers_cache.json` with its own row shape, contract and
partition test; `note_peer_seen` MOVED to it and `last_seen` dropped from `_row`;
five `gateway.peer.*` contracts emitted from the writing process; the revision
memo; `peer.announce` (allowlist + handler + `apply_peer_announce`);
`gateway_announce.py` with the revoke ordering; `usable_peers` and
`resolve_install_ref`; `peers list` gaining `cache` / `usable` /
`unusable_reason` / `ref`; hello refresh; `dial_peer` cache-endpoints-first with
the trust pin always. Named pins moved: `test_gateway_peers_store.py` (the
partition is derived, so it re-derived itself; `note_peer_seen`'s test moved
deliberately), `test_serve_gateway_peer_lane.py:288`,
`test_s15_event_contract_pruning.py` 59 -> 64, the allowlist literals, and every
stream golden + `MANIFEST.sha256` (`decision_contract_hash` moved).

**S2b — `485f33a7f6`.** `peer_directory.py` (roster projection, far-target scope,
the shared `read_chat_lane_tail`, the HUD block); `peer.roster.list` /
`peer.thread.read`; `tools/agent_chat_remote.py`'s callers; `agent_chat_installs`
(44th tool); `@install/` on `agent_chat_open` and `agent_chat_threads`; the
`clarify_token_not_portable` refusal; the HUD `installs` field and the `also_on`
residency note. Named pins moved: the allowlist literals again (four -> six),
`test_harness_core_ratchet.py` (43 -> 44, 1149 -> 1177 — both RE-MEASURED, see
below), `test_harness_tool_inventory.py`'s count, and the three inventory
artifacts.

**S2d — the launcher's push door.** Scope addendum from the orchestrator
mid-build, sourced from the Stage 3 launcher plan §0.6 + S3-R13. See its own
section below.

### The measurement behind the ratchet move (R-S2-17)

Re-measured, not adjusted:

```
resolve_tool_visibility(neko_supervisor, permission_mode=unbounded)
  -> final_tool_count 44, model_tool_tokens 1177
```

`DECLARED_TOOL_COUNT = 44`, `DECLARED_TOKEN_ESTIMATE = 1177` (was 43 / 1149).
`agent_chat_installs`' schema description is deliberately short for that reason.
Three inventory artifacts regenerated (`--check` green, 44 tools / 15 toolsets,
sha256 `36780ed3…`), and two Operate rows written by hand before the last regen
so the generated block carries them.

### Deviations from the plan, each with its reason

1. **No `agent_runtime/correlation.py`.** R-S2-14 allowed one "if the fence is
   not already importable without pulling `serve_rpc`". It is:
   `state_patches.normalize_correlation_id` / `CORRELATION_ID_MAX_LEN` have no
   heavy imports, and `serve_rpc._correlation_id_param` itself defers to them.
   Both CLI sites import from there. A new module would have been a third name
   for one rule.
2. **`tools/agent_chat_remote.py` landed with S2c, not S2b.** The outbound
   announce is its first caller, and S2c ran first. Its tool callers arrived in
   S2b as planned.
3. **The allowlist grew in two steps.** `peer.announce` in S2c;
   `peer.roster.list` / `peer.thread.read` in S2b, the commit that REGISTERS
   them. Adding all three in S2c would have reddened
   `test_the_peer_prefix_is_the_declaration_that_it_touches_no_level` (every
   registered `peer.*` name == the allowlist) on two names nothing served —
   which is that test working.
4. **The refusal WORD leads the message on the two `join` refusals.**
   `emit_harness_error` carries only `error_class` in `safe_details` (it does not
   accept extra detail), so the message is the one channel this lane has for a
   machine-readable reason. `tls_fingerprint_mismatch: …` and
   `tls_fingerprint_invalid: …` lead their sentences. R-IP17 asks for one
   enumerated set of reason codes; this is how they are readable.
5. **`PeerRecord.last_seen` was KEPT as a legacy read** rather than deleted.
   R-S2-7 says `_decode_peer` "tolerates and ignores" a legacy `last_seen`;
   ignoring it would have deleted a fact an operator can already see on a
   pre-S2c store. `_row` no longer writes it, `PEER_ROW_CACHE_FIELDS` no longer
   names it, and `peers list` overwrites the top-level key from the cache when
   there is one — so the ack stays additive and the live answer comes from the
   file that owns it.
6. **`revoked_you`'s exit is `_clear_revoked_you`, called from the two trust
   writers.** R-S2-9 says the flag is cleared "only by a trust write"; a one-way
   flag with NO exit would mean a re-pair produced an edge every reader still
   treated as dead. The exit is a property of the call graph (two callers, both
   credential writers), asserted in `test_peer_announce.py` through the ceremony
   rather than through the helper.
7. **`runtime.gateway.peers.list` exists beside `subscribe`** (S2d). Not in
   S3-R13's three surfaces. A caller with no push channel is refused by
   `subscribe` — otherwise the registry fills with sinks nothing will write to —
   and such a caller then needs a door. One method, same body, no registration.

### Two bugs found while building, both worth naming

1. **`_cache_row(**merged)` collided with its own positional `peer_install_id`.**
   Every stored row carries that key, so the FIRST write to a fresh row
   succeeded (nothing to merge) and every write after it raised `TypeError`
   into `_touch_cache`'s best-effort `except` and silently did nothing. Found by
   the `last_seen`-after-a-handshake test going red for a reason that made no
   sense, then traced by wrapping `_touch_cache`. `_cache_row` takes a DICT now,
   and the id stays positional because it is the one field a merge must not be
   able to change.
2. **`store_file_io.store_lock` falls through WITHOUT the lock rather than
   raising** (its own docstring says so, and argues correctly that the cost is a
   lost update rather than a corrupt store). That trade is right for the
   credential stores, whose writers are ceremonies an operator runs one at a
   time. It is wrong for the CACHE, whose writers are a handshake on the
   listener thread, a dial from a tool and an announce fan-out on a background
   thread — all inside ONE serve process, all merging into one row. Added
   `_CACHE_WRITE_LOCK` (a module-level `threading.Lock`) on top of the file
   lock, held only across the read-modify-write.

### S2d — the Stage 3 lane's dependency (launcher S3-R13)

Added to this lane by the orchestrator mid-build. The decisive measurement is
the launcher plan's §0.3/§0.6: **the launcher's hermes stream carries no
events.** Its hydrate core is `agents, boards, offices, persona_instances,
running_work, …`, its fold entities are `persona_instance, incident, office_*,
scope`, and hermes reads `event_log.tail(20)` only for parity warnings. So S2c's
five `gateway.peer.*` contracts reach a stream consumer, a snapshot and an
operator, and reach a launcher NEVER. Canon 03 invariant 6 routes new
server->client push over JSON-RPC notifications; S2d is that.

Shipped:

* `agent_runtime/serve_gateway_peers_rpc.py` — `peer_directory_row` /
  `peer_directory_rows` (the CLI verb's row shape, so the push lane and the
  greet lane agree), `PeerDirectorySubscriptions` (a sink registry, released
  from the same disconnect path the office lane's is), and `publish_peer_event`.
* `runtime.gateway.peers.subscribe` (TIER_READ) — directory + registration in
  one call, for `runtime.office.subscribe`'s reason. Refuses a caller with no
  push channel and names `…list` in the refusal.
* `runtime.gateway.peers.list` (TIER_READ) — the same body, no subscription.
* `runtime.gateway.peers.roster` (TIER_CONSOLE) — the fetch-through. A launcher
  cannot call `peer.roster.list` (a PEER method; it holds a DEVICE credential),
  so it asks its own hermes, which IS that install's peer. Caches before
  replying so the reply and the notification describe one roster.
* Notification `runtime.gateway.peers.changed {contract, event, peer_install_id,
  peer|null, store_revision, grant_id?}`, fanned out from **the same
  `_emit_peer_event` call site** the EventLog append happens at — one write, one
  process, one moment, so the two lanes cannot disagree about WHEN.

**No watermark, no sequence gate, no re-baseline receipt**, unlike the office
lane, and the reason is the DATA: an office patch is a delta, so a subscriber
that misses one is out of sync; a peer-directory notification carries the whole
row, so a dropped frame costs one row's freshness until that row next changes.
Inventing the three mechanisms would guard a failure this shape does not have.

**The KIND gate.** All three joined `LOCAL_CONSOLE_METHODS`. S3-R13 says "stdio
owner + local console", and that is a KIND and not a strength — the directory is
the operator's own map of their network (which machines they paired, what those
are called, the addresses they answer at), and `roster` additionally DIALS on
the caller's behalf, so admitting a remote caller would let a paired device
spend this install's peer credential. The tier vocabulary has two words, both
about strength; `LOCAL_CONSOLE_METHODS` is the existing answer to kind (WS4 /
R-B). A console-tier DEVICE is refused, and that case is asserted by name.

Named pins that moved for S2d, each deliberately:
`test_scope_use_methods.py` (the set is no longer only the two scope verbs — the
test now pins the SCOPE half exactly and leaves the peer half to S2d's own
file), `test_serve_rpc_authorization.py`'s gate walk (a `LOCAL_CONSOLE_METHODS`
read verb IS refused by the gate, which is the opposite of the plain read arm
and is the whole point of the set), and the literal method-set lists in
`test_serve_rpc_office{,_subscribe,_upsert}.py`, which grew by all six names
this wave adds.

### The e2e evidence (real serve children, two isolated roots)

`tests/agent_runtime/test_gateway_peer_two_roots_e2e.py` grew five tests; the
whole live lane is green:

```
bash scripts/run_tests.sh tests/agent_runtime/test_gateway_peer_two_roots_e2e.py \
  tests/agent_runtime/test_gateway_peer_cross_install_chat_e2e.py \
  tests/agent_runtime/test_gateway_peer_cross_install_media_e2e.py
=== Summary: 3 files, 12 tests passed, 0 failed (100% complete) in 104.5s (8 workers) ===
```

What each proves, on two real `harness serve` children with real TLS:

* `test_introduce_on_b_join_on_a_and_the_device_half_redeems` — B runs
  `introduce` once; A joins with `--expect-fingerprint` from the account;
  the device half redeems against B through `pair_hello` as a phone would.
  Both ends hold the SAME `expires_at` (asserted as a 29-30 day window, not an
  equality); the device row carries `account_device_id == "dev-acct-1"` beside
  its own minted `dev_<hex>`; `grant_payload`'s key set is the backend's;
  `hello_ok.gateway.capabilities` is the four words verbatim.
* `test_a_peer_code_scoped_to_one_install_is_refused_to_any_other_on_the_wire`
  — a THIRD install (C) spends A's code and is refused with the same words a
  nonexistent code gets; nothing is written on B; A then still redeems it.
* `test_the_roster_and_one_far_thread_cross_the_wire_on_real_serves` —
  §0.10 fact 1's proof. A real persona chat thread is seeded on B through
  `ensure_persona_chat_session` + two `append_message` rows, then READ FROM A
  via `peer.thread.read`, so the transcript read happens inside B's live serve.
  Both messages come back in order. A session outside that lane is
  `foreign_session` over the wire. The roster crosses first with exactly the six
  projection fields.
* `test_a_revoke_on_b_reaches_a_as_revoked_you_before_the_next_send` — B's
  revoke announces first over the still-working edge; A's CACHE carries
  `revoked_you` while A's own trust row is untouched (`revoked: false`,
  `usable: false`, `unusable_reason: peer_revoked_you`); A's next
  `resolve_install_target` refuses on that word.
* `test_a_cli_join_beside_a_running_serve_is_visible_with_no_restart` — R-IP12
  E1: the join runs in its own CLI process while A's serve is up, A's serve
  answers about the new row with no restart, and `gateway.peer.recorded` is on
  A's EventLog tail.

**The find, and it is the plan's own §0.10 risk firing live.** The far thread
read first came back `thread_unreadable / chat_scope_unresolved` — which is the
CORRECT failure (closed, typed, never an empty page). Root cause was the
FIXTURE, not the door: `publish_chat_head_home` is a no-op for a process that
named no explicit head, and `_sandbox_env` set `HERMES_HOME` without
`HERMES_HEAD_HOME`, so no serve in that file had ever published a chat-head
pointer. The Launcher always sets both (`HERMES_HOME=profiles/<profile>`,
`HERMES_HEAD_HOME=profiles/base`). The sandbox now sets both, which makes it
model the configuration that ships rather than one nothing produces — and the
ambient rung's fail-closed behaviour keeps its own unit test in
`test_peer_directory.py`. **The §5 argv-lane fallback was NOT needed** and is
not built.

### Verification

Plan §4, verbatim, all green:

```
# S2   9 files, 176 tests passed, 0 failed
# S2b 11 files, 279 tests passed, 0 failed
# S2c  9 files, 178 tests passed, 0 failed
# S2d  3 files,  87 tests passed, 0 failed   (the addendum's own set)
# live 3 files,  12 tests passed, 0 failed   (real serve children, 104.5s)
python scripts/dump_cli_contract.py --check
  -> CLI contract fresh: 191 command paths, sha256 86837537988fdfcf
python scripts/emit_harness_tool_inventory.py --check
  -> tool inventory fresh: 44 tools across 15 toolsets, sha256 36780ed3d8aec5a5
python scripts/doc_cite_adjacency.py --exclude archive --exclude planned
  -> UNWAIVED FAILURES: 0
```

Read-only live sanity on this box (`HERMES_HOME=X:\Eternia\.hermes`), and the
operator's store is byte-untouched — `gateway/` still holds only `install.json`,
so no read created a cache sidecar:

```
harness gateway id --json
  -> capabilities ["announce","introduce","roster","thread_read"],
     endpoints [], endpoints_source "unknown",
     listener {host: null, port: null, source: "unknown"}
     (the lane is off on this root, which is the honest empty answer)
harness gateway peers list --json  -> count 0, items []
```

Whole-suite sweep, `bash scripts/run_tests.sh tests/agent_runtime tests/hermes_cli`
(~9,770 tests): **one red, and it is a pre-existing wall-clock flake** —
`test_read_model_slo::test_synthetic_snapshot_full_build_within_rd0_slo`, which
asserts `build_ms <= 2000` and measured 2938 under 8-worker contention. Measured
rather than assumed:

* green in isolation on this branch, three runs: 1.90 s / 1.92 s / 1.62 s;
* green in isolation with `agent_runtime/`, `tools/` and `hermes_cli/` checked
  out at `origin/main`, two runs: 1.82 s / 1.75 s;
* the one thing this wave adds to that path is `_installs_block()`, hoisted to
  ONCE per snapshot. Measured over an empty store: n=200, mean 0.687 ms.

So the budget was already ~1.8 s of 2.0 s before this branch and a sub-millisecond
addition is not what tips it; the test is marginal under parallel load. Not
weakened and not baselined — recorded here with its numbers.

Two reds the sweep DID own, both fixed forward in `aa9e964411`:

* `test_duplicate_helper_bodies` caught `_announce_roster_changed` copied
  byte-for-byte into `agent_create.py` and `agent_retire.py`. Folded onto one
  authority in `gateway_announce.py` — the module that owns the outbound edge.
  The gate was right.
* `test_persona_tool_diff_declaration` pinned `"dev: 43 tools"` in the
  operator's text read; 43 -> 44 for `agent_chat_installs`, re-measured with the
  ratchet.

### Counts, before and after

| | before | after |
|---|---|---|
| `harness_core` tools / token estimate | 43 / 1149 | 44 / 1177 |
| registered `peer.*` methods | 3 | 6 |
| `PEER_METHOD_ALLOWLIST` | 3 | 6 |
| registered RPC methods (manifest) | 16 | 22 |
| registered event contracts | 59 | 64 |
| `LOCAL_CONSOLE_METHODS` | 2 | 5 |
| CLI command paths | 191 | 191 (verbs replaced flags one-for-one in the count) |
| files under `<store_root>/gateway/` | 4 | 5 (`peers_cache.json`) |

### OWED cross-repo at landing (none of it closable from this worktree)

1. **`tests/fixtures/hermes_cli_contract.json`** -> launcher
   `test/features/mission_control/fixtures/hermes_cli_contract.json`, byte
   mirror. Moved by S2 (`gateway introduce` + its four flags, `peers join
   --expect-fingerprint` / `--correlation`) and by S2c (`peers revoke
   --no-announce`). Final sha256 `86837537988fdfcf8b06acbe1c6571025f923f86b27451da2823bad78f0cd210`,
   191 command paths. Then `flutter test
   test/features/mission_control/harness_capability_argv_test.dart`.
2. **Every regenerated stream golden + `MANIFEST.sha256`** ->
   launcher `test/fixtures/harness_stream/`. S2c registered five events, so
   `decision_contract_hash` moved on every golden that carries it. Files:
   `delta.json`, `delta_agent_create_narrow_profile.json`, `delta_batch.json`,
   `hydrate.json`, `hydrate_authoritative_same_offset.json`,
   `hydrate_running_work_owner.json`, `hydrate_stale_first.json`,
   `MANIFEST.sha256`. Then `python tool/test_quality/check_producer_contracts.py
   --hermes-root=<checkout> --no-generate` and `flutter test
   test/features/mission_control/mission_stream_contract_fixture_test.dart`, and
   update the README's CROSS-STACK COPY STATUS block as S0a's was.
3. **The Agent Command Atlas artifact**, regenerated from
   `docs/agent-runtime-harness/harness-skills/harness-runtime-model/references/tool-inventory.json`
   (S2b's `agent_chat_installs`; 44 tools, sha256 `36780ed3d8aec5a5`).
4. **S2d is now LANDED on this branch**, which closes the launcher plan's OWED
   item (4): S3c's E1 arm has its three methods and its notification, and S3b's
   Refresh has `runtime.gateway.peers.roster`.
