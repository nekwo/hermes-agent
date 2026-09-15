# w18/hb field notes — the CI reds in slices 5 to 8 of run 33969282189

2026-09-06. Lane hb of wave 18. The row is the mission-control queue's
"hermes CI on `main` has not reached a test since 2026-08-04"; my part is
slices 5–8. The CI log is the oracle for anything Linux-only
(`gh run view --repo nekwo/hermes-agent --job <id> --log-failed`).

## What the slices actually contained

The row said "9 files, 17 tests, plus the slice-8 file where no tests ran".
Reading the four logs gives **11 files**, because slice 8 also reported a
FLAKY file the row's summary did not carry:

| slice | job | files |
| --- | --- | --- |
| 5 | 101314848638 | `test_no_midtest_monkeypatch_undo.py`, `test_find_bash_windows.py` |
| 6 | 101314848636 | `test_machine_roots_migration.py` (6), `test_cli_entrypoint_gate.py` (3), `test_nous_provider.py` |
| 7 | 101314848667 | `test_stream_contract_fixture.py`, `test_gateway_launcher_managed_python.py`, `test_docket_stage_claims.py` |
| 8 | 101314848708 | `test_gateway_peers_store.py`, `test_async_delegation.py`, `test_tombstone_registry.py` (collection error), **`test_serve_socket_lane.py` (FLAKY — failed attempt 1, passed on retry)** |

## The four classes, and how many of each

Nine reds, and they are not one problem:

1. **A Windows assumption in a test** (2): `test_find_bash_windows`,
   `test_gateway_launcher_managed_python`.
2. **A real product defect that only a POSIX host could show** (3):
   `_ABS_PATH_RE`'s POSIX branch could not hold a space; `entrypoint_name`
   used the running host's separator; the stream-golden redaction baked in the
   generating host's separator. Plus `note_peer_seen`'s two clock reads, which
   is host-independent and was merely *hidden* by Windows' coarser clock.
3. **A test left behind by a deliberate production change** (2, and both are
   the SAME commit): `96cfc09a34`, the eager-tool-discovery audit, moved
   `_load_config_oauth_section` to `load_config_readonly` and
   `restore_durable_completions` out of `ProcessRegistry.__init__`. The nous
   dashboard-auth fixture and the async-delegation restart E2E each kept
   addressing the retired surface. Neither could have been noticed: hermes CI
   reached no test between 2026-08-04 and this run.
4. **The CI checkout, not the code** (2): the slice job used
   `actions/checkout`'s default `fetch-depth: 1`, and two shipped gates read
   this repo's history.

## The three findings worth carrying past this lane

**A gate that cannot tell "no" from "I cannot see" produces a false
accusation.** `test_docket_stage_claims` named seven BACKED stages in slice 7
purely because `merge-base --is-ancestor` answers no in a one-commit clone.
Its own self-probe (`test_ancestry_is_the_question_asked_not_existence`)
passed the whole time, because both of its cases — HEAD, and forty zeroes —
are still right in a clone with no history. A probe has to test something the
broken environment gets wrong. Reproduced exactly with
`git clone --depth 1`: same seven stages, verbatim.

**A subprocess with `check=True` warmed at module import is a
collection-level hazard.** `test_tombstone_registry.py` lost all 1,155 of its
tests to one `git diff` exit 128, reported as `subprocess.CalledProce...`. The
cost of one broken precondition should be one red test with a readable reason.

**A cross-repo, cross-OS byte pin cannot carry the generating host's path
syntax.** The stream goldens redacted the temp ROOT to `<isolated-root>` and
left the TAIL alone, so eight of the nine generated files were reproducible
only on Windows — and the launcher's byte-identical vendored copies inherited
those bytes. The diff after the fix is separators and nothing else, verified
per file by folding `\\` to `/` in the old bytes.

## Cross-stack, for the launcher lane

The launcher's wave-18 lane `la` was told to re-vendor
`delta_agent_create_narrow_profile.json`. After `262a165b47` **eight** goldens
plus `MANIFEST.sha256` move: `delta.json`,
`delta_agent_create_narrow_profile.json`, `delta_batch.json`, `hydrate.json`,
`hydrate_authoritative_same_offset.json`, `hydrate_running_work_owner.json`,
`hydrate_stale_first.json`, `patch_agent_create.json`. Runtime-safe (opaque
display paths), so the owed work is the byte mirror plus both manifests. The
copy-status block at the top of `tests/fixtures/stream_frames/README.md` says
so.

## Killing mutations, recorded

| commit | mutation | reds |
| --- | --- | --- |
| `bc04fe139c` | drop `if not _IS_WINDOWS: return existing_path` | `test_off_windows_it_is_a_no_op` |
| `6e16b4cf97` | restore the `monkeypatched.undo()` spelling | the AST gate, naming the file and line |
| `854f0f2482` | space-free POSIX segment class | the two spaced-path cases |
| `854f0f2482` | let a segment OPEN with a space | `test_slashes_in_prose_are_not_absolute_paths` |
| `854f0f2482` | `exe_suffix()` returns `.exe` unconditionally | the host-relativity test |
| `d956c20cea` | drop the backslash from the separator tuple | 5 tests, incl. both parametrised CI shapes |
| `d3fa1a2731` | point the plugin's import back at `load_config` | the anti-vacuity guard + the original red |
| `262a165b47` | put the Windows spelling back into `hydrate.json` | the separator gate + byte-compare + both manifests |
| `524b12d97a` | remove `_venv_interpreter`'s Windows branch | the launcher render test |
| `6a46810058` | restore the two clock reads in `note_peer_seen` | the one-clock-read AST gate |
| `7d53005a8a` | make `restore_durable_completions()` a no-op | the restart E2E |

`468d8f97ee` (the workflow) and `b67e55c760` / `2fb6b107e9` (the two
history gates) were proven against a real `git clone --depth 1` instead: the
clone reproduces the CI failures exactly, and with the commits applied it reds
on the precondition, naming the shallow checkout.

## Owed / handed back

- **`test_serve_socket_lane.py::test_the_accept_loop_reports_how_it_ended_instead_of_dying_quietly`**
  — flaky on Linux, and the mechanism is a real product weakness, not a slow
  test. `ServeSocket._close_listener` calls `listener.close()` and nothing
  else. On Windows `closesocket` aborts a pending `accept()`, so the loop
  returns at once and stamps `accept_loop_exited`; on Linux another thread's
  `close()` does not wake a blocked `accept()`, so the field whose entire
  purpose is "the loop never stops quietly" is set only by luck. `close()`
  does `thread.join(2.0)` and moves on regardless, so nothing enforces it. The
  cure is a wakeup, not a longer `WAIT` (15 s already, and widening a deadline
  is refused): `listener.shutdown(SHUT_RDWR)` before `close()`, a self-connect
  to the bound port, or a socketpair the accept loop selects on beside the
  listener. Not taken here — it is a serve-lane product change outside this
  lane's file list.
- **`tests/plugins/dashboard_auth/test_self_hosted_provider.py`** (2 tests) is
  lane **ha**'s file (slice 3) and reds locally on Windows with what looks
  like the same `96cfc09a34` accessor drift as the nous provider. Flagged, not
  touched.
- **`tests/test_subprocess_home_isolation.py::TestGetSubprocessHome::
  test_two_profiles_get_different_homes`** is red on Windows BEFORE any change
  in this lane (verified by stashing) and appears in no CI slice's red list.
  Untouched, and not mine.
