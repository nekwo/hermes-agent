# w14/h3 — field notes, 2026-09-04

Five rows, all hermes, base `3d3a33be3e`, branch `w14/h3`. One commit per row,
none pushed. Every figure below was RUN, not carried over from the row text —
three of the five rows' own numbers had moved since they were written.

---

## Row 1 — the foreign-line cite arm (36 cross-repo cites)

**What the row asked.** Port the launcher's arm 1 (`test/architecture/
doc_cite_adjacency_test.dart`, shipped 2026-09-04 w12/l1) into hermes: a
`path.ext:N` cite whose path names no file `git ls-files` reports is a FAILURE,
because nothing on this side can ever tell a reader that a launcher line number
went stale. Spec verbatim in the launcher's
`docs/mission_control/planned/w12-l1-cite-rot-and-console-lifts.md` §A4.

**What I measured.** The row said 36, distributed 02×1 04×2 05×4 06×20 07×9. Run
at `3d3a33be3e`: **37 path cites** (06 is 21, not 20) plus **16 bare `:N`
continuations** trailing a `.dart` path, which the row did not count at all —
**53 total**. Also measured, and the reason the ambiguity split matters: 35 `.py`
cites in the same canon resolve AMBIGUOUSLY (several tracked files share the
basename), and the single `resolve_path -> None` could not tell those from the
foreign ones.

Two of the 53 were already rotted. `.../mission_control_serve_session_io.dart
:1615-1621`, cited for the spawn receipt's env keys, lands on
`_routeStreamLaneFrame` — a stream-lane router. And `07-observability.md:94`
carried a `.dart:23-40` inside a parenthesis that reads, in the doc's own words,
"Symbols, not lines".

**What changed.** `scripts/doc_cite_adjacency.py` gains arm 1 beside the
adjacency walk: `FOREIGN_CITE` / `FOREIGN_PATH_MENTION` (dart|py|json|yaml),
`resolve_candidates` splitting "names no tracked file" from "names several",
`foreign_cites_in_doc` (the rule, no git and no filesystem in it) and
`foreign_walk`. Budget `docs/agent-runtime-harness/foreign-line-cites.json`,
`{_comment, waived}`, asserted in both directions. All 53 cites re-anchored to
the symbol their own sentence already named, so the budget landed **EMPTY** —
the same result the launcher's nine gave.

Arm 1 keeps its own patterns so no `.dart`/`.json`/`.yaml` token can reach
`verdict`, which parses an AST. Arm 2's verdicts are byte-identical across the
change: 322 cites, 286 checkable, 201 adjacent, 11 in-symbol, 74 FAILED, 68
no-subject, 40 ambiguous, 3 past-EOF, before and after.

**Red-first.** With the arm built and the budget empty, before the doc edits:
`UNBUDGETED: 53`, exit 1. After the edits: `foreign line cites found : 0`,
exit 0.

**Commands.**
- `python scripts/doc_cite_adjacency.py --exclude archive --exclude planned`
  → 1 (red-first), then 0.
- `scripts/run_tests.sh tests/scripts/test_doc_cite_adjacency.py` → 0 (40 tests,
  11 of them new).

**Left.** Nothing for this row. One observation not fixed: `07-observability.md`
carries a bare `` `:255-265` `` whose sentence names no path, so neither arm can
resolve it; it is a line number pointing at nothing in particular. Out of the
row's scope.

Commit `f57e458243`.

---

## Row 2 — the placement-policy test mirror

**What the row asked.** Mirror the launcher's two closed constant halves
(`mission_office_placement_policy_test.dart`) into hermes: derive the constant
list rather than restating it, and force each constant to be exercised by a case.

**What I measured.** `tests/agent_runtime/test_office_layout_policy.py`'s join
typed out all seven names in a dict literal, so it answered "do these seven
agree" and never "are these seven all of them"; and nothing anywhere required a
constant to change any case's outcome.

**What changed.** `_declared_lattice_constants()` enumerates the numeric
constants off the IMPORTED MODULE OBJECT — runtime introspection, never a source
scan, which AGENTS.md bans outright and which Python does not need. The
perturbation sweep is parametrized over that same derived set: it asserts the
unperturbed run reproduces every `expected` first, then moves one constant and
requires some case to notice.

**The sweep found a hole on its first run.** `DESK_LANE_OFFSET` moved nothing,
because the resolver was feeding the scan the case file's own `lane_offset`
field. That field is the launcher's PREDICTION of what the policy resolves,
asserted against it elsewhere; feeding it back in routed the scan around the
constant. The resolver now takes folder and lane from the policy —
`next_free_slot_for_kind`'s body with the actor flattening skipped, which is the
production path.

Measured margins, so the anti-vacuity claim is checkable: `origin_x` / `origin_y`
move 13 cases each, `row_spacing` 9, `desk_lane_offset` 3, `column_spacing` and
`rows_per_column` 2 each, `occupancy_radius` 1.

**Killing mutations, both verified by running them.** Adding an eighth constant
(`SHELF_SPACING = 2.0`) reds the derived join. Deleting
`boundary_item_at_exact_radius` from the case file reds the `occupancy_radius`
parametrization — it is the only one of the thirteen that witnesses the radius.

**Commands.** `scripts/run_tests.sh tests/agent_runtime/test_office_layout_policy.py`
→ 0 (49 tests, 7 of them the new sweep).

Commit `ee578c4085`.

---

## Row 3 — the MANIFEST-sibling `-text` gate mirror

**What the row asked.** Port the launcher's
`test/architecture/manifest_sha256_siblings_text_test.dart`: enumerate every
tracked `MANIFEST.sha256` and require `git check-attr text` to resolve `unset`.

**What I measured.** Three tracked `MANIFEST.sha256` families, and only
`tests/fixtures/office_layout/` carried a `-text` rule. `response_envelopes` and
`stream_frames` resolved `text: auto` — for the manifests themselves and for all
24 files they pin. Both READMEs state the launcher commits byte-identical copies
(`test/fixtures/hermes_responses/`, `test/fixtures/harness_stream/`), and the
launcher pinned both of those sides on 2026-09-04, so hermes was the half that
could still drift.

**What changed.** `tests/test_byte_pinned_fixture_families.py` (three tests: the
manifests themselves, the siblings each manifest names, and a floor refusing a
walk that finds fewer than three families), plus two `.gitattributes` rules.

**Red-first.** 2 manifests + 24 siblings listed as `text: auto` before the pins,
exit 1.

**No fixture bytes moved.** The families were already LF in the index, so
`tests/test_line_endings.py`'s `DELIBERATE_CRLF` stays empty and
`test_response_contract_fixture.py` still gets its committed bytes back (18
tests, 0 failed).

**Commands.**
- `scripts/run_tests.sh tests/test_byte_pinned_fixture_families.py tests/test_line_endings.py` → 0.
- `scripts/run_tests.sh tests/agent_runtime/test_response_contract_fixture.py` → 0.

Commit `93c652955e`.

---

## Row 4 — the stale-docket gate port

**What the row asked.** Port the launcher's `no_stale_shipped_mcf_row_test.dart`
rather than invent one: nothing in hermes strikes a docket or ledger row when a
stage lands.

**Premise re-derived and TRUE.** `scripts/` has no such check;
`.githooks/` holds only `post-merge`.

**What I found that the row could not know.** The launcher's gate rests on a
convention hermes does not have. There, a landing claim is `git log` finding a
commit whose SUBJECT names the row's id (`MCF-39`), which is what makes "the row
says OPEN and the work shipped" provable. Hermes subjects name a surface and a
change and never a stage id, so from here there is no way to find the commit
that landed a stage the docket never edited. That arm is not portable without a
convention being adopted first.

**What IS derivable, and is what I built** (`tests/test_docket_stage_claims.py`):

* a stage heading claiming EXECUTED/LANDED must cite a commit that is an
  ANCESTOR OF HEAD;
* a doc claiming stages "remain" must not name a stage its own rows record as
  landed on such a commit — the eight-day defect, as a rule.

Both compare the docket against the repository, never against a second document,
which is the whole point of the launcher's gate: two documents agreeing proves
consistency, never correctness.

**Ancestry, not existence, and the difference is measured.** 56 sha-shaped
tokens under `docs/agent-runtime-harness/` resolve to no commit here, and the
great majority are launcher commits cited legitimately. `git cat-file -e` would
report all 56 as missing and invent 56 findings; `merge-base --is-ancestor`
asks "did this land HERE".

**The live corpus at `3d3a33be3e`.** 82 docket files, 42 stage sections, 11 with
an in-history landing sha, 7 stage headings claiming done — **7 of 7 backed** —
and 1 "stages … remain" claim, consistent with its own rows. Green at the base,
so the rule is falsified on fabricated dockets (four unit tests) rather than on
the live corpus, and three anti-vacuity floors refuse a walk that finds nothing.

**Commands.** `scripts/run_tests.sh tests/test_docket_stage_claims.py` → 0
(9 tests).

**Left.** The commit-subject arm. See the row hand-back.

Commit `4ff4a986a2`.

---

## Row 5 — the `AgentRuntimeError` catch-all (RULED)

**The ruling.** Option 2: read `exc.code` when the class declares one, keep the
constant otherwise; delete the four redundant escapes; add
`ERROR_EXIT_CODES` / `_error_hint` rows for any class that gains a code; pin it
with a test enumerating every subclass.

**What I measured.** All four escapes ahead of the catch-all
(`ArchiveUnreadable`, `WorkspaceUnresolved`, `IdempotentReplayUnresolved`,
`IdempotencyKeyVerbMismatch`) did nothing but `return exc.code`. Enumerating the
subclass tree with every `agent_runtime` submodule imported: **10 classes** carry
a class-level `code`, three more set one per-raise in `__init__`, and five carry
none. Of the ten, `actor_archived` was the only code with no `ERROR_EXIT_CODES`
row. `realm_default_workspace` (`WorkspaceDeleteBlocked`, reached by hand from
`harness.py`) had none either, so it exited 1 — `internal_error`'s number —
while its envelope carried the correct typed reason.

**What changed.** The four escapes are gone; the catch-all reads the declared
code and still answers `internal_error` for a class that has none.
`actor_archived` and `realm_default_workspace` gain family-4 rows and their own
hints (the default hint, "retry after correcting the request", invites exactly
the re-add `actor_archived` exists to refuse). Family 4 for `actor_archived`
because `serve_rpc.py` already answers `ERR_CONFLICT` for the identical
condition — one refusal, one family across the two lanes. Three now-unused
imports dropped.

**Red-first**, both new pins failing at the base: the mapping returned
`internal_error` for `ActorArchived`, `DuplicateDeskRefused` and
`ClassKeyedPlacementRefused`, and `actor_archived` had no table row.

**One trap worth recording.** `__subclasses__` only sees classes whose module has
been imported. Without a `pkgutil` sweep over `agent_runtime`, the enumeration
finds 8 classes instead of 10 — and the two it misses (`DuplicateDeskRefused`,
`ClassKeyedPlacementRefused`) are both live refusals. A subclass-walking gate
without that sweep is a gate that shrinks silently.

**Commands.**
- `scripts/run_tests.sh tests/hermes_cli/test_error_exit_code_producers.py` → 0
  (13 tests, 3 new).
- `scripts/run_tests.sh tests/hermes_cli/test_harness_parts_namespace.py
  tests/agent_runtime/test_office_store.py
  tests/agent_runtime/test_office_class_key_guard.py
  tests/agent_runtime/test_board_store.py
  tests/agent_runtime/test_response_contract_fixture.py
  tests/agent_runtime/test_serve_rpc_office_remove.py
  tests/agent_runtime/test_serve_rpc_office_resolve.py
  tests/agent_runtime/test_tombstone_registry.py` → 0 (1,351 tests).

**Left.** `errors.py`'s `WorkspaceDeleteBlocked` docstring names
`workspace_has_goals` as one of its two typed reasons; no production site raises
it, so it has no table row and could not get one (the producer gate would refuse
it). Either the guard is missing or the docstring is stale — not this row's
question, and recorded rather than guessed at.

Commit `a431dfdc92`.

---

## Lane A, at the end

| check | exit |
| --- | --- |
| `scripts/doc_cite_adjacency.py --exclude archive --exclude planned` | 0 |
| `scripts/dump_cli_contract.py --check` | 0 |
| `scripts/dump_toolset_manifest.py --check` | 0 |
| `scripts/emit_harness_tool_inventory.py --check` | 0 |

## A red that is not mine

`tests/test_coverage_claims_resolve.py::test_every_coverage_claim_names_a_test_that_exists`
is red at the base. The claim is at line 246 of
`docs/agent-runtime-harness/planned/w13-h1-field-notes-2026-09-04.md`, which
quotes a pytest `FAILED …` line naming a test in that same file that does not
exist under it; the gate reads the quoted name as a coverage claim and cannot
resolve it. The file is untouched by this lane and its last commit is
`8f9f0b8ac3`. Not re-quoted here on purpose — repeating the token would file the
same red a second time.
