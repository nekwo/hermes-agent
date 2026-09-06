# w18/hd — field notes (2026-09-06)

Two small defects, both found by w17/hb while writing something else and rowed
rather than fixed there. Short notes, one section per row.

## 1. `realm sync revert --item` could not address a removed persona instance

**The read.** `_persona_instance_store_drift_items` emits `container=""` for a
baselined instance with no live record — the `workspace_id` is read off the
record, and the record is the thing that is gone. `parse_item_spec` then refused
the row's own `spec` (`persona_instance::<id>`) because it required every one of
the three parts to be non-empty. So the row was counted, named in
`store_drift.items`, and revertable only through `--all` — on the one drift kind
an operator most wants to take one at a time.

**Which fix, and why.** The row offered two: derive the container from the item
key the way the canvas family does (`owner_instance_id_of`), or let
`parse_item_spec` accept an empty container. The first is not available here and
would not be honest if it were: a graph id literally CONTAINS its owner instance
id, which is why the canvas family can derive one, while an instance id says
nothing about the workspace that held it — any derived container would be a
guess printed as a fact. And this family needs no container at all:
`_Upstream.lookup` reads the persona-instance projection (ONE realm-wide
document) by key and never touches the container. So the blank is the accurate value, not a
missing one, and the fix belongs in the parser.

The relaxation is one field wide. Family and key stay required — a blank family
has no transition table to dispatch on, a blank key names nothing — and the
guard now says exactly that (`parts[0]` and `parts[2]`), rather than "all three".

**The property that makes it safe.** A blank container is a SPELLING, never a
wildcard. Selection is `by_spec.get(f"{family}:{container}:{item_key}")`, an
exact match against the derived drift set, so `FAMILY::KEY` reaches only a row
that itself reported a blank; a live agent is still addressed by its workspace.
That is its own test (`test_a_blank_container_is_not_a_wildcard`), because it is
the property the original refusal was protecting and the one a future
"convenience" lookup would quietly break.

**Red → green.** Red: 3 failed, 67 passed
(`test_a_removed_agent_is_addressable_by_item_and_restored`,
`test_a_blank_container_is_not_a_wildcard`,
`test_a_blank_container_parses_to_the_empty_container`). Green after the fix:
70 passed.

**Killing mutations** (each applied on its own, both registered in
`tests/mutation_claims.json` so they keep running):

| mutation | reds |
| --- | --- |
| the guard back to `not all(part.strip() for part in parts)` | all three new tests |
| `by_spec` miss on a blank container falls back to a family+key scan | `test_a_blank_container_is_not_a_wildcard` |
| drop the `parts[2].strip()` half of the guard | both empty-key cases of `test_the_family_and_the_key_are_still_required` |

**Two stale claims corrected in passing.** The canvas family's docstring and
`docs/agent-runtime-harness/01-system-architecture.md` both said a blank
container makes `FAMILY:CONTAINER:KEY` *unparseable*. That was true when it was
written and is the reason the canvas family derives its container; it is no
longer true, and the canvas family's reason is now stated as the naming property
it actually is.

## 2. `draft_lock._claim` answered two different things on two hosts

**The read, reproduced.** With a DIRECTORY at the lock path,
`os.open(path, O_CREAT|O_EXCL|O_WRONLY)` raises `EEXIST` on POSIX and `EACCES`
on Windows. `_claim` caught only `FileExistsError`, so the identical draft
refused the identical generation with a typed `DraftBusy` on one host and an
unhandled `OSError` from the middle of the verb on the other. Measured here
before the fix, unfaked: `PermissionError: [Errno 13] Permission denied` out of
`draft_lock.py:133`. That is the exact split
`agent_runtime.locks._file_lock`'s docstring exists to forbid — "the same call
had two contracts" — and the module's own rule is one policy on both hosts.

**The fix, and the one narrowing that matters.** `except PermissionError:` is
added, but it answers "taken" only when the path EXISTS. `EACCES` also means
"this process may not create files here" — a read-only draft, a bad ACL, a
quarantined copy — and that is not a held lock. Answering `DraftBusy` there
would tell an operator to wait for a holder that does not exist and then delete
a lock file nobody ever wrote, so a permission fault over an empty path travels
as itself. That refusal-not-to-lie has its own test.

**The errno is parametrized rather than left to the host.** A Windows-only test
of a Windows-only errno proves nothing on the Linux runner that would have to
catch the regression, and vice versa. So the occupied-path test raises each
error class over a real planted directory, and a second test runs the same
scenario with nothing patched at all — whatever this host raises, the answer is
the typed refusal. On this workstation the unfaked one is the Windows arm.

**Red → green.** Red: 2 failed, 10 passed (`…[PermissionError]` and the unfaked
directory test; the `[FileExistsError]` arm and the propagation test were the
guard rails and were green from the start). Green: 12 passed.

**Killing mutations** (both registered in `tests/mutation_claims.json`):

| mutation | reds |
| --- | --- |
| delete the `except PermissionError` arm | `…answers_taken_on_both_hosts[PermissionError]`, and the unfaked host test |
| widen it to an unconditional `return False` | `…nothing_at_the_lock_path_is_not_a_busy_draft` |

**One measured residual, left as it is.** A directory at the lock path is never
aged off, however old it is: `_read_holder` returns early when `read_text` fails
(a directory raises `OSError` there), so the record never gains `age_seconds`
and the refusal always reads `held it for 0s` and never reaches the stale-break
arm. Measured after the fix — `DraftBusy` with `safe_details == {"lock": …}`,
the directory untouched. That is the SAFE side of the edge (nothing tries to
unlink a directory), and the refusal still names the path, which is the
recovery; the only cost is a `0s` in a message about something that has been
there for an hour. Not worth a second read of the holder file on a path that
cannot be one.

**The row's sibling note is not this lane and is left open.** COLD statements in
`agent/charsheet` are untriaged — `draft.py` 25, `draft_lock.py` 6,
`revisions.py` 6 — a different class from one-armed, and nobody has asked for
them yet.

## Gates

`doc_cite_adjacency.py` is **red on `main` before this branch**, and the failure
set is byte-identical with mine apart from one line number that moved because I
inserted text above an already-failing cite in
`docs/agent-runtime-harness/01-system-architecture.md`. Diffed run-against-run
against the primary checkout rather than asserted; it is not this lane's row.
Every other Lane A gate is green.
