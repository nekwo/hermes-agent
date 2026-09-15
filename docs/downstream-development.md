# Downstream development instructions

These fork-specific instructions supplement AGENTS.md and its area guides. Historical measurements describe the original fork baseline; they are not proof of the upstream integration candidate.

**Path references in tool schemas**: If the schema description mentions file paths (e.g. default output directories), use `display_hermes_home()` to make them profile-aware. The schema is generated at import time, which in a hermes CLI process is after `_apply_profile_override()` sets `HERMES_HOME` — a schema string is a LABEL, which is the one thing safe to freeze at import (see rule 3 under "Rules for profile-safe code" for why anything that then READS the filesystem is not).


**State files**: If a tool stores persistent state (caches, logs, checkpoints), use `get_hermes_home()` for the base directory — never `Path.home() / ".hermes"`, and resolve it at call time rather than into a module constant (rule 3 again). This ensures each profile gets its own state.


3. **Resolve `get_hermes_home()` at CALL time, not at module scope.** A
   module-level `X = get_hermes_home() / "..."` is safe only in a process that
   is a hermes CLI entrypoint, where `_apply_profile_override()` has run before
   the import. It is NOT safe under pytest: `_profile_bootstrap.is_hermes_cli_entrypoint`
   deliberately gates the override off there (an import must not re-parse
   pytest's argv or point the whole session at the operator's live profile), the
   module is imported at COLLECTION, and the autouse hermetic-home fixture
   redirects `HERMES_HOME` *afterwards* — so the constant stays frozen on the
   operator's real store while every caller believes it moved. This is not
   hypothetical: it deposited fixture chat sessions into the live `state.db`,
   and `hermes doctor`'s `PRAGMA integrity_check` ran against the developer's
   real database.

   `hermes_state._resolve_default_db_path` is the canonical pattern — resolve
   live via `get_hermes_home()`, while still honoring an explicitly reassigned
   module constant so `monkeypatch.setattr` isolation keeps working:
   ```python
   # GOOD — re-resolves per call, and a pinned constant still wins
   def _resolve_default_db_path() -> Path:
       if DEFAULT_DB_PATH != _IMPORT_DEFAULT_DB_PATH:
           return DEFAULT_DB_PATH
       return get_hermes_home() / "state.db"

   # BAD — frozen at import, which under pytest is before the fixture moves HERMES_HOME
   DB_PATH = get_hermes_home() / "state.db"
   ```

   `tests/test_no_frozen_hermes_home.py` is the gate: it imports every module
   that mentions `get_hermes_home()` under a fresh `HERMES_HOME` and reports each
   module-level attribute still holding that path. It carries a ledger of the
   existing frozen names (mostly upstream `gateway/` / `tools/` / `cron/` files a
   fork rewrite would collide on) and **fails on a stale entry**, so converting a
   module means deleting its line. Do not add to it.

   A frozen name is defensible only when it is a LABEL and never a filesystem
   read — `display_hermes_home()` for a printed path is the example, and
   `hermes_cli/doctor.py` states that split at its own module top.


### Cut your worktree from a NEUTRAL cwd — a read-shaped setup step can move a ref
`git worktree add` and the `git fetch` in front of it are commands about the
CLONE, not about the directory you happen to stand in. Run them from anywhere
OUTSIDE the primary checkout (a scratch directory, or a worktree root that
already exists) and pass every path explicitly. Measured 2026-08-31 at the
W1-H1 landing: a correct `--ff-only` merge was followed by another agent running
`fetch` + `worktree add` from the primary checkout, and `main` was yanked back
to its pre-merge tip with the merge left staged. **Both of the usual tells
lied** — the reflog still held the merge and `push` answered "up-to-date" — so
the rollback is invisible to the checks anyone would run; it was repaired by
`reset --hard` plus a re-merge (`2638504f9b`). This is a third, DIFFERENT
instance of the one-index-per-clone hazard behind the commit-by-pathspec habit,
and it is new in kind because nothing was being COMMITTED — a read-shaped setup
step moved a ref. Corollary for whoever is landing: primary-checkout git writes
are not safe to interleave with a landing, so do the merge and the push in one
breath and do not start a setup step in that window. The launcher carries the
same rule in its `CLAUDE.md` git-discipline section; there is no hook enforcing
it in either repo (see "There is no push gate" under Testing), so it is a rule
agents follow, not a gate.



### There is no push gate — install the one hook there is

```bash
git config core.hooksPath .githooks
```

`.githooks/` holds exactly one hook: `post-merge`, which re-installs the
canonical shared skill packages after a pull. git config is per-clone and
shared by every worktree of it, so one command covers the primary checkout and
every `git worktree add` under it.

**There is no `pre-push` hook in this repo.** One existed from 2026-08-30 and
was DELETED on 2026-09-03 (`504953f6ad`) by operator ruling, in both this repo
and the launcher: a landing blocker that costs a suite run is not how these
checks get value, and pushes are instant now. Everything below is a check
someone runs — by hand, or from the unattended report in the next section —
never something that stops a push.

**Why the checks exist anyway.** hermes `main` sat red and unreported from
`6979bad59` — `test_every_json_verb_states_its_root_or_is_classified`, a whole-
program gate — because CI on this fork is largely inert and nothing local ran
it. An unrun gate is indistinguishable from a passing one. That is still true,
and it is still true with the hook gone: the coverage-claim gate went red on
`main` again on 2026-09-04 by five citations the S2 wave landed, and nothing
reported it. Which of these gets an automatic runner, and where, is an open
row in the Mission Control queue.

The two checks the deleted hook ran, and what they cost:

| check | command | cost |
|---|---|---|
| doc-cite adjacency + CLI contract | `scripts/doc_cite_adjacency.py --exclude archive --exclude planned` (its RULED scope — the bare walk is red by 829 by ruling) and `scripts/dump_cli_contract.py --check` | ~11 s warm |
| the validated suite scope | `scripts/run_tests.sh tests/agent_runtime tests/hermes_cli tests/cli tests/state` | **≥25 min** — see below |

**The suite number.** This section claimed "~18 min" for all four directories.
Measured 2026-09-04 and recorded in `dcba382f0a`: `tests/agent_runtime` and
`tests/hermes_cli` ALONE ran `1014 files, 12186 tests passed, 1 failed in
1508.2s` at 8 workers — 25.1 minutes for two of the four. Treat ≥25 min as the
floor for the full four-directory scope, not a budget for it, and expect the
number to move with the box's core count.

That four-directory scope is **the 4 directories R3 was proven on**,
deliberately not the runner's whole-tree default: a whole-tree run on a green
`main` reads ~142 failed, every one triaged environmental or pre-existing (see
[`planned/hermes-suite-perf.md`](docs/agent-runtime-harness/planned/hermes-suite-perf.md)
§Follow-ups). Two things sit OUTSIDE it and are named nowhere in it:
`tests/test_coverage_claims_resolve.py` and all of `tests/scripts/`. They are
section 3 of the unattended report below — the only lane that runs them
without someone typing them.

Run these through `scripts/run_tests.sh`, never `pytest` directly. That is not
style: the updater tests inside that scope do `git branch -f main origin/main`,
and a plain `pytest tests/hermes_cli` in the primary checkout detached 11
unpushed commits on 2026-08-01. Per-file hermetic subprocesses are the
mitigation; re-verified 2026-09-02 from a linked worktree with refs, reflog and
worktree registrations byte-identical before and after. The runner finds the
canonical shared test venv on its own (`$HERMES_TEST_VENV`, else
`~/.venvs/hermes-test`) — a worktree with no `.venv` of its own needs nothing
set.

### Unattended reporting

Pushes are instant now (2026-09-03 ruling: both repos' pre-push hooks are
gone, hermes `504953f6ad`) and hermes' own CI is largely inert (billing), so
the checks that used to run on every push to `release` — the validated suite
and any mutation-claim drift — now run only when someone happens to run them
by hand. Nothing catches a `main` that goes red between two people's runs, or
a refactor that silently moves a claimed line off its claimed source spelling
(`scripts/changed_line_mutation_check.py` needs its `find` needle to still be
present at the claimed symbol — see the mutation-gate rows in the Mission
Control queue).

`scripts/unattended_suite_run.ps1` is a REPORT, not a gate — nothing consumes
its exit code besides Task Scheduler's own run history and whoever reads the
file it writes. It runs three things and writes one dated Markdown report to
`qa-artifacts/unattended-suite-<UTC timestamp>.md` (git-ignored; the
directory itself is kept via `qa-artifacts/.gitkeep`), plus the raw stdout of
each command beside it:

1. `scripts/run_tests.sh` on the validated four-directory scope (see above —
   never the whole-tree default).
2. `scripts/changed_line_mutation_check.py --list --base origin/main` — the
   inventory lane. `--list` never mutates the tree (it returns before the
   mutating section of that script runs), so this is safe to run unattended
   and on any schedule without the `.mutation_gate.lock` concerns a real
   mutating run has.
3. `scripts/run_tests.sh tests/test_coverage_claims_resolve.py tests/scripts`
   — the two scopes that sit OUTSIDE the four directories in 1 and are
   therefore run by nobody. Added 2026-09-04 after the coverage-claim gate
   went red on `main` by five S2-wave citations with no lane reporting it.
   Its own section and not extra roots on 1: 1's scope is the RULED one, and
   widening it quietly would make "the validated suite" mean something the
   ruling does not cover.

`scripts/hermes-unattended-suite-task.xml` is a Windows Scheduled Task
definition that calls it on a cadence. **Nothing in this repo registers it.**
The operator enables it by hand — edit its two `REPLACE-ME` markers (point
them at the primary checkout, not a worktree) and either import it
(`schtasks /Create /TN "Hermes Unattended Suite" /XML
scripts\hermes-unattended-suite-task.xml`) or use it as a template in Task
Scheduler's GUI. The XML's own header comment carries the full run-book.

### The hermes CLI contract dump

`scripts/dump_cli_contract.py` walks this repo's argparse tree and gates
`tests/fixtures/hermes_cli_contract.json` on it.

```bash
python scripts/dump_cli_contract.py --check   # the gate; run it after any argparse change
python scripts/dump_cli_contract.py --write   # regenerate after a parser change
```

It exists because the launcher's Mission Control checks every operator button's
argv against a dump of these parsers **committed in the launcher** — so a
hermes-side argparse change leaves every launcher test green while that fixture
lies, and only a hand-run refresh notices. Measured: the `--message` deletion on
`persona instance create` (`ab6254643`) left it stale three days across five
hermes commits. A hand-run regen is not a mechanism. Now the repo that MOVED is
the repo that goes red.

**When this gate reds, read the diff before regenerating.** A removed command or
flag is not a fixture update — it is a launcher operator button that now exits 2.
Re-sync the launcher's own fixture in the same wave and record the sync in its
`tool/hermes_cli_contract/README.md`.

### The character payload contract dump

`scripts/dump_payload_contract.py` runs this repo's `harness characters`
producers and gates `tests/fixtures/charsheet_payload_contract.json` on the key
paths they emit.

```bash
python scripts/dump_payload_contract.py --check   # the gate; run it after any payload change
python scripts/dump_payload_contract.py --write   # regenerate after a producer change
```

The same hole, one repo over: the launcher compares every character payload key
against a copy of this document **vendored in the launcher**
(`tool/charsheet_payload_contract/`), by default-deny, so a hermes-side producer
move left every hermes test green while that copy lied. Three moves landed blind
that way — `handednessAccepted` added (`34a8dad32e`, which threw for every
character on every machine with hermes installed), `cardSafe` removed
(`4659127eba`, which left every live crop read as unjudged), the conditional
`sheet` slot added (`a4f8e62af7`). Now the repo that MOVED is the repo that goes
red. The gate and its round-tripped failure are pinned by
`tests/hermes_cli/test_payload_contract_dump.py`.

**When this gate reds, read the diff before regenerating.** A REMOVED key is the
dangerous half: an added key the launcher does not know about throws, a removed
one leaves it acting on a stale default. Re-vendor
`tool/charsheet_payload_contract/` in the same wave and record the new sha256 in
its README.




**Full-suite default (ruled 2026-09-01, hermes-suite-perf R3):** the per-file
parallel runner at its adaptive default of **8 workers** IS the full-suite
lane; plain serial `python -m pytest <dirs>` is the exception, kept for
single-file debugging and for the `tests/integration` / `tests/e2e` /
`tests/docker` suites the runner deliberately skips (they need real external
services and run in their own dedicated jobs — name them explicitly, e.g.
`run_tests_parallel.py tests/e2e`, when you do want them). Do NOT raise
`HERMES_TEST_WORKERS` past 8: the 12-worker probe measured slower (23:55 vs
17:36) AND load-flaked 2 tests that are serially green (field notes §14 of
`docs/agent-runtime-harness/planned/hermes-suite-perf-field-notes-2026-09-01.md`).
Any parallel-only failure is compared as a SET against a serial confirmation
run of just that file before it is believed. Two knobs worth knowing:
`HERMES_TEST_TMP_ROOT` (point it at a dedicated, Defender-excluded throwaway
dir — the suite's temp moves under it; forwarded through `run_tests.sh`'s
hermetic env) and the fact that `tests/acp` cannot collect from a git
worktree (`No module named 'acp'`; the editable install resolves to the
primary checkout — run it from the primary, or name your lanes explicitly).

**A wait bound above 30 seconds MUST declare its own `pytest.mark.timeout`.**
`pyproject.toml`'s `addopts` carry a repo-wide per-test `--timeout=30`
(`--timeout-method=thread`). A test whose own bound exceeds it can never
report: pytest-timeout kills it first and prints a thread dump where the
test's message would have been, so the run says "hung" about a test that was
about to say exactly what went wrong. That is the trap under the obvious
repair for a wall-clock flake — RAISING the bound trades one bad failure mode
for a worse one unless the test also carries `@pytest.mark.timeout(N)` with
`N` comfortably above the new bound (module-wide:
`pytestmark = pytest.mark.timeout(N)`). Worked examples in the tree:
`tests/hermes_cli/test_active_sessions.py` and
`tests/hermes_cli/test_relay_shared_metrics.py`, both repaired this way in
`99c8fa5725`, and `tests/scripts/test_doc_cite_report.py`, which carries the
marker with the measurement that sized it written beside the constant.

There is deliberately NO gate for this, and the reason is worth knowing before
someone writes one: a scan for numeric wait bounds over `tests/` flags 51
modules (measured 2026-09-04), and nearly all of them are SAFETY VALVES — a
`subprocess.run(..., timeout=60)` wrapped around a call that normally returns
in two seconds is not a wait the test expects to reach. Telling a valve from a
bound is semantic, so the honest form of this rule is the paragraph above and
not a literal scan carrying an allowlist.

**Validated scope vs. what the runner discovers by default.**
`scripts/run_tests_parallel.py` default-discovers the WHOLE `tests` tree
(`_DEFAULT_ROOTS = ['tests']`, minus the integration/e2e/docker skips above),
but R3's parity and integrity proof — the evidence the 8-worker default is
ruled on — was run against **exactly four directories**:

```
tests/agent_runtime tests/hermes_cli tests/cli tests/state
```

That four-directory set is what "the validated suite" means everywhere else
in this doc. It used to be a push-gate lane; the hook is gone (`504953f6ad`)
and the scope outlived it. A bare
whole-tree invocation on a green `main` is not a wider proof of the same
thing — it is a **different, unvalidated scope**, and on this workstation
(`de710d0b89`, 2026-09-01) it read 31,063 passed / 142 failed. Every one of
those 142 triages into an environmental class, not a code defect:

- **Provider-network hangs** — a test that reaches a real model-provider
  endpoint has no such endpoint reachable outside a network-shaped CI runner
  and hangs or times out instead of failing fast. Not fixable by retrying;
  the test's own environment gate (an API-key check, a `pytest.mark.skip`) is
  what is supposed to keep it out of a hermetic run, and a red here usually
  means that gate is missing or too narrow.
- **WSL-bash PATH shadow** — on a Windows box with WSL installed, `bash` on
  `PATH` can resolve to WSL's bash ahead of Git Bash. Anything that shells
  out expecting Git Bash semantics (path translation, line endings, the
  installed toolchain) gets WSL's instead and fails in ways that look like a
  code defect but are a PATH-ordering fact of that machine.
- **`acp` / `ripgrep` dependency holes** — `tests/acp` cannot collect at all
  outside the primary checkout (`No module named 'acp'`; the editable install
  resolves there, not to a worktree — see the bullet above), and a subset of
  tests need a real `ripgrep` binary on `PATH` that a fresh checkout or a
  worktree may not have. Both are "this environment does not have the tool
  the test needs," not a code regression.

Widening the validated scope past these four directories is a scope decision
with an open row (Mission Control queue, "the ruled default's SCOPE is wider
than its proof"), not something a hook or a doc edit does unilaterally.
Details and the full triage: `docs/agent-runtime-harness/planned/hermes-suite-perf.md`
§"Follow-ups this diagnosis surfaced" (residual 2026-09-01).
