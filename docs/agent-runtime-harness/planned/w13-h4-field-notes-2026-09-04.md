# w13/h4 — field notes, 2026-09-04

Seven rows, all from `mission-control-queue.md`. Worktree branch `w13/h4`, base
hermes `9ea840bb90` (main; the brief's `e6dbcbb40c` is an ancestor of it — the
branch was cut after the D1 gateway wave landed). Test env: the canonical shared
venv `~/.venvs/hermes-test`, which `scripts/run_tests.sh` probes on its own.

Order below is the order the work happened in, which is not the order of the
row file: the two long measurements (the full `tests/hermes_cli` lane and the
coverage-claim gate) were started first and read later.

---

## 1. The five duplicate-named tests in `test_doctor.py` — UNION, done

`3e75d0…`-style shadowing, but in a test file. Python keeps the LATER definition
of a name, so `:447`–`:712` never ran.

**Measured before touching anything:**

```
python -m pytest tests/hermes_cli/test_doctor.py --collect-only -q | grep catalog_aliases
-> 4 params: ai-gateway, opencode-zen, kilocode, kimi-coding
```

`nvidia` and `moa` — the earlier block's two — appeared nowhere. That is the
red: the dead block is invisible to collection, so no assertion in it has ever
been evaluated.

**Why a union and not a delete.** The two blocks are not copies. The dead one
patched `auth.get_nous_auth_status_local`; the live one patches
`get_nous_auth_status`. Both are real functions (`hermes_cli/auth.py:6620` and
`:6705`). Same for the parametrize lists and the vendor-slug assertion set.

**What changed.** The five dead definitions are gone; the five live ones patch
BOTH Nous spellings; the parametrize list gains `nvidia` and `moa`; `nvidia`
joins the vendor-slug arm the dead copy put it in (`moa` deliberately does not —
the dead copy did not assert it there either).

`test_run_doctor_accepts_vendor_slugs_for_named_custom_provider` sits inside the
dead block's line range and is NOT a duplicate. Kept. Deleting by line range
without reading the range would have taken it.

**After:** 6 params collected; `51 passed in 117.89s` (was 49 — the two that had
never run). An AST pass over the file reports zero duplicate top-level names.

Commit `9b7ea1fdab`.

## 2. The coverage-claims red — five S2 claims, done

**Measured:** `python -m pytest tests/test_coverage_claims_resolve.py -k
test_every_coverage_claim_names_a_test_that_exists` → `1 failed in 171.08s`,
naming exactly the five the row named.

Fixed strictly those five lines — another session owns the S-lane.

| claim | verdict |
|---|---|
| `…_names_the_session_id_route` | spelling drift → repointed at `…_names_the_session_route` |
| `test_notify_operator_rides_a_remote_dispatch_row` | spelling drift → repointed at `test_notify_operator_rides_the_row` |
| `…_send_with_a_title_opens_a_fresh_far_thread` | **never written** → UNCOVERED SEAM, with the sender-side test that DOES exist named beside it |
| `…_is_a_fresh_chain_root_on_b_recorded_as_a_known_gap` | **never written** → UNCOVERED SEAM; the parity table's "still ✗ by design" row was itself unpinned |
| `test_decision_contract_registry.py` (BARE, field notes) | **false positive on correct prose** — the note said no such file exists and spelled its name to say so. Reworded to name the gate that does exist. |

Two of five were the gate seeing a sentence that was already telling the truth,
and two were seams the wave claimed and did not build. Only one was a typo of
the kind the row's title implies.

**After:** `1 passed in 74.43s`. Commit `cb4f8f9c8b`.

## 3. The `dashboard_auth` flake — DELETE, not reproduced

The row asked for one thing: re-run the full lane once. Done, at the base,
before any commit of mine.

```
scripts/run_tests.sh tests/hermes_cli -j 8
=== Summary: 598 files, 4550 tests passed, 1 failed (100% complete) in 2453.1s (8 workers) ===
```

All three named files passed on the FIRST attempt, no retry:

```
✓ tests\hermes_cli\test_dashboard_auth_native_flow.py     (4✓,  53.7s)
✓ tests\hermes_cli\test_dashboard_auth_middleware.py      (14✓, 62.5s)
✓ tests\hermes_cli\test_dashboard_auth_status_endpoint.py (2✓,  32.8s)
```

The runner's retry list held seven files; none of the three is on it. This is
the row's own 597-file evidence shape (598 here) and it does not reproduce. The
box was under heavy concurrent load from thirteen agents and my own pytest runs
while this ran, which makes a non-reproduction stronger, not weaker.

Nothing changed; nothing to commit.

## 4. AGENTS.md's stale push-gate table — done

`.githooks/` holds one file: `post-merge`. AGENTS.md §Testing opened with
"### The push gate — install it once per clone" and a two-lane table for a
`pre-push` hook deleted on 2026-09-03 (`504953f6ad`) — three sections before
another section that says it was removed.

Rewritten. The "why" survives (an unrun gate is indistinguishable from a passing
one) with the 2026-09-04 recurrence beside it. "~18 min" is replaced by the
measurement `dcba382f0a` recorded — `tests/agent_runtime tests/hermes_cli` alone,
1014 files / 12186 passed / 1 failed in 1508.2s at 8 workers, 25.1 min for TWO of
four — stated as a floor.

Two stragglers found while checking: the `--check` comment called itself "the
gate Lane A runs", and the four-directory paragraph called that set "Lane B of
the push gate". Both fixed.

Lane A after: `dump_cli_contract.py --check` exit 0 (191 paths, sha
`9a64ef52c737ca62`); `doc_cite_adjacency.py --exclude archive --exclude planned`
exit 1 with 4 unwaived failures, all in `01-system-architecture.md` and
`07-observability.md` — neither touched here, pre-existing on main.

Commit `1305ff1e29`.

## 5. The `AgentRuntimeError` catch-all — a real defect found under the question

The row asks whether the catch-all's default should be `internal_error` at all.
Answering it means surveying every subclass, and the survey found a live bug
before it reached the design question.

`_error_code_for_exception`'s `ArchiveUnreadable` arm returns `exc.code` for the
whole family, and the comment beside it states the invariant: a subclass
"inherits the exit family and the cure SHAPE ... while naming a DIFFERENT file".

It does not. `ERROR_EXIT_CODES.get(code, 1)` falls back to 1 for any code the
table lacks, and neither `cards_unreadable` (`board_store.py:775`) nor
`persona_instances_unreadable` (three sites in `persona_assignments.py`) had a
row. Both are really raised. So both exited **1** with `retryable: false` beside
an honest family-7 `error.code`, while their sibling `actors_unreadable` exited
7 — a corrupt server file read as a harness crash, arriving through the door
left open behind the fix for exactly that.

**Red first**, two new tests in
`tests/hermes_cli/test_error_exit_code_producers.py`: both failed naming those
two codes. Green after two table rows and two retryable-set entries.

**The design question is NOT answered here** — it moves live exit statuses and
needs the operator. The survey is recorded beside the catch-all instead: six
subclasses still land on `internal_error`, four of them refusals, and
`ActorArchived` declares `code = "actor_archived"` and can never spend it
through this lane (only the two arms that catch it by hand name it), with no
`ERROR_EXIT_CODES` row to move to.

Verified: `test_error_exit_code_producers.py` 10 passed; `test_board_store.py`,
`test_office_store.py`, `test_tombstone_registry.py`,
`test_serve_rpc_office_remove.py`, `test_response_contract_fixture.py` 1296
passed.

Commit `5cb386ad26`.

## 6. An automatic lane for the coverage-claim and mutation gates — done

The row treats this as an open "which gates get a runner at all" decision. It is
narrower than that at this base, because the lane already exists:
`scripts/unattended_suite_run.ps1` plus `scripts/hermes-unattended-suite-task.xml`.
It already carried the mutation-claim inventory. It did not carry the
coverage-claim gate — which is the whole measured failure the row cites.

Added as section 3: `scripts/run_tests.sh tests/test_coverage_claims_resolve.py
tests/scripts`. Its own section rather than two more roots on section 1, because
section 1's scope is the RULED one (R3's parity proof was run on exactly those
four directories) and widening it quietly would make every future report's "the
validated suite" mean something the ruling does not cover.

**Proved by running exactly that command:**

```
scripts/run_tests.sh tests/test_coverage_claims_resolve.py tests/scripts -j 4
=== Summary: 18 files, 136 tests passed, 1 failed (100% complete) in 171.6s (4 workers) ===
FAILED tests/scripts/test_doc_cite_adjacency.py::test_the_live_canon_is_capped_by_its_baseline
```

That red is the SAME four unwaived cites Lane A reported in row 4, reached from
a different direction — and it is a second unreported gate that lives inside the
scope nothing ran, found by the first run of the lane that now runs it. It is
red on `main`; not mine.

Also fixed in passing (same file, stale claim): the interpreter comment said
"what IS the canonical test env" is an open row. `run_tests.sh` has probed
`$HERMES_TEST_VENV` / `~/.venvs/hermes-test` since 2026-09-03.

`ParseFile` on the `.ps1`: 0 errors. AGENTS.md and the task XML's run-book both
say "three" now.

Commit `8896589d24`.

## 7. The stream golden's prompt-body bulk — PLAN ONLY

Re-measured at this base: frame 56,627 B, `prompt_observability` 33,143 (58.5%),
`prompt_layers[].content` 20,250 across the two entries (35.8%). Confirms
w12/m2's correction, with drift in the direction the row predicted — the table
grew again between two measurements of the same row.

Not built. The cut crosses a wire a launcher screen reads
(`initial_chat_context_dialog.dart` renders `layer.content`), so it is an
operator ruling, not a patch. Two findings that make the eventual build cheap
are recorded with it: the eviction machinery already exists one field over
(`_evict_final_model_input`, plus two more instances of the same stub pattern in
the same module), and `MissionPromptLayer.content` is already nullable
launcher-side.

Plan: `planned/w13-h4-stream-golden-prompt-body-budget.md` — three stages, each
with its red-first test and its must-not-change, all blocked on R-1.

The row's own suggested cut (a fixture persona resolving no prompt context) is
recorded as NOT recommended so nobody re-proposes it: it shrinks the golden and
leaves the live projection exactly as it is, and a golden that stops carrying
what the producer emits stops being a contract.

Commit `418323eca9`.

---

## Reds on `main` that are not mine

* `tests/hermes_cli/test_dashboard_admin_endpoints.py::TestSystemStatsEndpoint::test_stats_shape`
  — `arch` comes back `''` on this box; the test asserts every identity field is
  truthy. Environmental or a real hole in the stats endpoint's arch probe; one
  failure in the 598-file lane.
* `scripts/doc_cite_adjacency.py` / `tests/scripts/test_doc_cite_adjacency.py`
  — 4 unwaived cites, `docs/agent-runtime-harness/01-system-architecture.md:694`
  (`harness.py:4915 -> _cmd_characters_auto`) and
  `07-observability.md:636` (two `hermes_cli/harness.py` line ranges, "names:
  unavailable"). Red before my first commit; both files untouched here.
* Four files that time out at 8 workers and still fail the runner's 1-worker
  retry: `test_config_loader_e2e.py`, `test_dump_env_visibility.py`,
  `test_dump_terminal_backend.py`, `test_kanban_boards.py`.

## Two things worth knowing next time

* **A "duplicate test names" row is a union row until you diff the bodies.**
  Both halves here disagreed about which of two real production functions to
  patch. A delete would have thrown away half the coverage and left the file
  looking correct.
* **The coverage-claim gate cannot tell correct prose from a rotted citation.**
  One of its five reds was a field note stating, accurately, that a file does
  not exist — and spelling the name to say so. Its own failure text says green
  means "the citation is not rotted", never "the seam is covered"; the inverse
  needs saying too.
