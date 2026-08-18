# MISSION_CONTROL_LEDGER_REFACTOR_PLAN — 2026-08-17

> **Home.** `hermes-agent/docs/agent-runtime-harness/`, beside `REFACTOR_DEBT_AUDIT_2026-08-17.md` and `MISSION_CONTROL_ENTERPRISE_PLAN_2026-08-17.md`. Source of truth: the vault ledger `Launcher_Brain/20 — Active Initiatives/plan-eg-weakness-escalation-ledger.md` at launcher `fd70e8fb2`. Repos as read: launcher `fd70e8fb2` (main), hermes `0cc106724c` (main) — both verified (RAN, `git log --oneline -5`).
>
> **This is a diagnosis-and-plan document.** No code was changed in its production. Every stage-load-bearing claim below was re-read first-hand this session at the SHAs above (READ); ledger rows repeated without re-verification are tagged LEDGER.
>
> **Standing constraints (operator orders, binding on every stage):** priorities READABILITY > EFFICIENCY > RELIABILITY > PERFORMANCE; one launcher implementer at a time; mutation evidence per stage; witnesses assert counts / ordering / typed reasons, **never elapsed-ms**; explicit-path commits; `dart format` forbidden; never write `X:/Eternia/.hermes/`. WV-L6 is in flight in `lib/features/mission_control/office/` — office file:line numbers below are as of `fd70e8fb2` and must be re-anchored on symbols at dispatch.

---

## 0. Ledger-vs-tree verdict — what the code has outrun, what is amended

Every candidate group was checked against the current tree before staging. Corrections:

1. **B3 is DROPPED — WV-L6 is closing it now.** The launcher half of `resolve_conflict` correlation is the in-flight stage's scope. What survives is its hermes shadow, **C15**: the landed handler validates and echoes `correlation_id` (`agent_runtime/serve_rpc.py:379-418` validator, `:1786`, `:1922-1923` tombstone carry, `:1940-1941` echo — READ) while `tests/agent_runtime/test_serve_rpc_office_resolve.py` contains **zero** occurrences of `correlation` (RAN, grep count 0). Staged as ML-1.
2. **B2 is CLOSED as recorded; the CLASS is live in three verified places.** `scan_actors`/`ActorScan` landed (`agent_runtime/office_store.py:60-78`, `:492`, `:1015` READ; `serve_rpc.py:537-541` reads it, and `snapshot.py:1638-1642`'s comment spends the same lesson). But the silently-skipping-lister class survives where the ledger predicted it would: `office_sync.py:83` and `:210` still iterate `list_actors` (the thin view) under realm publish/compare — a live second **writer** deciding on partial knowledge; `office_sync.py:120-131` skips undecodable pulled actor files with `except Exception: continue` inside the pull path; `snapshot.py:1633-1637` silently drops a whole workspace from the snapshot when `get_surface` throws; `board_store.py:118-127`, `:512`, `:524` skip undecodable board files. All READ. Staged as ML-8 (the flagship).
3. **B6 amended: five `.undo()` sites, not four.** `tests/plugins` holds two (`tests/plugins/memory/test_holographic_store.py:199` and `tests/plugins/platforms/photon/test_sidecar_paths.py:137`), plus `tests/cron/test_cron_profile_isolation.py:66`, `tests/tools/test_lazy_deps_venv_barrier.py:142` (a deliberate mid-test drop, comment says so), `tests/hermes_cli/test_plugins.py:215` (all RAN). The gate itself (`tests/agent_runtime/test_no_midtest_monkeypatch_undo.py`) documents that widening happens by hand with reasons (`:59-70` READ). Staged as ML-4.
4. **C12's rule already lives in the registry — as a buried comment.** `test/features/mission_control/mission_control_tombstone_registry_test.dart:2925-2932` carries "A retype is not a retirement" beside the deliberately-not-rowed `_officeLayoutPendingSave` (READ; seven live occurrences confirmed at `mission_control_page.dart:453,587-588,4006,4022,4040-4042`). Remaining S-e launcher work is F4's row collision: the unscoped `isUnavailable` row (`registry_test.dart:204-210`) reds against four live, legitimate `lib/features/education/` uses (`media_availability.dart:101`, `lesson_video_surface.dart:278-279`, `video_source_providers.dart:117` — all READ), while the in-file scoping pattern already exists one row down (`maxMessageLength`, `:218-227`, `scopes:`). Staged as ML-5.
5. **B5 verified current, and "eight" checks out.** Exactly 8 `@method` registrations reachable from the serve lane: 7 office verbs (`serve_rpc.py:455,592,920,981,1256,1471,1649`) plus `runtime.agent.create` (`:1948`) — RAN. `docs/agent-runtime-harness/harness-serve-design.md` method-lane example still lists the first four; `hermes_cli/harness_parts/serve.py`'s doc block (~`:93-99`) still names only `runtime.office.get` | `runtime.office.upsert`. Staged in ML-2.
6. **C7 is PARTIALLY CLOSED — S-d shrinks to the launcher half.** EG-2.1's `log_stream_attach` landed (`agent_runtime/stream.py:131,161`; used by `serve_office_subscriptions.py:900-903` — RAN). What remains of S-d is C6, and it is **two** vocabulary splits, not one, in the same function: `mission_read_model.dart:980` writes receipt reason `patch_without_base:` while `:982` logs `REFUSED no_base`; `:995` writes `patch_gap:` while `:1000` logs `REFUSED gap:` (READ). Staged as ML-6.
7. **A9 verified live, mechanism confirmed end-to-end.** The host resolves the render model itself in `build()` (`mission_office_host.dart:166-170` READ) and hands it to Scene Health (`:1288`); the probe copies `model.interactionMode` (`mission_office_render_probe.dart:46`); Scene Health prints it (`mission_office_scene_health.dart:170`); the game mutates its **own** `_resolved` copy at pan start (`mission_office_game.dart:244`), which the host's freshly-resolved model never sees. The `mode` line is structurally pinned to `idle`. Staged as ML-9 (serialized after WV-L6).
8. **B9/B10/C13 verified current** (written by EG-6.7 at this HEAD, re-confirmed): `hermesCredentialHealthCue` at `mission_control_hermes_visibility.dart:305` has zero production callers and one test group (`mission_control_hermes_visibility_test.dart:681-696`); `configuredApiKeys`/`missingApiKeyCount` fields at `:755-763`/`:822-823` have no reader outside the wire-contract test (`:598-599`, `:635-636`); `catalogIdFor` at `:1302-1313` falls back silently, and no `catalogAliasFallback` receipt kind exists anywhere in `lib/` (RAN). Staged as ML-3.
9. **C16 is CLOSED** (`f2bf3f35a`, pinned by mutation M10) — no work; its lesson (a default arm returning one state's copy is fail-quiet) is carried into ML-8's gate specs.
10. **A7 verified with its bill** at `mission_control_serve_session_io.dart:1058-1083` (READ) — the comment itself names the cure this plan stages (ML-11) and the constraint (witnesses count activations, never ms).

---

## 1. Stages, in operator priority order

Within a priority band, cheapest first. Stage ids `ML-n`, ledger group in parentheses. Every stage: rolls back by reverting its commit; commits with explicit paths; no `dart format`; launcher stages follow the F6 pubspec procedure (§4).

### Priority 1 — READABILITY

---

#### ML-1 (S-i) — hermes: the resolve RPC's `correlation_id` echo gets its canonical-suite pin — **S**

- **Defect class retired:** behaviour nobody's test defends (C15). WV-L6's caller sends the token on every gesture; the hermes echo is currently free to regress silently.
- **Files/symbols (verified):** `agent_runtime/serve_rpc.py` — `_correlation_id_param` (`:379-418`), `CORRELATION_ID_INVALID_REASON` (`:368`), the resolve handler's accept/carry/echo (`:1786`, `:1810`, `:1922-1923`, `:1940-1941`); `tests/agent_runtime/test_serve_rpc_office_resolve.py` (currently correlation-free).
- **Target shape:** one new test group in the existing file; no production code moves.
- **Gate:** `test_resolve_conflict_echoes_the_callers_correlation_id` — sends the resolve with a generated token, asserts (1) `result["correlation_id"]` equals the sent token, driven with **two distinct tokens** across two calls; (2) a resolve sent **without** the token has no `correlation_id` key in the result; (3) a malformed token (non-string; overlong) answers invalid-params (`-32602`) with `data.reason == "correlation_id_invalid"`; (4) the minted tombstone carries the token (store read-back).
- **Killing mutation:** delete the echo lines (`:1940-1941`). The mutant cannot echo a token it never writes; a constant-echo second-order mutant fails the two-distinct-token drive; an always-echo mutant fails the absent-key case.
- **Blast radius:** one test file. Zero production risk.
- **Does NOT buy:** the launcher half (WV-L6's), correlation on `office.surface.created` (B4, unstaged), or any receipt census.

---

#### ML-2 (S-g) — both repos, docs only: the register sweep — **S**

- **Defect class retired:** register rot — plan/design sentences the tree now contradicts (B5, B8, C14's rule).
- **Files (verified):**
  - `docs/agent-runtime-harness/harness-serve-design.md` — the method-lane example line naming four methods; reword to name all eight or to reference the advertised set with "currently eight; the registry in `serve_rpc.py` is the authority".
  - `hermes_cli/harness_parts/serve.py:93-99` — the `method:` doc arm naming only get|upsert; same fix.
  - `docs/agent-runtime-harness/PROVIDER_LOGIN_FIRST_CLASS_PLAN_2026-08-16.md` — every "v3" sentence (verified at `:38, :154, :233, :254, :259, :265-266, :310`) re-worded per B8's binding correction: the catalog landed **additively on `hermes.provider_visibility/v2`, feature-detected by presence**; "v3" never shipped.
  - `docs/agent-runtime-harness/MISSION_CONTROL_ENTERPRISE_PLAN_2026-08-17.md` — the "serve.py/office.py" shorthand corrected: the resolve RPC handler lives at `agent_runtime/serve_rpc.py:1649` (verified).
  - `docs/agent-runtime-harness/UNIFIED_GESTURE_PREDICTION_PLAN_2026-08-16.md` §1/U-1 — append C14's correction verbatim: repo-wide, `interactionMode` has readers at `mission_office_render_probe.dart:46` and `mission_office_scene_health.dart:170`; and the rule: **dead-symbol claims are repo-scoped or they are nothing.**
  - The F4 handover: a note (vault + harness task #41) assigning the education-lane `isUnavailable` collision to that lane's queue — ML-5 fixes the registry's own half; the sweep only records the handover, does not absorb it.
- **Gate:** RD-0 precedent — no behaviour, so no behaviour gate; the commit quotes each re-grep receipt (the `@method` count, the "v3" line list) verbatim. **Optional hardening, needs operator ruling R-a:** a hermes doc-drift test that parses the serve-design method-lane line and compares it to `sorted(_METHODS)` — killing mutation: remove one method name from the doc line. Not assumed.
- **Blast radius:** docs only; anytime; collides with nothing.
- **Does NOT buy:** any behaviour; the PL-6 "v3" gate's satisfiability (that is C13's receipt, ML-3).

---

#### ML-3 (S-j) — launcher: the provider-surface retirements PL-6 exposed — **S**

- **Defect class retired:** dead code propped up by its own test (B9); a writer-less/reader-less ambiguity trap (B10); a retirement gate with no observable (C13 — the C9 class on the provider surface).
- **Files/symbols (verified):** `lib/features/mission_control/data/mission_control_hermes_visibility.dart` — `hermesCredentialHealthCue` (`:305`), `configuredApiKeys`/`missingApiKeyCount` (`:755-763`, `:822-823`), `catalogIdFor` (`:1302-1313`); `test/features/mission_control/mission_control_hermes_visibility_test.dart` (`:681-696` cue group; `:598-599`, `:635-636` wire pins); `test/features/mission_control/mission_control_tombstone_registry_test.dart` (row table at `:188+`); receipt kinds in `lib/features/mission_control/data/mission_transport_receipt.dart` + log at `mission_transport_receipt_log.dart` (`receipts.jsonl`, verified `:15`, `:89`).
- **Target shape, three items:**
  1. **B9:** delete `hermesCredentialHealthCue` WITH its test group; add a `Tombstone('hermesCredentialHealthCue', form: name, …)` row to the registry with the reason ("orphaned by PL-4's tile; kept alive solely by its own test group").
  2. **C13:** `catalogIdFor` stops being silent — it reports which lanes took the alias branch (e.g. the built report carries `aliasFallbackLanes`), and the caller that owns the receipt log records one `MissionTransportReceiptKind.catalogAliasFallback` per lane id onto the existing `diagnostics/mission_transport/receipts.jsonl` lane — exactly EG-6.7's deliberately-unimplemented proposal, implemented out loud. This makes PL-6's "zero fallback receipts across N sessions" gate satisfiable for the first time.
  3. **B10 — needs operator ruling R-b:** arm (i) prune the parse as a wire-contract item, or arm (ii) keep it and document at the declaration "wire pin, no UI consumer — the parse records a live `api_keys` block" and retitle the test group to say so. Default proposal: arm (ii) (the ledger calls the parse defensible). The stage lands whichever arm is ruled; the point is the decision is recorded at the site.
- **Gates:**
  - `the tombstone registry bans the cue` — killing mutation: reintroduce a `hermesCredentialHealthCue` declaration; the token-stream scanner reds (comment-immune by construction, so the retirement prose cannot false-positive).
  - `a lane resolved through the alias map is billed; a catalog-resolved lane is not` — two fixtures: catalog absent/missing the lane → `aliasFallbackLanes` contains exactly the driven lane ids (two distinct ids across two fixtures) and one receipt per lane is recorded; catalog covering the lane → zero fallback receipts. Killing mutation: restore the silent alias branch (report nothing). The mutant yields an empty `aliasFallbackLanes` against fixtures driving two ids; the always-bill second-order mutant fails the zero case.
- **Blast radius:** `data/` + tests; not in `office/`, so no WV-L6 file overlap — but launcher stages serialize regardless (one implementer). `hermes_install_panel.dart:1673`'s `degradationNote` rendering is untouched.
- **Does NOT buy:** the alias map's actual retirement (that needs the field window the receipt creates); Settings/OAuth live verification (E5/E6, field-gated).

---

#### ML-5 (S-e, launcher half) — the tombstone registry stops redding on another lane's live code — **S**

- **Defect class retired:** an over-broad dead-symbol row disabling the registry's enforcement (F4's launcher half; C12's rule made a headline instead of a buried comment).
- **Files/symbols (verified):** `test/features/mission_control/mission_control_tombstone_registry_test.dart` — the unscoped `isUnavailable` row (`:204-210`); the in-file scoping precedent (`maxMessageLength`, `:218-227`); the C12 note (`:2925-2932`).
- **Target shape:** scope the `isUnavailable` row to the agent-chat surfaces the s40 wave actually retired (the `scopes:` pattern one row down), with the reason updated to name the education collision and task #41; hoist "a retype is not a retirement" from the `:2925` comment into the registry's header doc as a named rule. Education files untouched (their four uses are live and legitimate — verified).
- **Gate:** the registry itself, now green over full `lib/` — plus a pinned pair: killing mutation (1) reintroduce `isUnavailable` in a file inside the row's new scope → row reds; discriminator (2) the education files as they stand stay green. The pair pins the scope boundary; neither ban-everywhere nor ban-nowhere passes both.
- **Blast radius:** one test file. Also a reliability gain out of band: EG-5.3's registry rows become enforced again (the gate stops being excluded as known-red).
- **Does NOT buy:** the education lane's own naming decisions (handed over, ML-2); B6 (hermes half, ML-4).

---

#### ML-4 (S-e, hermes half) — the no-`undo()` gate covers the whole test tree — **M**

- **Defect class retired:** a structural gate whose scope is one directory, with five unaudited sites outside it (B6, amended count).
- **Files/symbols (verified):** `tests/agent_runtime/test_no_midtest_monkeypatch_undo.py` (scanner + self-test table `:260-281`; widening policy `:59-70`); the five sites: `tests/cron/test_cron_profile_isolation.py:66`, `tests/plugins/memory/test_holographic_store.py:199`, `tests/plugins/platforms/photon/test_sidecar_paths.py:137`, `tests/tools/test_lazy_deps_venv_barrier.py:142`, `tests/hermes_cli/test_plugins.py:215`.
- **Target shape:** read each site (the widening-without-reading refusal was EG-0.1's stated reason — this stage does the reading); convert each to a scoped pattern (`pytest.MonkeyPatch.context()` or a scoped fixture) preserving each test's intent — `test_lazy_deps_venv_barrier.py:142` drops a stub deliberately for one probe, so its conversion must keep that probe outside the stub's scope rather than un-stub mid-test; then widen the gate's scan roots to `tests/` with the per-dir reasons recorded in the gate's own docstring, per its stated policy.
- **Gate:** the widened gate. **Killing mutation:** re-add a mid-test `monkeypatch.undo()` to one converted file → the AST scan reds. (The detector's own self-test table already proves the scan is comment-immune and receiver-spelling-immune, so the mutation cannot hide in prose.)
- **Blast radius:** five test files across four directories + the gate. Note the gate is a static source scan run from `tests/agent_runtime` — it does **not** require the F1/F3-blocked directories to be runnable; verifying the five converted tests still pass uses the per-file runner (§4).
- **Does NOT buy:** `tests/hermes_cli` runnability (ML-7); any production behaviour.

---

### Priority 2 — EFFICIENCY

---

#### ML-6 (S-d, shrunk per §0.6) — launcher: one reason vocabulary per refusal, both channels — **S**

- **Defect class retired:** the measured false-zero census (C6) — one event worded differently on its two channels, so a grep on either channel undercounts. Two live instances in one function.
- **Files/symbols (verified):** `lib/features/mission_control/data/mission_read_model.dart` — `_recordDrop('patch_without_base:…')` at `:980` vs diag `REFUSED no_base` at `:982`; `_recordDrop('patch_gap:…')` at `:995` vs diag `REFUSED gap:` at `:1000`. Channel homes: `mission_transport_receipt_log.dart` (`receipts.jsonl`), `missionFoldLogSink` (diag log).
- **Target shape:** the receipt reason token is THE vocabulary; the diag line quotes it verbatim (`[MissionFold] REFUSED patch_gap: base=… held=…`, `REFUSED patch_without_base …`). Plus the one-authority answer to "where does this receipt live": a channel table in `mission_transport_receipt.dart`'s header doc (which already frames the grep contract at `:26`) naming, per receipt kind, the channel and the token — so the next census names its channel by reading one place.
- **Gate:** `a fold refusal words itself identically on both channels` — capture `missionFoldLogSink` and the receipt recorder in one driven refusal each for gap and no_base; assert the diag line **contains the receipt's reason token as a prefix-exact substring**, driven for both refusal shapes (two shapes = two driven values). **Killing mutation:** revert the gap diag line to `REFUSED gap:` — the substring probe cannot match a token the mutant never prints; a mutant that "fixes" it by changing the receipt token instead reds any existing receipt-reason pins and the no_base case.
- **Blast radius:** any existing test or tooling grep pinned to the old diag wording — the implementer must sweep `test/` for `REFUSED gap` / `REFUSED no_base` pins first and update them in the same commit (they are wording pins, not behaviour pins; a stage-stopping event only if a pin turns out to assert behaviour through wording).
- **Does NOT buy:** C7 (already closed by EG-2.1's `stream_attach` lines, §0.6); C8's payload-content receipts (a separate decision, unstaged); any new receipt kind.

---

### Priority 3 — RELIABILITY

Ordered: ML-7 first despite equal size because it gates G1 measurement for everything after it; then cheapest-first.

---

#### ML-7 (S-c) — hermes: test-infra runnability (F1-F3) — **M**, carries three operator rulings

- **Defect class retired:** unmeasurable checkpoints — the full-suite rule (G1) is unsatisfiable while `tests/hermes_cli` cannot run as a directory. This blocks measurement of every other stage, which is why it is a stage and not a footnote.
- **Files/symbols (verified):** `hermes_cli/profiles.py:1380` `_profile_bound_backend_pids` (F1 — iterates real processes; caller `:1501`); `tests/cli/test_surrogate_sanitization.py` (F2 — real TCP connect, 30s thread timeout); the 41 `test_web_server_*`/`test_web_ui_build.py` cross-file failures under a single interpreter, all green in isolation (F3 — 19 such files present, RAN).
- **Operator rulings required before dispatch (the environmental parts are not the implementer's to decide):**
  - **R-c (F1):** may `profiles.py` grow an injectable process-lister seam so tests never iterate the live process table — or is the timeout accepted as environmental with the per-file runner canonized? (Harness task #50 context.)
  - **R-d (F2):** is the real TCP connect intended integration behaviour (then mark/skip it offline, typed) or a defect (then fake the socket)?
  - **R-e (F3):** invest in cross-file pollution isolation, or canonize the per-file runner as THE documented runner (script + doc + the checkpoint rule in §4 referencing it)?
- **Gates (for whichever arms are ruled code-side):**
  - F1 arm: `profile-bound pid discovery reads the injected lister` — fake lister returns a driven pid set (two distinct sets); asserts the returned pids equal the driven set and the real process table is untouched (recorder seam). **Killing mutation:** bypass the seam and call the real enumerator — the test-owned recorder shows zero reads of the fake while the assertion needs its driven values.
  - F2 arm: `sanitization test completes with zero real connects` — monkeypatched socket layer records connect attempts; assert count == 0 with the test still exercising its sanitization claims. **Killing mutation:** restore the real connect — the recorder count convicts it.
  - F3 arm (if runner-canonization): the gate is procedural, stated honestly — the runner script exists, the verification section (§4) names it, and a red is defined as "any file red under the canonical runner". No vacuous pytest-shaped gate is faked for a process decision.
- **Blast radius:** test infra + possibly one seam in `profiles.py`. F5 (the leaked `hermes gateway run` child) is an operator kill, not code — listed NOT STAGED.
- **Does NOT buy:** green-ness of any currently-failing test's substance; launcher-side infra (F6 is a procedure, §4).

---

#### ML-9 (S-h) — launcher: Scene Health stops printing a value it structurally cannot vary, and every probe line names its authority — **S/M** — **must serialize after WV-L6**

- **Defect class retired:** a diagnostic surface wired to a source that cannot show the fact it reports (A9) — the same shape as C13, whose receipt half lands in ML-3; this stage carries the rule.
- **Files/symbols (verified at `fd70e8fb2`; office/ is in WV-L6 flux — re-anchor on symbols at dispatch):** `mission_office_host.dart:166-170` (host-resolved render model), `:1288` (Scene Health fed the host's model); `mission_office_scene_health.dart:32` (`MissionOfficeRenderProbe.fromModel(widget.model)`), `:170` (the `mode` line); `mission_office_render_probe.dart:12,46,69`; `mission_office_game.dart:244` (the game's own `_resolved`, the only place interaction truth lives).
- **Operator ruling R-f — two arms:**
  - **(a) feed the truth:** the game exposes its resolved model (or just its interaction mode) to Scene Health — a listenable/callback seam from `MissionOfficeGame` — and the probe's `mode` reads it.
  - **(b) drop the line:** delete the `mode` probe line AND `interactionMode` from `MissionOfficeRenderProbe` — which, as a bonus, makes C14's corrected U-1 claim TRUE repo-wide (the probe/scene-health readers were exactly what falsified it).
  - Default proposal: **(b)** — cheapest, honest, and consistent with the gesture plan's direction; but the ruling must consider whether UP-series work wants a live mode diagnostic, which only (a) provides.
- **Gate (arm a):** `the mode line reports the game's interaction state` — drive the game to `draggingNode` via its own seam; assert the probe renders `draggingNode`, then back to `idle` (two driven values). **Killing mutation:** revert the probe to the host-resolved model — it can only ever produce `idle`, failing the `draggingNode` drive. **Gate (arm b):** a tombstone row for the probe's `interactionMode` field (form: name, scoped to office/), killing mutation: reintroduce the field — the registry scanner reds.
- **Rule half (either arm):** Scene Health's header doc gains an authority column — every `_ProbeLine` names the model/owner it reads — and the plan-level rule is recorded beside it: *a diagnostic line names the authority it reads, and a field-evidence gate names the receipt that could satisfy it, or the line/gate is deleted.*
- **Blast radius:** `office/` — hard serialization behind WV-L6; re-read fence hashes at dispatch (G3). A10 (stale `deskCenter` during desk echo) stays a site note — no runtime effect, verified reasoning is at the site.
- **Does NOT buy:** C13's receipt (ML-3's); any gesture-prediction behaviour (UP plans own that); Scene Health's other lines' content.

---

#### ML-10 (S-a, observability subset) — hermes: the fingerprint core's refusals and non-convergence become receipts — **M**

- **Defect class retired:** a disabled cache core that announces itself only as a log WARNING (A4), and a cache that can silently buy nothing forever (A2 — the implementer's own named next fix). A5 is re-stated, not changed.
- **Files/symbols (verified):** `agent_runtime/core_cache.py` — bounds `MAX_FINGERPRINT_ENTRIES`/`MAX_SKILL_ENTRIES_PER_ROOT` (`:147`, `:152`); bound-exceeded WARNINGs (`:386-388`, `:464-466`); the symlink/bound doctrine (`:284-291`); the convergence facts (`:585-592` — the build mutates its own inputs via `PersonaInstanceStore.ensure_for_personas`, cold store converges in 2 builds, measured 3 on a virgin root per LEDGER); the shadow-receipt machinery (`:1036-1075`).
- **Target shape:**
  1. **A4:** bound-exceeded refusal emits a structured receipt (`snapshot_core_cache fingerprint_refused reason=entries_exceeded root=… bound=…`) on the same receipts channel the shadow lane uses — countable by census, not just greppable prose. Refusal semantics untouched: reaching the bound still means never-cache.
  2. **A2:** the cache tracks consecutive key-mismatch builds within a process/boot; past a small bound (proposal: 3 — the measured virgin-root convergence) it emits `snapshot_core_cache never_converged builds=N diff=<first differing entries>` naming the oscillating input paths from the stat-set diff — the diagnostic that turns "the cache silently buys nothing" into a named input to widen the closure on (the A1 rule: widen the stat set, never trust the cache harder).
- **Gates:**
  - `an entry-bound refusal is receipted, not just warned` — fixture store driven past a test-lowered bound; assert exactly one receipt carrying the driven root (two roots across two fixtures). **Killing mutation:** restore the bare `logger.warning` — no receipt exists to carry the driven root.
  - `an input that oscillates every build is named` — inject an input the build itself rewrites per pass (the `ensure_for_personas` shape, simulated); after the bound, assert the receipt names the oscillating path, driven with two distinct paths. **Killing mutation:** drop the diff-naming — the receipt cannot name a path the mutant never computes; drop the emission — no receipt at all. (C16's lesson applies: the no-diff arm must carry a typed `diff_unavailable`, never silently reuse another arm's sentence.)
- **Blast radius:** `core_cache.py` + its test file. No fingerprint semantics change; cache hits/misses byte-identical.
- **Does NOT buy:** A1/A6's closure audit and A3's dirty-tree separation (**field-gated on the shadow-validation window** — NOT STAGED, §3); E1's ~2s boot measurement (operator runtime); symlink loop *detection* (A5's bound-refusal doctrine is kept deliberately, `:284-291` says why).

---

#### ML-8 (S-b) — hermes: silently-skipping listers as a defect class — the flagship — **L**, two parts

- **Defect class retired:** an authority deciding on a lister that silently skips unreadable rows — B2's class, which recurred once already (the `class_key_collision` fence) and is verified live in three more places (§0.2). EG-1.5's `scan_actors` + typed refusal is the house pattern; `ActorScan`'s own docstring states the law: *the two facts have to travel together; any seam that carries only the list re-opens the hole at that seam* (`office_store.py:70-72`).
- **ML-8a — the census (read-only, RD-0 pattern).** Classify every `except Exception: continue` lister in `agent_runtime` (verified inventory: `events.py:506`, `mission_chat_turns.py:1267`, `board_sync.py:149`, `board_store.py:126/:512/:524`, `office_sync.py:130`, `persona_chat_continuity.py:1798`, `persona_assignments.py:1111/:1761/:1912`, `dispatch_store.py:993/:1002`, `prompt_observability.py:1517`, `realm_sync.py:999/:1040`, `runtime_instances.py:44`, `serde.py:63`, `snapshot.py:1636`) into: **(i) authority-input** — a gate/writer/sync decision reads the shortened answer → refuse typed; **(ii) projection** — a reader renders it → the count travels (ActorScan pattern); **(iii) benign** — documented why, at the site. Output: the table, in this doc, no code. C14's rule applies to every claim in it: repo-scoped or nothing.
- **ML-8b — the pre-verified authority fixes** (the census may add more; these three are already evidenced):
  1. **`office_sync.py:83` / `:210`** — realm publish/compare iterating `list_actors` (the thin view). A store with one undecodable actor file publishes/compares a world that silently lacks it — a **writer** deciding on partial knowledge, the exact B2 shape. Fix: read `scan_actors`; `unreadable > 0` on a workspace → that workspace's sync arm refuses typed (`sync_unknowable`, reason carrying the count), never publishes or converges partial.
  2. **`office_sync.py:120-131`** — pulled remote actor files skipped with `continue` inside the pull read. A skipped remote actor is indistinguishable from "the peer deleted it" to any downstream compare. Fix: count travels on the pull summary (`unreadable_remote: N`) and a nonzero count fences delete-shaped decisions for that pull, typed. **Constraint, non-negotiable:** the B1 carve-out does not move — `test_the_carve_out_is_a_live_hole_and_not_a_stale_note` (`tests/agent_runtime/test_office_class_key_one_fence.py:552`, verified) must stay green byte-unchanged; touching `apply_office_pull`'s class-key behaviour is task #33's ruling, not this stage's.
  3. **`snapshot.py:1633-1637`** — a workspace whose `get_surface` throws vanishes from the snapshot's offices silently. Projection class: the core carries `offices_unreadable: N` (additive; old launchers ignore it), mirroring the `scan_actors` chokepoint comment four lines down.
  - `board_store.py` sites: fixed per their census classification (the boards list feeds the board UI and any write guard that consults it — expected class (ii) at minimum).
- **Gates (one per fixed site, each with its mutation):**
  - `a workspace with an unreadable actor file refuses realm publish typed` — fixture corrupts one actor JSON; assert the typed reason carries count 1 (then 2, two fixtures) AND the store recorder shows zero publish writes for that workspace. **Killing mutation:** swap back to `list_actors` — the mutant publishes (recorder convicts) and cannot mint the reason.
  - `an unreadable pulled actor cannot read as a peer delete` — corrupt one pulled file; assert `unreadable_remote == 1` on the summary AND no delete-shaped decision for that key (decision recorder). **Killing mutation:** restore the `continue` — the summary lacks the count while the fixture drives it, and the delete-fence probe reds.
  - `a workspace with an unreadable surface is counted in the core, not vanished` — assert `offices_unreadable` equals the driven count (two values) and the readable workspaces still list. **Killing mutation:** restore the silent `continue` — constant-zero cannot match two driven counts.
- **Blast radius:** realm sync behaviour on stores with corrupt files changes from quiet-partial to typed refusal — operator-visible, named in the commit; office wire additive only. Hermes-side throughout: **interleaves freely with WV-L6.**
- **Does NOT buy:** B1's ruling (the carve-out stays a documented live hole — its terminal state is legitimate per the promotion rule); RD-H4's launcher rendering of new counts (follow-on); the launcher-side silent-skip class (RD-L5 owns it in the RD plan).

---

### Priority 4 — PERFORMANCE

---

#### ML-11 (S-f) — launcher: the hub lane routes without building the object graph — **M**

- **Defect class retired:** the billed double decode (A7) — a hub-delivered full core decoded once on the UI isolate purely to route it, and again off-isolate by the intake. The bill, verbatim, is at `mission_control_serve_session_io.dart:1074-1083` (READ): *"the cure is a routing discriminator that does not require the graph, or Plan B/D3 shrinking the demoted frames that make it matter."* This stage is the first cure; frame-shrinking stays hermes/Plan-B's.
- **Files/symbols (verified):** `lib/features/mission_control/data/mission_control_serve_session_io.dart` — the hub line router inside the stream-lane path under `subscribeStreamLane` (`:1106+`); the intake seam it feeds (bridge-side, off-isolate decode) untouched.
- **Target shape:** extract a pure `routeHubLine(String) -> <lane/kind>` that discriminates on a cheap string probe of the frame's envelope (the discriminator key near the head of the line), never `jsonDecode`; the UI-isolate path calls it; the full decode remains exactly where it is today — off-isolate in the shared intake, byte-for-byte the same downstream work (the property the current comment guarantees and this stage must preserve). The billed comment is rewritten to record the cure and its date instead of the bill.
- **Gate:** `the router classifies a frame whose body the UI isolate could not decode` — feed lines whose envelope is valid for the probe but whose deep payload is deliberately malformed (truncated tail past the discriminator); assert correct kind per driven frame (hydrate/patch/heartbeat — three driven kinds), no throw. **Killing mutation:** reinstate `jsonDecode`-based routing — the mutant throws (or misroutes to an error arm) on the malformed-tail fixtures; it cannot classify a graph it cannot build. Witnesses assert kinds and counts, never elapsed-ms, exactly as the site's own comment demands.
- **Second witness, pre-existing, byte-unchanged:** the EG-4.1/TC-1 frame-parity and lane pins (hub cadence fork, `restart_producer` rejoin pins — C4's survivors-turned-pins) — proving the routing change moved no downstream behaviour.
- **Blast radius:** `data/` session io; bridge intake untouched. **Sequencing per the ledger: lands with or just after the Class-B reaps** — confirm at dispatch that TC-4/Class-B state has not changed the lane's ownership. Launcher stage: serialize with WV-L6 and all other launcher stages.
- **Does NOT buy:** demoted-frame shrinking (Plan B/D3); D4's byte-parity posture (deliberately kept — parity is a claim about the frame, not the line); any measured ms claim.

---

## 2. Dependency and collision map

| Stage | Repo | Touches launcher `office/` (WV-L6 serialization) | Can interleave with WV-L6 | Notes |
|---|---|---|---|---|
| ML-1 | hermes | no | yes | test-only |
| ML-2 | docs both | no | yes — anytime | docs-only |
| ML-3 | launcher | no (`data/` + tests) | file-level no overlap, but **one launcher implementer at a time** — queue behind WV-L6 or any running launcher stage | |
| ML-5 | launcher | no (one test file) | same serialization rule | unblocks registry enforcement |
| ML-4 | hermes | no | yes | five test dirs + gate |
| ML-6 | launcher | no (`data/`) | same serialization rule | sweep old-wording pins first |
| ML-7 | hermes | no | yes | 3 operator rulings first |
| ML-9 | launcher | **YES** — `office/` | **hard-serialize after WV-L6 lands; re-read fence hashes (G3)** | ruling R-f first |
| ML-10 | hermes | no | yes | |
| ML-8 | hermes | no | yes | B1 carve-out fence must not move |
| ML-11 | launcher | no (`data/` session io) | same serialization rule; confirm Class-B reap state at dispatch | |

Recommended dispatch interleave: hermes lane ML-1 → ML-4 → ML-7 → ML-10 → ML-8 running beside launcher lane (after WV-L6) ML-3 → ML-5 → ML-6 → ML-9 → ML-11; ML-2 anytime.

---

## 3. NOT STAGED — the cut line, explicitly

**Waiting on an operator ruling (promotion rule clause 3):**
- **B1** — `apply_office_pull` past the class-key fence: waits on task #33. Its live-hole test (`test_office_class_key_one_fence.py:552`, verified present) is the legitimate terminal state; ML-8b names it as an immovable fence.
- **B7** — #59's ranking half: EG-5.5's decision, taken with `office_hold_mask` receipts in hand.
- **B4** — `office.surface.created` correlation: judged authoring, revisit only if a folder-creation trace is ever needed.

**Field-gated / needs the operator's runtime (promotion rule clause 3; E-rows verbatim):**
- **A1/A6 closure audit, A3** — gated on the shadow-validation window's receipts (ML-10 lands the receipts that feed it).
- **E1** (~2s cache-hit boot, unmeasured), **E2** (per-phase first live acceptance), **E3** (EG-1.5 + EG-5.1 end-to-end on a real store), **E4** (`ArchiveUnreadable` reachable only by store-boundary injection — says so out loud, stays), **E5** (warm serve child picking up a Settings login — PROVIDER_LOGIN §10 ASSUMPTION), **E6** (B-3 Stage C screenshot owed; PL-4 OAuth first wrapped flow).
- **C9, C10, C11** — field-decidable or uncapturable; recorded as gaps, not faked.

**Closed — no work, rulings stand (D-rows):** D1 (fourth `writesInFlight` term refused), D2 (dedicated witness), D3 (drain witnessed via refused+superseded), D4 (byte parity is about the frame), D5 (`coalesces:` split only when a caller motivates it), D6 ("one write per changed actor", stated), D7 (unreachable guard kept as named defence). **A8** (store default flip — production-preserving, landed), **A10** (no runtime effect, noted at site), **C1-C5, C8, C12's incident itself, C14's incident, C16** — closed; their lessons are load-bearing in this plan's gate specs (witness diversity per C5/C16; inject below the seam per C1; two-driven-values per C2).
- **F4's education half** — handed to the education lane (task #41) by ML-2; ML-5 fixes only the registry's own row. **F5** — operator `taskkill`, not code (killed 2026-08-17 this session). **F6** — a procedure, adopted in §4, not a stage. **G1-G5** — operating discipline, §4.

---

## 4. Verification — the run's process rules, adopted

1. **Full suite at every checkpoint (G1).** Hermes: the full suite under the canonical runner — until ML-7's R-e ruling lands, that is the **per-file runner** for `tests/hermes_cli` (F3 is a fact about the interpreter, not the tests), full directories elsewhere; three regressions reached main last run on targeted runs alone. Launcher: full `flutter test` of the owned feature directory at minimum, full suite at merge checkpoints; C16 was caught by the FULL-DIR run and by nothing else — that is this rule earning its keep on the launcher side.
2. **Quote plan exceptions verbatim into briefs (G2).** A brief written stricter than the plan stopped a stage mid-run. Every ML brief that touches a fence quotes the fence's exception text as written, never paraphrased tighter.
3. **Fence hashes are read at dispatch time (G3), never inherited from the brief.** WV-L6 is changing `office/` while this plan is being read; ML-9 and every launcher stage re-hash at dispatch. Stage entries above are complete except fence hashes and pubspec hashes, which the dispatcher appends.
4. **Merged combinations are untested until the seam is run (G5).** After every merge: the merged-seam run, both repos if the merge crossed the wire contract. G1 is how the exposure arrived even with per-branch green.
5. **Launcher tree procedure (F6):** the not-mine dirty pubspec downgrade breaks widget-test compilation; every launcher implementer runs the hash-verified backup → checkout → `pub get` → `test --no-pub` → byte-exact restore procedure, and commits with explicit paths so the pubspec never rides along.
6. **Witness law (task #60, RD preamble):** witnesses assert counts, ordering, and typed reasons — never elapsed-ms; every gate above carries one killing mutation and states why the mutant cannot also satisfy the probe; two-driven-values on any probed field a constant could fake.
7. **Permission boundaries:** no writes under `X:/Eternia/.hermes/` ever (live root is read-only evidence); `dart format` forbidden; operator-blocked operations (G4's class) are escalated, not retried.

---

## 4.5 amendment (ML-2) — F6 covers `flutter analyze`, not just tests

**The rule.** §4 item 5's F6 launcher-tree procedure applies to **`flutter analyze` as well as
`flutter test`**. Read every "widget-test compilation" in that item as "any command that resolves
the package graph".

**Why it had to be said.** F6 was written as a *test* procedure, so an implementer reasonably
reads `flutter analyze` as exempt — it is a static check, it runs in seconds, it looks like the
cheap signal you are allowed to reach for mid-implementation. It is not exempt. The not-mine dirty
`pubspec.yaml` downgrades the analyzer pin (`analyzer: ^13.0.0` → `^9.0.0`, alongside
`flutter_riverpod ^3.4.2` → `^3.2.1` and `riverpod_annotation ^4.0.6` → `^4.0.2`; READ
`git diff -- pubspec.yaml` in the launcher, 2026-08-17), and `flutter analyze` performs version
solving **before it emits its first lint**. So it fails in the resolver, not the analyzer: the
implementer gets a dependency-resolution error where they expected either lints or silence, and
nothing in that error names the dirty pubspec as the cause. **Found by ML-5** while running the
cheap signal the implementation discipline explicitly encourages — which is exactly why this
belongs in §4 and not in one stage's notes: the discipline sends every implementer straight into
it.

**How to apply.** Any launcher stage that runs `flutter analyze` runs it *inside* the same
hash-verified backup → checkout → `pub get` → run → byte-exact restore window as the tests, and
still commits with explicit paths so the pubspec never rides along. Analyze and test share one
window; do not open two. A resolution failure from either command is a signal to check the
pubspec's dirty state first, before believing anything it says about the code.

**R-a (doc-drift test) — DECLINED.** ML-2's optional hardening was a test asserting these
documents do not drift from the tree; not taken, because a doc-drift gate keyed to counts and
line numbers rots faster than the prose it guards and would have to be maintained by the same
sweep it replaces — the corrections above instead name the *authority* (the `@method` registry,
a repo-wide grep) so the documents stop carrying copies that can drift at all. Recorded as
declined rather than silently skipped; revisit only if a third register-drift sweep is commissioned.

---

## ML-8a census — every silently-skipping lister in `agent_runtime`

Read-only pass (RD-0 pattern), executed 2026-08-17 against hermes `671c8d2c84`.
Every `file:line` in the tables below is **at that base SHA**; where ML-8b then
moved a site, the post-stage line is given in parentheses beside it.
**Method, and the correction it forced:** the plan's §1 inventory was re-derived
by walking every `.py` under `agent_runtime/` with an AST-shaped scan for a
handler whose body begins `continue` (bare `except Exception:` **and** the
narrower spellings the inventory did not look for). That found **71 sites**
against the inventory's 19. Restricted to bare `except Exception:` — the
inventory's own scope — it found **26**, so the verified inventory was **short
by seven**: `board_sync.py:262`, `office_sync.py:263`, `office_store.py:1008`,
`persona_assignments.py:1999`, `dispatch_delivery.py:1089`, `flow_graph.py:506`,
`snapshot.py:2037`. Four of those seven turn out to be the model answer (they
already count what they skip), which is why they matter: they are the shape the
class-(i) and class-(ii) rows below are being moved toward.

C14's rule applies to every claim in this table: each consumer named was read at
this SHA, and the classification is a claim about a READER, not about the
`continue` itself. A lister is only class (i) because something DECIDES on its
answer.

**The three classes.** (i) **authority-input** — a gate, writer, or sync arm
reads the shortened answer, so the fix is a typed refusal. (ii) **projection** —
a reader renders it, so the fix is that the count TRAVELS (the `ActorScan`
pattern). (iii) **benign** — documented why, at the site.

### Class (i) — authority-input

| Site | Symbol | The authority that decides on the short answer | Status |
|---|---|---|---|
| `office_sync.py:83` (now `:161`) | `update_office_baseline_after_sync` | The publish-side baseline writer. A missing actor's hash records the row as never-published, and the next pull reads that absence as a peer delete. | **FIXED** — `sync_unknowable`, per workspace |
| `office_sync.py:210` (now `:343`) | `apply_office_pull` local read | The 3-way classifier: an unreadable local actor arrives as `local_hash=None`, i.e. a local delete. Mutant proof: it reports `adopted: 1`, having written the remote copy over the file it could not read. | **FIXED** — same refusal |
| `realm_sync.py:999`, `:1040`, `:1044` | `_office_artifacts` / `_office_wanted_persona_ids` | Publish scope. Two independent walks of the same directories decided *which offices travel* and *which persona definitions are pinned*; publish copies actor FILES verbatim, so an undecodable one travelled and every peer archived that desk. | **FIXED** — one `_office_publish_scan`, typed refusal |
| `office_sync.py:130` (now `:245`) | `_read_remote_office` | The pull's delete signal IS a key's absence from this dict. | **FIXED** — `unreadable_remote` + per-workspace delete fence |
| `board_sync.py:149` | `_read_remote_board` | Exact twin of the row above, in the module the office lane was lifted from. | **FIXED** — same shape |
| `board_store.py:512` | `_list_active_cards` → `_next_order_key` / `_allocate_order_key` / `_rebalance_column` | Order-key ALLOCATION. The neighbour keys an insert brackets between, and the keys a rebalance rewrites wholesale, were computed from a column missing whatever would not decode — so the allocator places on top of the invisible card. Corruption written, not merely read. | **FIXED** — one `_ordering_cards` chokepoint, `CardsUnreadable` |
| `board_store.py:126` | `list_all` → `realm_sync._board_artifacts` | Publish scope, the board twin of the office row. Also `store.py:351`'s workspace-delete cascade, which skips the directory it cannot name (an orphan board survives the delete). | **FIXED** for publish (`_board_publish_scan`); the delete cascade is NOT — see follow-ups |
| `persona_assignments.py:1761` | `PersonaInstanceStore.list_all` → `:604` steering repair | `live_ids` is computed from the short list, and every child edge pointing at a row that would not decode is **stripped as dangling**. A delete-shaped write derived from a parse error. | **NOT FIXED — stated cut** |
| `persona_assignments.py:1761` | `…list_all` → `:1743` `_session_owned_by_other_instance` | A uniqueness guard answering "is this session already owned?" from a short list answers **no**, and a second binding lands — the class-key fence's blind spot, one subsystem over. | **NOT FIXED — stated cut** |
| `persona_assignments.py:1761` | `…list_all` → `:741`, `:812` | Chat-binding repair and parent-backlink release; both write on the short answer. | **NOT FIXED — stated cut** |
| `persona_assignments.py:1912` | `PersonaAssignmentStore.list_all` → `:1293` `_active_assignment_ids_for_instance` | The RETIRE guard, whose own docstring says "a retire must never orphan a live assignment". An undecodable assignment file answers "none active" and the retire proceeds. Doubly fail-open: `:1293` also wraps the call in `except Exception: return []`. | **NOT FIXED — stated cut** |
| `persona_assignments.py:1111` | `_validate_no_steering_cycle` | The DAG walk stops at a node it cannot read, so a cycle BEHIND that node is not detected and the edge is admitted. | **NOT FIXED — stated cut** |
| `persona_chat_continuity.py:1798` | `_iter_records` → `_rebuild_index` (`:1365`), `_scan_open_ticket_for_session` (`:1679`) | The index rebuild drops the ticket permanently; the open-ticket guard answers "none open" and a second clarify ticket is minted. | **NOT FIXED — stated cut** |

**The cut, stated.** Everything above the persona rows is the office/board sync
and projection lane ML-8b names, and is fixed. The `persona_assignments` /
`persona_chat_continuity` rows are class (i) on the same evidence standard, and
they are NOT in this stage's commits. Two reasons, both boundary-shaped rather
than budget-shaped: (1) each of them changes operator-visible behaviour in a
different subsystem — a retire that starts refusing, a steering repair that
starts holding — which is a ruling about the persona lane, not about sync;
(2) the steering-repair row is a **delete-shaped write** and deserves the same
fence-plus-discriminator pair the office arm got, not a bolted-on guard. Filed
as the top escalation out of this stage. `store.py:351` (workspace-delete
cascade over `list_all`) is the same call: now that `scan_all` exists the fix is
small, but "what should deleting a workspace do when a board file will not
decode" is a ruling.

### Class (ii) — projection: the count must travel

| Site | Symbol | What renders it | Status |
|---|---|---|---|
| `snapshot.py:1636` | `_offices_summary` | The core's `offices` map; a whole workspace vanished. | **FIXED** — `offices_unreadable` (additive) |
| `board_store.py:126` | `list_all` → `_boards_summary` | The core's `boards` map; a whole board vanished. | **FIXED** — `boards_unreadable` (additive) |
| `board_store.py:512`/`:524` | `list_cards` → `board_summary_row` | Each board row's card list, which computed `cards_truncated` from the already-shortened length and answered 0 — the exact defect `actors_unreadable` fixed one seam over. | **FIXED** — `cards_unreadable`, required by keyword |
| `events.py:506` | `_archived_event_slices` → `event_log_health` | `archived_event_slices`, `archived_event_rows`, and the derived `index_health` verdict. An unreadable manifest under-reports the archive and can flip `index_health`. | **NOT FIXED — follow-up** |
| `runtime_instances.py:44` | `GoalRuntimeInstanceStore.list_all` → `status.py:58` lanes | The status wire's `runtime_instances` block. | **NOT FIXED — follow-up** |
| `prompt_observability.py:1517` | `load_latest_prompt_observability_contexts` | Four readers (`:1090`, `:1420`, `:1473`, `:1627`). Notably the same FILE already counts unreadability correctly at `:1284` (`unreadable += 1`) and `:1461` (`index_misses += 1`) — the loader is the one arm in the family that does not. | **NOT FIXED — follow-up** |
| `snapshot.py:2037` | `_event_summary_warnings` | A WARNING generator failing closed to "no warning": `event_summary_missing` throwing yields `missing=False`, so the parity warning is suppressed by the error it should report. | **NOT FIXED — follow-up** |
| `flow_graph.py:506` | `reconcile_departed_agents` | The row is stamped `ok=True, changed=False, steered_by=[]` — reported as a completed strip — when the instance merely would not read. No write is taken (fail-safe), but the REPORT is wrong; it wants an `unresolvable` reason rather than the success arm's sentence (C16). | **NOT FIXED — follow-up** |

Cut for class (ii): five follow-ups, each one field on one row, none in the sync
lane this stage owns and none delete-shaped. Listing them rather than landing
them keeps ML-8's commits revertable per site.

### Class (iii) — benign, and the four that are already the model

| Site | Why it is benign | At-site documentation |
|---|---|---|
| `dispatch_store.py:993`, `:1002` | The `continue` means "not disproof of ownership", which is the correct conservative answer for a PID probe that failed. | **Yes** — `# Unreadable probe, or a genuine match — either way not disproof.` |
| `serde.py:63` | Not a lister: the `continue` IS the union-coercion dispatch, trying the next member type. | **No** — reads like a swallow; wants one line saying it is control flow |
| `mission_chat_turns.py:1267` | Inbound payload sanitation, not a file read: an element with no integer `seq` cannot be ordered, so it has no position to occupy. | **No** — wants one line |
| `realm_sync.py:1074` (now `:1203`) | `_published_profile_file_hashes`: a missing hash makes the pull HOLD that file rather than adopt it. Fails toward the operator, not toward a delete. | **No** — wants one line |
| `office_store.py:1008` | **Model answer.** `failed += 1`, and the count is returned. | Yes |
| `persona_assignments.py:1999` | **Model answer.** `held.append({... "reason": f"unreadable:{type(exc).__name__}"})`. | Yes |
| `dispatch_delivery.py:1089` | **Model answer.** `_telemetry.record_bounce(event_key, "unclaimed", repr(exc))`. | Yes |
| `prompt_observability.py:1284`, `:1461` | **Model answer.** `unreadable += 1` / `receipt["index_misses"] += 1`. | Yes |

### Out of class, found on the way

`office_sync.py:263` and `board_sync.py:262` are not silently-skipping listers —
they are swallowed WRITE failures: `remove_actor` / `archive_card` raising is
caught with `pass`, the summary's `archived` is not incremented, and then the
baseline row is popped anyway. So a failed archive is recorded as though it
succeeded, and the next pull sees convergence. Different defect class, real,
unfixed, recorded here so the next reader does not have to re-find it. (ML-8b's
delete fence sits ABOVE both arms, so the fenced path no longer reaches them —
which narrows the exposure without closing it.)
