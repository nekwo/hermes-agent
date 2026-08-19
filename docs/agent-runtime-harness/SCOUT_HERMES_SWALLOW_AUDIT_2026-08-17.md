# HERMES LANE-AMBIGUITY AUDIT — main @ ca19df1e48
Delivered by the Explore scout 2026-08-17. Verbatim relay.

BOUNDARY: did not analyze snapshot_build / _BUILD_COALESCE internals. Did read
stream.py:273-441 (patch_batch_frame + _batch_frames_with_liveness) and
snapshot.py:1758-1762 (empty-patches promotion decision + read_model.enabled
reader) — flagged as possible overlap with the other two agents.

=====================================================================
1. BLANKET SWALLOWS THAT ERASE ERROR CLASS
=====================================================================

--- A. The S1 fix's SIBLINGS, all in hermes_cli/harness.py, all still live ---

* harness.py:3298-3318 `_usage_lane_detected` — `except Exception: return False`.
  "not signed in" vs "credential resolution threw" ambiguous; docstring :3300
  says undetected providers are OMITTED, so on a fault the lane VANISHES from
  the Limits panel — STRICTLY WORSE than the "no usage data" S1 fixed (no row
  left to carry a reason).
* harness.py:3261-3276 `_codex_usage_login_detected` (swallows :3269, :3275) and
  :3279-3295 `_openrouter_usage_login_detected` (:3287, :3294) — feeders.
* harness.py:3526-3529, 3536-3539, 3542-3547 `build_account_usage` — three
  swallows collapsing to `lanes: []`; `_render_account_usage_human` then prints
  :3562-3563 "No account-usage lanes (no signed-in providers detected)." — a
  POSITIVE CLAIM about auth state, false whenever detection/fetch collapsed.
* harness.py:3614-3630 `_emit_usage_json` — serialize failure prints
  `_empty_usage_envelope()`; `_cmd_usage` returns 0 always (:3651).
* harness.py:3099-3126 `build_provider_visibility` — four `except Exception:
  pass` dropping environment/api_keys/auth_logins/catalog blocks. Absent is
  ambiguous between "old hermes" and "this block threw" (:3095-3098). Worst is
  `catalog` (docstring :3039-3040): built to distinguish "never configured" from
  "configured and dead"; a throw makes the block vanish and the client falls
  back to exactly the indistinguishable rendering it exists to end.
* Upstream swallow live at agent/account_usage.py:893-901; TWO sibling consumers
  still route through it: cli.py:11061-11072 (/usage) and
  gateway/slash_commands.py:4891-4900 (gateway /usage). Both render 401 as "no
  usage data". Only hermes_cli/harness.py was routed around.

--- B. office_store.py ---

* :683-692 `_read_actor_dir` — `except Exception: continue`. HIGHEST-VALUE
  read-side swallow. Unreadable/half-written/permission-denied actor JSON is
  indistinguishable from nonexistent. Chokepoint under `list_actors` (:404-408)
  -> `_office_projection` (serve_rpc.py:442) -> BOTH runtime.office.get AND the
  subscribe baseline. `actors_truncated` (serve_rpc.py:481) computed from the
  shortened list, so a dropped file reports actors_truncated: 0 ("projection is
  complete") while it is not. Also feeds the truncation guard at
  office_store.py:225 so a dropped file can suppress the honest refresh. No
  count of skips anywhere.
* :461-467 — archived actor unreadable -> archived=None -> base_revision falls
  to 0 (:470) -> revision restarts at 1. remove's ack contract
  (serve_rpc.py:1027-1032) carries the archived revision as guard token; an
  unreadable archive silently resets it, so a stale expect_revision can pass.
* :415-423 `conflict_actor_keys` — read failure -> key falls to `path.stem`.
  "key the sidecar names" vs "the filename" rendered identically.
* :674-678 `archive_actors_for_instance` — `except Exception: continue`,
  returns count. 0 conflates "no placements" with "every archive raised", in
  the phantom-desk prune lane.
* :153-160 `_emit` — domain-event append best-effort (logged). Feeds the
  structural defect below.
* :816-830 `_archive_conflict_sidecar` — synthesizes a payload on exception;
  flagged for follow-up, not read to the end.

--- C. THE STRUCTURAL ONE (highest severity found) ---

AN EMPTY `patches` LIST CAN SHIP AS A `patch` FRAME — client advances its
watermark having folded nothing; row permanently stale, unrecoverable by any
gate.

stream.py:292-299 `patch_batch_frame` filters rows to STATE_PATCHED_EVENT_TYPE
with NO non-empty guard; stream.py:422-430 promotes purely on
`batch_is_patch_coverable`. Every LIVE_COVERED_DOMAIN_EVENT_TYPE
(patch_coverage.py:213-294) is coverable ON ITS OWN, so any batch carrying the
domain event WITHOUT its paired patch ships `patches: []` stamped with the
batch watermark.

state_patches.py:1032-1039 names this exact failure and it is the stated reason
`emit_office_actor_refresh` exists — but the guard is in the PRODUCER, not the
frame builder, and four+ paths re-open it:
  1. office_store.py:221-234 `_emit_actor_patch` swallows -> office.actor.upserted
     (:509-516, AFTER the patch at :508) rides alone.
  2. office_store.py:261-270 `_emit_surface_patch` swallows ->
     office.surface.updated (:385-390) rides alone.
  3. office_store.py:284-293 `_emit_actor_remove_patch` swallows ->
     office.actor.removed rides alone.
  4. NO exception needed: office_store.py:383-384 skips the surface patch when
     surface_existed is False while still emitting office.surface.updated at
     :385. Only the uncovered office.surface.created in the SAME drain batch
     saves it; if the two split across the 200ms coalescing boundary, or the
     created _emit at :331 swallows, the second batch is the event alone ->
     empty patch frame.
  5. Cross-process: `delta_patches_enabled` (state_patches.py:326-397) evaluated
     independently in writer and stream producer. A transient root-config fault
     in the WRITER returns FALLBACK_DELTA_PATCHES=False (:374/:386/:396) so no
     patch is emitted, while the stream process reads config fine and promotes
     the domain-event-only batch. Warning lands only in the writer's log.

Fix shape: refuse to promote when the filtered patches list is empty
(stream.py:429 or inside patch_batch_frame) — honest answer is the full core.

--- D. state_patches.py / patch_coverage.py / serve_rpc.py ---

* state_patches.py:531-538 `_persona_for` -> `except Exception: return None`.
  "no such persona" vs "AgentStore unreadable"; ships a null field rather than
  demoting.
* CORRECT-SHAPE CONTRASTS (cite as the standard): state_patches.py:764-772
  (None is a documented third answer); :312-323 `_root_config_fault` TYPED
  tuple; :326-397 `delta_patches_enabled` degrades off AND warns with class +
  config path. patch_coverage.py has ZERO blanket excepts.
* serve_rpc.py:339-347 — dispatcher boundary -> -32000
  {"reason": "handler_failed", "method": name}; class name NOT a field (only in
  message), so a decoder cannot separate store fault from bug. Low severity;
  where every unhandled office/agent-create fault lands.
* serve_office_subscriptions.py swallows are all teardown/log paths, each
  argued; only :748-754 worth naming (probe-vs-stats failure erased) and its
  consequence is correct-and-expensive. Acceptable.

=====================================================================
2. serve_office_subscriptions.py (read whole, 977 lines)
=====================================================================

Scope predicate verbatim (:445-456), prefix = f"{workspace_id}/" at :371:

    rows = [patch for patch in frame.get("patches") or [] if isinstance(patch, dict)]
    in_scope = any(
        patch.get("entity") == OFFICE_ACTOR_ENTITY
        and str(patch.get("id") or "").startswith(prefix)
        for patch in rows
    )
    if not in_scope:
        return

Gate on the whole FRAME, not a row filter. Admits iff >=1 row is office_actor
with slash-prefixed id; once admitted the WHOLE batch forwards unfiltered
(:481-491, deliberate per §V6 :457-480).

CONFIRMED: folder-only (office_surface) patch frame is DROPPED.
emit_office_surface_patch stamps entity_id = bare workspace id, no slash
(state_patches.py:997), entity office_surface (:929). Fails the gate TWICE.
Batch whose only state.patched is the surface row returns at :456 with NO patch
AND NO resync (frame type is patch, resync arm :399-444 already passed).
folders / surface revision / updated_at silently stale. The WV-H3 producer
(office_store.py:384) and its capability token (patch_coverage.py:179,
:311-313, :542-543) have NO CONSUMER on the push lane. Reachable on every
folder rename with no actor write in the batch. Mixed batches unaffected —
which is why it survives most testing.
Fix shape: gate must also accept entity == OFFICE_SURFACE_ENTITY and
id == workspace_id.
Related, same cause: `_delta_touches_workspace` recognizes only
OFFICE_ACTOR_ENTITY on state.patched (:311-315); saved in practice because the
paired office.surface.updated hits the event_type.startswith("office.") arm
(:301-310) and office_store.py:385-390 includes workspace_id — DELTA lane
covered, only the PATCH lane is not.

Paths delivering office state to a client: 5 live + 1 orphan
1. runtime.office.get (serve_rpc.py:360) — pull.
2. subscribe reply baseline (serve_rpc.py:730-750) — pull; SHARES
   `_office_projection` (:423) with (1) so they cannot disagree (:426-429).
3. runtime.office.patch notifications (:481-491) — push.
4. runtime.office.resync notifications (:433-443 + drop path :694-709) — push,
   refetch-only.
5. Stream lane: NDJSON child AND socket subscribe (serve.py:2258-2407), same
   hub, same stream_frames generator (serve.py:1749-1757).
6. Orphan: launcher's cached snapshot.json boot paint —
   state_patches.py:788-812 records no freshness gate, no receipt, no owner.

Fences after FC-H1: baseline gate :397 BEFORE the type branch (:381-396) —
ordering stops the hydrate->resync->restart loop. Unplaceable-watermark third
answer :337-354 -> resync arm (:429-432). Delta scoping (O-H4) :262-316 +
:422-425. Re-baseline receipt :819-862, producer_restarted MEASURED off
hub.stats() generation. FC-H1 added the verbatim reason (:842) + boundary
validator (:206-238), refused at serve_rpc.py:667-673 BEFORE the office lock.
Non-narrowing rejoin skip (O-H5) :745-753 measured against serve.py's
_accepted_fold_entities (serve.py:1683-1721). Client: one MissionReadModel,
one sequence, base == held gate (:461-469).

CAN ANY PATH DELIVER THE SAME BATCH TWICE? YES, two ways:

(a) BY DESIGN, no server-side dedup. A connection holding BOTH the stream
subscription (bare key, serve.py:2304) and an office subscription (namespaced,
serve_office_subscriptions.py:334) is two hub subscribers — every coverable
batch fans to it twice (raw patch frame + runtime.office.patch re-envelope at
the same watermark, same rows :481-491). Only fence is the client's base==held
gate. Stated at :17-23 and :457-480; D7 is the plan to retire the second
producer.

(b) LIVE DEFECT — baseline_offset silently becomes 0 when the event log cannot
be stat'ed. serve_rpc.py:692-695:

    try:
        baseline_offset = int(events_watermark().get("event_offset") or 0)
    except (TypeError, ValueError):
        baseline_offset = 0

events_watermark returns {"event_offset": None, "event_offset_error": ...} on
OSError (parity.py:205-213); docstring :189-202 says the stat "can fail — on
this runtime's platform routinely, under AV scanning or a share violation" and
"Zero is the single most damaging value this field can carry". `None or 0` -> 0
with NO exception — the typed except never fires, event_offset_error discarded.
Consequences: reply advertises watermark 0 (:734), indistinguishable from an
empty log; sink gate :397 becomes <= 0 (no gate); the mandatory post-subscribe
hydrate is not dropped, hits the frame_type != patch arm, emits unconditional
resync (:433-443) -> client re-subscribes -> producer restart -> fresh hydrate
-> exactly the loop the gate ordering exists to prevent, every other subscriber
paying ~822KB core per lap; every buffered frame at any offset (re)delivered.
Honest handler exists next door: stream.py:499-503 takes None ->
resync_pending=True and refuses to invent 0 (reasoning stream.py:484-490). The
subscribe lane is the one reader still doing `or 0`.
Fix shape: treat None as "cannot baseline" -> typed transient refusal
(vocabulary exists: NO_PUSH_LANE :539 / PUSH_LANE_DRAINING :545) or force
resync-first registration.

=====================================================================
3. DUPLICATE ANSWER PATHS
=====================================================================
* Office READ, get vs subscribe baseline — ONE chokepoint `_office_projection`
  (serve_rpc.py:423-482), explicitly so they cannot disagree. Clean.
* Office read vs `harness office show` (office.py:130) — FORK. RPC refuses
  unknown workspace (serve_rpc.py:408-419); office show answers honest empty;
  divergence named and defended (:409-413).
* Stream vs push — one producer; office sink is a re-envelope, not a second
  derivation. Fork is the SCOPE PREDICATE only — where the office_surface bug
  lives.
* Office WRITES, upsert: three writers — runtime.office.upsert (serve_rpc:814),
  harness office actor-upsert (office.py:149), perform_agent_create
  (agent_create.py:810-813). All share OfficeStore.upsert_actor AND all call
  class_key_collision separately (serve_rpc:928, agent_create:794, office.py) —
  fence at three call sites, not in the store;
  scripts/office_actor_rekey_to_instance.py:95-97 flags any new writer reaching
  _write_actor directly is unfenced. Documented divergence: RPC refuses to
  lazily author a surface (:920-926, :1299-1305); CLI authors one (:829-840).
* remove: runtime.office.remove (serve_rpc:1017) + harness office actor-remove
  (office.py:239) + OfficeStore.archive_actors_for_instance (office_store:653).
  The THIRD bypasses surface-exists check and revision guard, swallows
  per-actor failures (:674-678).
* surface.update: RPC (serve_rpc:1190) + harness office set-folders
  (office.py:273). NAMED encoding fork: RPC takes a list; argv joins on commas
  and _safe_folder keeps commas, so "Design, Ops" splits into two folders on
  the CLI lane only (serve_rpc.py:1203-1212). One store, two input grammars.
* agent create: runtime.agent.create (serve_rpc:1352) and harness agent create
  (persona_commands.py:398) both -> perform_agent_create — ONE chokepoint
  (agent_create.py:604), 5-line shim (serve_rpc:1389-1395). Cleanest lane pair
  in the audit. Legacy persona instance create/open-chat --add-instance still
  call PersonaInstanceStore().add_instance directly (persona_commands:547,
  :740) — share the PREDICATE but not the SEQUENCE: roster row with no
  placement by design (persona_commands:413-417).
* Provider/model catalog: build_provider_visibility (harness.py:3047) = one
  payload from four independently-swallowed sub-builders (:3099-3126) +
  provider_login_catalog (:3042); `hermes auth list` is the human fork it was
  built to replace (:3048-3053).

=====================================================================
4. DEAD-CODE CANDIDATES
=====================================================================
* UNREACHABLE: harness.py:3355-3357 — _fetch_usage_lane's trailing
  fetch_account_usage fallback. Only runs for a provider outside
  {nous, openai-codex, anthropic, openrouter}; _USAGE_LANE_PROVIDERS (:164-169)
  is exactly those four, _detect_usage_candidates (:3462-3467) filters to that
  tuple. The fallback S1 routed around — now the only surviving route into the
  upstream swallow from this module, and nothing can reach it.
* runtime.office.resolve_conflict NEVER LANDED. **SUPERSEDED 2026-08-19: it
  landed at `32a392364b`, four hours after this document was committed. The
  registry holds EIGHT `@method` handlers, and 7 of the 8 have launcher
  callers — `runtime.office.unsubscribe` has zero, which is a launcher wiring
  gap and not a hermes deletion.** Original text follows.
  Registry holds exactly 7
  methods (get :360, subscribe :485, unsubscribe :753, upsert :814, remove
  :1017, surface.update :1190, agent.create :1352). harness office
  resolve-conflict (harness.py:767-775 -> office.py:294 -> office_store:583)
  is the only lane, and the one write path reaching _write_actor outside
  upsert_actor (office_store:629) with its own fence _guard_class_keyed_adoption
  (:623). Not dead code — the RPC-parity assumption is false.
* office actor-restore is NOT DEAD — REFUTES the launcher scout's candidate.
  harness.py:752-758 -> office.py:259; launcher capability registered
  (harness_capability_registry.dart:253), argv builder bridge:4112-4121, tests
  harness_capability_argv_test.dart:613 + gateway_manifest_test.dart:143;
  serve_rpc.py:870 names it as one of two sanctioned operator-intent overrides
  for the class-key fence. [Coordinator note: the launcher scout's "fully dead"
  meant no UI submit site; the verb is the operator RECOVERY lane — it was used
  live to restore the 2026-08-15 mass-archive. Both true; not dead.]
* The 822KB PUSH gate is LIVE, not always-on: read_model.delta_patches
  SHIPPED=True (runtime_config.py:73) FALLBACK=False (:82), sole reader
  state_patches.delta_patches_enabled (:326), registered root-only
  (config.py:361-362), operator's explicit false wins (config.py:626).
  822,671-byte measurement runtime_config.py:63-66.
* SCOPE SPLIT, real: read_model.enabled has two readers at DIFFERENT resolution
  scopes — snapshot.py:1758-1759 (profile-aware cfg) vs _cmd_snapshot
  (load_root_runtime_config(), runtime_commands.py:480-484); config.py:357
  documents profile-aware. So harness snapshot's prefer_cache can disagree with
  build_snapshot for the same key — the exact misplacement class that kept the
  delta-patch lane dark for its whole life (runtime_config.py:55-67).
* Correct-pattern, NOT candidates: HISTORICAL_COVERED_DOMAIN_EVENT_TYPES
  (patch_coverage.py:204-209, both-ways partition test :200-203); S54/S66
  removal notes state_patches.py:1053-1062; serve_snapshot_from_db has one
  reader (runtime_commands.py:484).

=====================================================================
5. AGENT CREATE VALIDATION (4df766d49c / 442f2d1c3a / 3772e2529e)
=====================================================================
Every MINTING lane validates against the roster, one shared predicate:
* runtime.agent.create -> perform_agent_create -> normalize_agent_create
  (agent_create.py:407-419) -> _persona_is_unknown (:249-287) -> STRICT
  persona_roster() (:161-177); read fault raises PersonaRosterUnavailable ->
  reason persona_roster_unavailable (:413), provably before any store touch
  (:372-376).
* harness agent create -> same function (persona_commands.py:462-464).
* persona instance create --add-instance + create_operator_chat branch ->
  require_known_persona at persona_commands.py:536, before both store calls
  (:547, :556).
* open-chat --add-instance -> require_known_persona at :699 before add_instance
  at :740; scoped to --add-instance only, :691-696 argues why rebind branches
  must NOT be fenced.
* open-chat --new-session mints no roster row (:956-967).
NO LANE LEFT THAT MINTS WITHOUT VALIDATION. Two narrow gaps:
* agent_create.py:278-279 — _persona_is_unknown returns False for ANY non-None
  persona. Safety argument "caller's resolver is a strict superset" is true
  today for _persona_by_id (persona_commands.py:6045-6095, synthesizes only for
  profile: ids = the D-U1 carve-out) but is an UNENFORCED invariant: a future
  caller passing a persona from a looser resolver disables the roster check
  with no assertion or test fence at the seam.
* LANE DIVERGENCE on roster fault: _persona_by_id calls
  ensure_persisted_personas UNWRAPPED (persona_commands.py:6049), and
  _cmd_agent_create calls it at :463 BEFORE perform_agent_create. So an
  unreadable roster answers runtime.agent.create with
  {"reason": "persona_roster_unavailable"} and answers harness agent create
  with a RAW TRACEBACK — same question, two renderings, and the CLI is the
  operator-facing one.

=====================================================================
TOP 4 BY SEVERITY (scout's ranking)
=====================================================================
1. serve_rpc.py:692-695 — `or 0` turns an unreadable event log into baseline 0,
   killing the sink's baseline gate, re-opening the documented
   resync<->restart loop; honest handler exists at stream.py:499-503.
2. stream.py:292-299 + :422-430 — no non-empty guard on patches; covered domain
   event without its paired patch ships a frame that advances the client's
   watermark having folded nothing; re-opened by three office_store swallows
   and by :383-384 with no exception at all.
3. serve_office_subscriptions.py:445-456 — office_surface can never satisfy the
   scope gate; every folder-only patch frame dropped, no patch, no resync.
4. harness.py:3298-3318 (+ :3536-3547, :3562-3563) — S1's unfixed siblings: a
   credential fault deletes the lane entirely; an empty envelope renders as a
   positive claim that no provider is signed in.
