# Field notes — instant workspace switching, lane A (WS1 hermes half), 2026-09-01

Running record, written as the work happened. Authority is the launcher's
`docs/mission_control/archive/instant-workspace-switching.md`; this file records
where the ground disagreed with it and what was done about it.

Baseline: hermes `c894c2b159`, branch `ws12-scope-entity`, worktree
`X:/Eternia/wt-ws12-hermes` (the primary checkout is shared with a concurrent
session and was never written).

---

## 1. Plan claims re-measured at the baseline

| plan claim | verdict at `c894c2b159` |
|---|---|
| `workspace.activated` / `realm.activated` absent from `LIVE_COVERED_DOMAIN_EVENT_TYPES` | **TRUE** |
| the two activate events are emitted only by `store.py`'s two `set_active` writes | **TRUE** — `agent_runtime/store.py:260,262` (workspace) and `:885,:887` (realm); no other producer |
| `snapshot.py` reads `active_id()` in exactly SEVEN places at `:796,:800,:887,:927,:932,:964,:965` | **TRUE, verbatim** — the grep reproduces those seven line numbers exactly |
| all seven are pure functions of the two pointers plus rows the client already holds | **TRUE of the readers, FALSE of the core** — see §2 |
| the producer emits op `replace` | **FALSE** — see §3 |
| "the hub: nothing" — negotiation + per-subscriber promotion already route it | **TRUE**, and the empty-frame hazard is already fenced (see §4) |
| a new fold entity is a cross-stack stream-golden landing | **TRUE**, and it cost more than budgeted — see §5 |

---

## 2. The derivability audit is not clean, and the plan's §1.1 overstates it

Plan §1.1: *"All seven are pure functions of the two pointers plus data the
client already holds … nothing else in the core varies with the pointer."*

The first half is true. **The second half is false.** Three of the seven readers
are INDIRECT — their values do not reach the wire as a pointer or as a per-row
`active` flag, they reach it inside another section:

- `:796` / `:800` resolve `active_workspace_name` / `active_realm_name`, which
  are passed to `snapshot_prompt_observability(realm=…, workspace=…)` and land
  in every chat context's `situational_hud`.
- `:887` passes `active_workspace_id=` into the same function, where
  `workspace_scope.effective_workspace_id` uses it to scope the addressable
  ("On level") roster.

So a client that folds a `scope` patch holds a `prompt_observability` section
whose recorded scope strings are one switch stale until any core arrives. The
demoted core would have said something the patch does not.

**Measured narrowing, before accepting it.** `effective_workspace_id` takes the
INSTANCE's own `workspace_id` first and falls back to the active pointer only
for a runtime-global instance (`agent_runtime/workspace_scope.py:75-84`), so
every placed instance's HUD is unaffected — the residue is the canonical
plumbing rows only. And the launcher's only reader of the section is the
injected-context JSON diagnostic dialog
(`lib/features/mission_control/agent_chat/injected_context_json_dialog.dart`).

**Disposition: accepted, named, and pinned rather than waved through.** The
section is per-turn telemetry regenerated on every chat turn — it RECORDS what a
prompt contained; the next turn re-resolves the HUD live through its own
wrapper, so this is not a value anything acts on. The residue is written into
`patch_coverage.LIVE_COVERED_DOMAIN_EVENT_TYPES`'s entry comment and enumerated
as `_INDIRECT_READERS` in `tests/agent_runtime/test_scope_patch_coverage.py`, so
the census's failure message tells an eighth-reader author which of the two
questions they are answering.

This is the plan row that was right that something needed auditing and wrong
about what the audit would find.

---

## 3. There is no `replace` op — WS1 ships `upsert`

Plan §2/WS1: *"a `state.patched` frame, entity `scope`, op `replace`"*.

`FOLDABLE_PATCH_OPS` is `{upsert, remove}` (`state_patches.py:102`). An op
`replace` is not foldable, so the patch would have demoted every batch it rode
in — the exact opposite of the stage's purpose, and it would have been silent
(the frame would still be produced; it would just never be promoted).

What `replace` names is the SEMANTICS, and they survive without a new op: the
row always carries BOTH keys (`SCOPE_PATCH_FIELDS`), so a merge of every field
of a two-field row *is* a replace. Built as `upsert`; the correction is written
at `emit_scope_patch`'s docstring so a reader looking for the plan's word finds
the answer where they look.

---

## 4. Two hazards that turned out to be already fenced

**(a) The lone covered event.** Covering a domain event that is not entity-gated
would let a batch of just `workspace.activated` promote for a declaring client
and ship a `patch` frame with an EMPTY `patches` list — a watermark advanced
having folded nothing. Two fences, and both already existed:

- `stream.py:905` refuses to promote any batch where
  `batch_carries_patch_rows(batch)` is false — structural, and it predates this
  lane;
- both events are added to `TOKEN_GATED_DOMAIN_EVENT_TYPES` anyway (belt and
  braces, the `office.surface.updated` precedent).

**(b) A fourth capability token.** The three existing tokens exist because their
events pair with a WIDENED OP on an entity a fielded client already declares —
a distinction the per-entity vocabulary cannot make. `scope` has no such
problem: brand-new entity, one op, so "can you fold the row" and "may the event
free-ride" are the same question. The two events therefore take the ENTITY NAME
as their gate token; no fourth string was minted. (R-W0: no second thing to get
wrong for no compatibility gained.)

**(c) Emit failure.** `_emit_active_scope_patch` is best-effort, matching
`_append_store_event` beside it — a broken event log must not fail an
activation. It is safe to swallow precisely because of fence (a): a batch with
no `state.patched` demotes rather than shipping an empty promotion.

---

## 5. Defect found by the ceremony: seven goldens were stale, and the pin could not see it

Regenerating `tests/fixtures/stream_frames/` moved **seven generated goldens**
that WS1 does not touch. The moved value in each is
`core.decision_contract_hash`: the committed bytes carry
`b941daedb22a161e4e54fea9911d9b448693d48318ee9c2b4704ff387b4f7165` while the
live event registry has produced
`b6985ac44ceee82e24d87dfc3e4d069371fac47f902d41060908f6918361baaa` since some
earlier landing that regenerated nothing. Confirmed pre-existing by computing
`contract_hash()` in the untouched primary checkout at the baseline SHA — same
`b6985ac…`. Nothing in this lane touches `decision_contract_registry.py`.

**Why nothing was red.** `test_stream_contract_fixture.py` compares each golden
against `MANIFEST.sha256`, and both sides of that comparison were equally stale.
The gate that WOULD have caught it is the launcher's
`tool/test_quality/check_producer_contracts.py`, whose default mode runs this
repo's generators — it is not on either repo's per-commit path.

**Disposition:** regenerated in this landing, launcher mirror moved with it, and
the reason is written into `tests/fixtures/stream_frames/README.md` so the next
reader of a seven-file hash move knows it was one defect and not seven. This is
the same "both repos green while they disagree" shape that README's notes exist
to prevent, one level in — the byte pin is only as honest as the last
regeneration.

**Launcher-side state at baseline:** `test/fixtures/harness_stream/MANIFEST.sha256`
was byte-identical to the hermes manifest, so the README's open
"CROSS-STACK COPY STATUS (AX2) — launcher mirror OWED" note is STALE: that
mirror had already landed. Not edited (it is a historical note), but recorded
here.

---

## 6. What landed (hermes half)

- `agent_runtime/state_patches.py` — `SCOPE_ENTITY` / `SCOPE_PATCH_ID` /
  `SCOPE_PATCH_FIELDS` and `emit_scope_patch` (both pointers, always; `None` on
  the wire as `null`, never an absent key, so a `--clear` is expressible).
- `agent_runtime/store.py` — `_emit_active_scope_patch`, called from BOTH
  `set_active` writes, patch-before-event, after the pointer file lands and only
  on the `apply` arm (a superseded/duplicate intent emits nothing).
- `agent_runtime/patch_coverage.py` — the two events into
  `LIVE_COVERED_DOMAIN_EVENT_TYPES` with the derivability audit written at the
  entry, plus both into `TOKEN_GATED_DOMAIN_EVENT_TYPES` keyed on `SCOPE_ENTITY`.
- `tests/agent_runtime/test_scope_patch_coverage.py` — the reader census, the
  coverage predicate in both directions, the required-tokens equivalence over
  the new vocabulary, the null-pointer wire shape, the two-subscriber split
  (resolver + real `StreamHub`), the producer, the refusal arms, and the golden.
- `tests/fixtures/stream_frames/patch_scope.json` (new) +
  `patch_coverage_manifest.json` + `MANIFEST.sha256` + README; the generator's
  `PINNED_ONLY_FILES` and `test_stream_contract_fixture.py`'s membership set.

**No flag, no version gate** (R-W0). Version skew rides the existing
fold-entities negotiation: a client that does not declare `scope` gets exactly
today's demoted core, per-subscriber, through S5's promotion.

## 7. Gate results

- `python -m pytest tests/agent_runtime/` — **7262 passed, 2 skipped, 1 failed →
  fixed**. The one failure was
  `test_persona_instance_pull.py::test_the_mint_emits_a_delta_patch_on_the_event_log`,
  and it was a POSITIONAL assumption rather than a defect: that file's fixture
  calls `WorkspaceStore.set_active` during setup, so the log's first
  `state.patched` is now the `scope` row and the test took `[0]`. Its claim was
  never "the mint's patch is first" — it is "the mint emitted one" — so the row
  is selected by entity now. Landed as `2e607c376e`.
- Focused: `test_scope_patch_coverage.py` (13), plus stream/patch/fold/negotiation
  and the contract-fixture suites — 153 + 84 green.
- **Mutation gate** (`--base c894c2b159 --max-candidates 40`, run in a SEPARATE
  detached worktree because the gate rewrites source in place): 4 candidates, and
  the first run reported **1 SURVIVED** —
  `iws-ws1-a-cleared-pointer-leaves-the-row-as-an-absent-key`. It was right about
  why: the null-pointer assertion sat on `build_state_patch`, the layer BELOW the
  one that decides what goes into `changed`, so a producer filtering its nulls
  out on the way in was invisible to it. The assertion moved up a layer (through
  `emit_scope_patch`, plus the half-clear case) and the re-run is **4/4 KILLED**.
  A found hole, not a formality.

## 8. Open

- **WS5 stays gated** (R-W3): `active_workspace_path()` is still in
  `stream._scope_fingerprint` and is now double-covered. Not touched — it needs
  WS1 in the field plus one quiet week.
- The canon graduation (`02-runtime-data-and-shapes.md` /
  `03-transport-and-wire.md`, and the 8.76 s republish-cadence figure into
  `08-performance-and-debt-ledger.md`) is the orchestrator's landing step, not
  this lane's.
- WS1's measured acceptance (a live serve producing a few-hundred-byte patch and
  no attributable core rebuild) needs a running serve; it is WS0/operator work.
