# Sync-honesty lane — field notes, 2026-09-02

Running record for the five rows worked on branch
`fix/realm-office-board-sync-honesty` (cut from `origin/main` at `ca8eb228c9`).
Four rows in `agent_runtime/`, one product row in `plugins/memory/`. Written as
the work happened; the conclusions live in
[01 §Realms and workspaces](../01-system-architecture.md#realms-and-workspaces),
[01 §The board](../01-system-architecture.md#the-board),
[06 §The board](../06-office-and-board.md#the-board--still-capability-only),
[06 §The fold model](../06-office-and-board.md#the-fold-model--what-a-fold-is-and-who-promotes-a-batch)
and [07 §The honesty contract](../07-observability.md#the-honesty-contract).
This file carries the evidence those sections state the conclusion of, and the
places a row's own premise turned out to be half right.

## §1 — All four premises held, and one of the four targets did not

Every row named a real defect and every one reproduced at `ca8eb228c9`. The
interesting failure is not a premise but a TARGET: row 2 asked for two things,
and the second one is unsafe for a reason the row could not have known without
reading the launcher's fold. It is written up in §3 rather than buried in a
commit body, because "the fix is right and the follow-on is wrong" is the
outcome a queue row is least likely to survive.

## §2 — Row 1: what "fails closed" was allowed to close

`realm_sync_status` called `_authorize(realm, "status", …)` FIRST and let it
raise. On a server-bound realm with an expired or revoked credential the whole
verb answered `sync_auth_failed` — deleting `store_drift`,
`skills_drift`, `profile_artifacts_held` and `workspace_statuses`, every one of
which is a pure local read, at the moment they were most worth having.

**The shape of the fix was decided by what already existed, not by taste.** The
envelope has carried `remote_checked` / `remote_check_error` since the
outbound-drift lane, for the case where the fetch could not reach the remote,
and the launcher already renders it (`realm_sync_detail_sheet.dart`'s
`_RemoteUncheckedNote`: "Couldn't reach the realm remote — showing last known
state"). A denial is the same fact to that sheet — what follows is the last
known LOCAL picture — so the degrade rides the existing pair rather than a new
key. Nothing on the launcher side has to move for this to render honestly.

**Three things this row deliberately did NOT do**, each because the honest
answer was smaller than the tempting one:

1. **`publish` / `pull` are untouched.** They call the same `_authorize` and
   still refuse whole. Every byte either verb touches is a realm-wide assertion;
   there is no half of them a denied member is entitled to run. `_authorize` now
   says this in a docstring so the asymmetry reads as a decision.
2. **`_ensure_sync_repo` is not called on the denied path at all.** Its remote
   branch CLONES, which is precisely the operation just refused — calling it
   would have turned a local diagnostic into a network attempt against the
   member's realm host, and (measured while designing this) would have changed
   `test_server_bound_realm_cli_denies_without_credential`'s answer from
   `sync_auth_failed` to `sync_remote_unreachable`: a WORSE error, arrived at by
   trying harder.
3. **`git init` in its place was rejected outright.** It looks like a free way
   to give the degraded read a repo shell. It is not: `_ensure_sync_repo`'s
   fast path is `repo.exists() and (repo / ".git").exists()`, so an empty repo
   minted here is indistinguishable from a completed clone and the member's next
   `pull` would converge against nothing, forever, with no error.

**So the degrade has a FLOOR, and the floor is the interesting part.** A member
who has never cloned this realm has no local repo, so there are no local git
facts to degrade to — and `_git_state` against a non-existent directory returns
all zeros through its `check=False` calls, which `_sync_state` renders as
`in_sync`. That is a worse lie than the refusal: "in sync" about a realm this
box has never seen. The verb therefore re-raises the original error object,
unchanged, when the repo is absent. A diagnostic degrades to the facts it HAS;
it does not manufacture the ones it lacks.

**Launcher reader, checked and reported (no edits made).**
`realm_sync_models.dart` parses both keys absent-tolerantly
(`body['remote_checked'] is bool ? … : null`, `_trimmedOrNull`), and
`realm_sync_detail_sheet.dart` renders `_RemoteUncheckedNote` on
`entry?.report?.remoteChecked == false` with the code appended. So the degraded
envelope renders honestly TODAY with no launcher change. One wording row is
filed back (§6): the note says "Couldn't reach the realm remote", which is not
what `sync_auth_failed` / `role_insufficient` mean — the remote was reachable
and said no. The sheet already has a `_PermissionFailedNote` for permission
outcomes; the codes should route there.

## §3 — Row 2: the fix is right, the follow-on is not

The premise held exactly. `resolve_conflict`'s `take="remote"` adopt arm did
`_write_actor(actor)` with no `_emit_actor_patch`, while its edit-vs-remove
sibling went through `_archive_actor_locked`, which emits its paired `remove`
even with the domain event suppressed (`emit=False` suppresses the EVENT, and
that function's own comment says the patch fires anyway and why). So a resolve
that TOOK a desk away reached every live consumer and a resolve that GAVE you
one reached none — the H1 asymmetry, on the same store, one method down from the
verb that closed it. Emitting the patch is a straight fix and it landed.

**Moving `office.actor.conflict_resolved` onto
`LIVE_COVERED_DOMAIN_EVENT_TYPES` is NOT, and the derivability audit is what
says so.** Two independent refusals:

1. **The conflict list.** Every arm — including `take="local"`, which writes no
   actor at all — calls `_archive_conflict_sidecar`. The office projection reads
   those sidecars into the office ROW's `conflict_actor_keys`
   (`snapshot.office_summary_row`, off `OfficeStore.scan_conflicts`). No
   `office_actor` patch carries that field. Verified on the consumer side rather
   than assumed: `mission_read_model.dart`'s `_applyOfficeActorPatch` writes
   `actors` and `actor_count` and nothing else, and `_officeSurfaceFields` names
   `conflict_actor_keys` among the office-row keys it deliberately refuses to
   let any patch move. A covered batch would fold the resolved desk and leave
   the sync strip's conflict pill lit for the rest of the session — a conflict
   rendered on the row whose conflict that gesture just resolved.
2. **`take="local"` carries no patch at all.** Covering the event would promote
   a batch whose only member the launcher ignores by contract: a patch frame
   carrying nothing, for a gesture that moved the conflict list.

The `patch_coverage.py` comment block has now been wrong about this event twice
— first "it bypasses the upsert chokepoint" (discharged by H1), then "there is
no row for a covered batch to ride" (discharged by this change). Both dead
reasons are kept beside the live one in the module, because a reason that was
retired by a LANDING is the most valuable kind to leave visible: it is the one
someone will otherwise re-derive.

What covering it would actually need is a producer for the CONFLICT LIST — a new
patch entity, or a widened office row plus its capability token. Cross-stack,
filed (§6).

## §4 — Row 3: the board family's H1, two days late

`apply_board_pull` wrote board defs (`atomic_json_write(paths.board_def_path…)`)
and cards (`…board_card_path…`) directly, past `BoardStore`. `board_store.py`'s
own module docstring states the rule those writes broke — "a typed `EventLog`
event on EVERY mutation (standing store rule — an event-less write is invisible
to the watermark-gated snapshot/serve pipeline)" — and `realm_revert.py`'s
`_adopt_from_upstream` docstring carried the queue row for it in prose.

**The diagnosis is the same asymmetry both times, and it is worth naming as a
detector.** In each family the arm that DELETES already went through a store
verb (`archive_card`, `remove_actor`) and emitted; only the arm that ADOPTS
wrote raw. A pull that takes something away is loud and a pull that gives you
something is silent — which is exactly backwards from what an operator would
guess, and is why neither hole was found by watching the product.

`BoardStore.adopt_remote_board` / `adopt_remote_card` are the office twins'
shape: the peer's revision verbatim (no `+1`, or the next `classify_board_pull`
reads an untouched row as locally edited), `updated_by` recording the sync,
nothing re-derived from the write, the absence question asked under the same
`board_lock` that holds for it, and the event a LOCAL write of the same shape
emits — `board.card.created` / `.edited`, `board.created` / `board.updated`
with `change="realm_sync"`.

**Hash-neutrality was checked, not assumed.** `board_models.board_content_hash`
excludes `updated_by`, `revision` and the timestamps, so stamping
`updated_by="realm_sync"` cannot move the baseline the pull records. A test
asserts the recorded baseline equals `board_content_hash(<the remote card as
written to the subtree>)` rather than trusting the docstring.

**One thing was deliberately left alone.** The office SURFACE twin unions
`archived_actor_keys` on adopt (C1) because adopting wholesale erases a
tombstone the peer never heard of. `adopt_remote_board` does NOT union
`archived_card_ids` — the board pull has always overwritten it and this change
does not alter one byte it writes. Making the two families agree moves a
resurrection guard, which is a decision about DATA and does not belong in a
change about events. Filed (§6).

## §5 — Row 4: the loader stopped one step short of an import

`plugins/memory/__init__.py` registered discovered modules with
`sys.modules[full_name] = mod` and never bound the child on its parent package —
the second half of what `importlib` does. The two spellings of one import then
answer differently, permanently, and `importlib.import_module` will not repair
it because it short-circuits on the `sys.modules` row it finds.

Fixed at the loader with one helper, `_publish_module`, applied at all five
registration sites (the synthetic package shell, the `plugins` /
`plugins.memory` parent bootstrap, the provider module, each pre-registered
submodule, and the CLI module).

**The rollback needed a symmetric half, and that was not in the row.**
`_load_provider_from_dir` pops `sys.modules[module_name]` when `exec_module`
raises. Binding the parent without undoing it there would have traded one
two-answers-for-one-name defect for another, aimed at the failure path — a
parent attribute pointing at a half-executed module `sys.modules` no longer
holds. Hence `_unpublish_module`, with its own test and its own mutation claim.

**On `tests/hermes_cli/conftest.py` (NOT edited; another agent owns it this
session).** Its setup-half repair loop walks `sys.modules` and binds any child
whose parent lacks the attribute. That loop is now redundant *for providers this
loader registers* — which is the case its own comment cites (`plugins.memory.
honcho`). It is NOT redundant in general: it repairs any dotted name in the
process, and hand-rolled `spec_from_file_location` loaders exist elsewhere in
this tree. The honest recommendation is to keep it and re-point its comment at
the loader fix rather than delete it; the comment already says "The loader not
doing it is a product row, filed separately", and the product row is now closed.

## §6 — Rows filed back (see the commit body / handback for the verbatim text)

Three, all out of scope for this branch: the launcher sheet's
"couldn't reach" wording over an authorization denial; a producer for the
office row's conflict list (the only thing that would make
`office.actor.conflict_resolved` coverable); and `adopt_remote_board`'s
non-union of `archived_card_ids` against the office surface twin's C1.

## §7 — Suite notes (this box, this day)

`tests/agent_runtime/test_realm_sync.py` timed out twice at the runner's default
300 s per-file cap, at two DIFFERENT git subcommands (`git fetch`, then
`git add`) inside `test_status_fetches_remote_so_upstream_changes_mark_behind`.
Re-run alone it finishes green in 171 s, 63/63. Three other hermes agents were
running suites on this box concurrently; treat it as the contention flake
`AGENTS.md` §Testing describes, compared as a SET against a serial re-run, not
as a defect in that test.

Two failures under `tests/honcho_plugin/` were checked against a pristine
`plugins/memory/__init__.py` from `ca8eb228c9` and reproduce identically there —
`test_oauth_flow.py::test_display_config_path_never_leaks_absolute_path`
(asserts POSIX separators in a rendered path, on Windows) and
`test_cli.py::test_local_setup_stores_jwt_under_host_block` (the setup wizard
blocks on an
interactive prompt and hits the 30 s per-test timeout). Pre-existing and
platform-shaped; not touched.
