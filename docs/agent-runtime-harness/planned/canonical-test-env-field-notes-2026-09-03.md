# Canonical test environment — field notes, 2026-09-03

The record for the pre-dispatch blocker of the launcher plan
`docs/mission_control/planned/same-account-instant-pairing.md` §7 ("Before
dispatch"): *hermes-agent has no canonical test environment, so
`scripts/run_tests.sh` has no interpreter and every test wave works around it
ad hoc.*

Built on branch `ops/canonical-test-env` in the worktree `X:\wt\test-env`. The
primary checkout `X:\Eternia\hermes-agent` was not touched — no edits, no
staging, no git state changes — and nothing was pushed.

Three things landed, and only the first is the row as written. The second and
third are what fell out of doing it honestly: a runner that can FIND the
environment without being told, and a fence that was inert on the very path
this row makes canonical.

---

## 1. The environment: `X:\Eternia\.venvs\hermes-test`

### Why outside every checkout

A per-checkout `.venv` is the right answer for one checkout and the wrong
answer for a machine that carries several. Measured on this box:
`git worktree list` reports **22 checkouts** of this repo (the primary plus 21
worktrees) and **zero of the 22 have a `.venv` or a `venv`** — the primary
`X:\Eternia\hermes-agent` included. So `scripts/run_tests.sh` refused from
every one of them, and every wave hand-carried
`HERMES_PYTHON=/c/Python312/python.exe`. The right unit for this environment
is therefore the machine, not the checkout: 22 identical venvs is not a fix,
it is the same fix 22 times and 22 chances for them to drift.

The venv therefore lives on the store volume beside the checkouts and the live
install, not inside any of them:

```
X:\Eternia\.venvs\hermes-test        <- new, shared by every worktree
X:\Eternia\.hermes\venvs\hermes-agent <- the LIVE install (read only, here)
X:\Eternia\hermes-agent               <- primary checkout
X:\wt\<name>                          <- worktrees
```

### What "the pins" were, and why the system interpreter was not them

`HERMES_PYTHON=/c/Python312/python.exe` — what the waves used — is a general
purpose grab-bag, and it does not agree with the environment the product
actually runs in. Measured side by side, both are Python 3.12.5:

| | live venv (`X:\Eternia\.hermes\venvs\hermes-agent`) | system `C:\Python312` |
|---|---|---|
| distributions | 85 | 200+ |
| `packaging` | **26.0** (the repo pin) | **26.2** |
| `mcp` | **1.26.0** (the `[dev]` pin) | **1.28.1** |
| `starlette` | **1.6.0** | **1.0.0** |
| `openai` | 2.24.0 | 2.41.0 |
| `pywin32` | 311 | 312 |
| `requests` | 2.33.0 | 2.31.0 |
| `rich` | 14.3.3 | 15.0.0 |
| `pytest` | absent | 9.0.3 |
| plus | — | `anthropic`, `boto3`, `modal`, `numpy`, `torch`-adjacent, `UnityPy`, `yt-dlp`, … |

That is not one test environment with a gap, it is a different environment. An
earlier wave already paid for this once: it pulled `packaging` 26.3 and
shadowed the pinned 26.0. The new venv is built from the LIVE venv's freeze so
this cannot recur — `packaging==26.0` is pinned there and is what got installed.

### The recipe, exactly as run

```
X:\Eternia\.hermes\venvs\hermes-agent\Scripts\python.exe -m pip freeze  > live-freeze.txt
grep -v '^-e ' live-freeze.txt                                          > req-base.txt
C:\Python312\python.exe -m venv X:\Eternia\.venvs\hermes-test
X:\Eternia\.venvs\hermes-test\Scripts\python.exe -m pip install \
    -r req-base.txt -r req-test-only.txt
```

`req-test-only.txt`, the whole delta, taken from `pyproject.toml`'s `[dev]`
extra:

```
pytest==9.0.3          # and the [dev] pin MOVED to match — see below
pytest-asyncio==1.3.0
pytest-timeout==2.4.0  # REQUIRED: addopts passes --timeout unconditionally
setuptools==81.0.0
ruff==0.15.10
ty==0.0.21
```

Result: 92 distributions, `pip check` clean, and the freeze diff against the
live venv is exactly the six lines above plus `iniconfig==2.3.0` and
`pluggy==1.6.0` (pytest's own dependencies). **Nothing else moved.** That diff
is the artifact worth keeping: it says the test environment is the runtime
environment plus a test runner, and nothing else.

### The one line dropped from the freeze, on purpose

```
-e git+https://github.com/nekwo/hermes-agent@504953f6ad...#egg=hermes_agent
```

The live venv installs the package editable, and its finder
(`__editable___hermes_agent_0_19_1_finder.py`) maps every module to
`X:\Eternia\hermes-agent\...` — the PRIMARY checkout. Installing that into a
SHARED venv would mean: run a worktree's suite, import the primary checkout's
`hermes_cli`. A green run that tested code you are not editing is worse than a
refusal. Verified the drop is safe rather than assumed:

```
cd X:\wt\test-env
<test venv>\Scripts\python.exe -c "import sys; sys.path.insert(0,'.'); \
    import hermes_cli.main; print(hermes_cli.main.__file__)"
-> X:\wt\test-env\hermes_cli\main.py
```

`tests/__init__.py` exists, so pytest's rootdir insertion makes the repo root
the import base and the RUNNING tree wins. `pywin32` also needed checking
(its `.pth` normally wants a post-install step): `import win32api` works.

### pytest: the `[dev]` pin moved to 9.0.3 (operator ruling)

The venv got 9.0.3 deliberately — it is what the ad-hoc waves have actually
been running under (`C:\Python312` carries 9.0.3), so this environment
reproduces their results instead of introducing a second variable in the same
change. That left `pyproject.toml`'s `[dev]` extra pinning 9.0.2, i.e. CI on
one version and every local run on another, which was filed as an open
question rather than decided unilaterally.

**Ruled: move the pin.** `[dev]` is now `pytest==9.0.3`, so the declared pin,
the canonical venv and what the waves ran are one version. CI's
`uv sync --locked --extra dev` needs the lockfile to agree, so `uv.lock` was
regenerated — `uv lock` reported exactly `Updated pytest v9.0.2 -> v9.0.3`
and the diff is that one specifier plus pytest's own version/sdist/wheel
hashes. Nothing else in 251 resolved packages moved, and `uv lock --check`
exits 0. Verified first that nothing else hardcodes the version: no test and
no workflow mentions `pytest==` or `9.0.2`.

`pyproject.toml` was edited in place on its existing line, so no line numbers
shifted and no doc cite needed re-pointing.

---

## 2. The runner: `scripts/run_tests.sh` finds it with nothing set

The probe list was `.venv`, `venv`, `~/.hermes/hermes-agent/venv`. It is now:

1. `$REPO_ROOT/.venv`
2. `$REPO_ROOT/venv`
3. `$HERMES_TEST_VENV` — explicit, when set
4. `$HOME/.venvs/hermes-test` — the default
5. `$HOME/.hermes/hermes-agent/venv` — the release venv (no pytest; skipped)

The order is the claim, and `tests/scripts/test_run_tests_script.py
::test_the_local_venv_still_outranks_the_shared_canonical_one` pins it by
index rather than by presence. **Appended, never prepended**: a checkout with
its own `.venv` keeps using it, and CI — which creates one — is byte-for-byte
unchanged. Every new candidate goes through the same already-existing
"pytest must be INSTALLED, not merely present" check, so an absent or
pytest-less shared venv is skipped exactly like the release venv rather than
selected and then failing every file with "No module named pytest".

`HERMES_PYTHON` still works, unchanged, as the last resort.

### No site-local path is committed (operator ruling)

The probe first carried `X:/Eternia/.venvs/hermes-test` as a literal candidate,
which worked and was filed as an open question: a machine-specific absolute
path in a shared script is a fact about one workstation that everyone else
reads past, and it rots silently when that machine changes.

**Ruled: keep the probe portable and link the venv into place.** The literal
is gone; every probed candidate is now `$REPO_ROOT`-, `$HOME`- or
`$HERMES_TEST_VENV`-relative. The venv still lives on the store volume beside
the checkouts and the live install, and is reached through a directory
junction created once on this box:

```powershell
New-Item -ItemType Directory -Force -Path "$env:USERPROFILE\.venvs"
New-Item -ItemType Junction -Path "$env:USERPROFILE\.venvs\hermes-test" `
         -Target "X:\Eternia\.venvs\hermes-test"
# FullName                          LinkType Target
# C:\Users\beast\.venvs\hermes-test Junction X:\Eternia\.venvs\hermes-test
```

A *junction*, not a symlink, on purpose: it needs no elevation and it crosses
volumes (`C:` → `X:`), which a hardlink cannot. Git Bash's `$HOME` is
`/c/Users/beast`, i.e. exactly `%USERPROFILE%`, so candidate 4 resolves
through it with nothing set. Verified end to end:

```
$ "$HOME/.venvs/hermes-test/Scripts/python.exe" -c \
      "import pytest, sys; print(pytest.__version__, sys.prefix)"
9.0.3 C:\Users\beast\.venvs\hermes-test
```

and then the runner itself, from the worktree with `HERMES_PYTHON` and
`HERMES_TEST_VENV` unset — it reports `▶ venv:
/c/Users/beast/.venvs/hermes-test` (see §4). `sys.prefix` reads as the junction
path rather than the target; that is expected and harmless, since `pyvenv.cfg`
and `site-packages` resolve relative to the prefix and the junction maps them
transparently.

`tests/scripts/test_run_tests_script.py
::test_no_machine_specific_venv_path_is_committed_in_the_runner` is what stops
the literal coming back: it walks every `VENV_CANDIDATES` line and rejects any
containing `:/` or `:\`. Drive letters in *comments* stay allowed and are
deliberate — the fence block records the real store root it was measured
against.

Two other runner changes, both about being able to read a result afterwards:

- **It now says which venv won** (`▶ venv: /c/Users/beast/.venvs/hermes-test`).
  With a shared candidate in the list, "the suite was green" is only a fact
  once you know which pins produced it, and a worktree's run now uses an
  environment that is not inside it. With the venv behind a junction this
  line is also the only place the indirection is visible, which is a second
  reason to print it.
- **The refusal lists every candidate it probed**, instead of naming two of
  them and telling you to enter a Nix devShell. It also points at
  `HERMES_TEST_VENV` in preference to `HERMES_PYTHON`, because the former
  takes a *venv* (whose pins are that venv's) where the latter takes any
  interpreter — including a system one whose site-packages shadow the repo's
  pins. That preference is the whole lesson of §1 written where the next
  person hits it.

---

## 3. The sibling defect: the gateway fence's real-store arm was inert

### What it claimed

`tests/hermes_cli/_gateway_fence.py`'s L3 backstop refuses, among other
things, "any spawn whose argv or `HERMES_HOME` names the operator's real store
root" — the second half of the measured 2026-08-31 escape, where an `atexit`
handler started *the operator's* gateway, not merely *a* gateway. It learned
that root from the production resolver:

```python
_REAL_ROOT = Path(get_default_hermes_root()).resolve()   # reads HERMES_HOME
```

### What it actually did under the canonical runner

`run_tests.sh` execs `env -i` and does not forward `HERMES_HOME` — correctly,
because `tests/conftest.py` must install its own hermetic home. So in the
child: no `HERMES_HOME`, conftest mints a throwaway session home, the fence
imports *after* that, and the resolver hands it the tempdir.

Measured on this workstation 2026-09-03, running the fence's own import order
(`tests.conftest` first, then the fence) under the two environments:

| | `_REAL_ROOT` | `classify(["hermes","config","get","model"], env={"HERMES_HOME": r"X:\Eternia\.hermes\profiles\alice"})` |
|---|---|---|
| **before**, runner env (no forwarding) | `C:\Users\beast\AppData\Local\Temp\hermes-test-home-g_quyxlh` | **ALLOWED** |
| **before**, bare pytest (operator shell) | `X:\Eternia\.hermes` | REFUSED |
| **after**, runner env | `X:\Eternia\.hermes` | **REFUSED** |

So the arm was comparing argvs against a directory that had existed for a few
milliseconds and would never appear in one. The three tests that drive it
(`test_classifier_refuses_a_hermes_run_pointed_at_the_real_store`,
`test_the_agent_browser_capability_probe_is_not_refused`,
`test_only_the_version_probe_itself_is_exempt`) all build their argv *from*
`real_root()`, so they passed against the tempdir and reported nothing. The
defence existed only on the path the runner's own header tells you not to use.

This is the same shape as the `${SystemRoot:+…}` bug already recorded in this
script — "the exact class of dead guard it was written to prevent" — and the
same shape as the `HERMES_HEAD_HOME` incident: a guard whose input came from
the ambient environment, on a path that deliberately has no ambient
environment.

### The fix, and the two things it is deliberately NOT

`run_tests.sh` computes the root before dropping the env and forwards it under
a dedicated name:

```bash
REAL_HERMES_ROOT="$("$PYTHON" -c 'import sys; sys.path.insert(0, "."); \
  from hermes_constants import get_default_hermes_root; print(get_default_hermes_root())' \
  2>/dev/null || true)"
...
  ${REAL_HERMES_ROOT:+HERMES_TEST_REAL_ROOT="$REAL_HERMES_ROOT"} \
```

and `_real_hermes_root()` reads `HERMES_TEST_REAL_ROOT` first, falling back to
the production resolver for a bare-pytest run.

**Not `HERMES_HOME`.** Forwarding that would put the live store back in front
of every test — the hazard, not the guard. `HERMES_HOME` stays unset in the
child and conftest still installs the per-test temp home; the before/after
table above shows `HERMES_HOME` resolving to a fresh tempdir in both columns.

**Not `HERMES_REAL_HOME`.** That name was the obvious candidate and is wrong
twice over: it is a *production* variable (`hermes_constants.py:1004`
`_iter_real_home_candidates`, whose first candidate it is — the OS-user home
an ACP child inherits, not a
store root), and `tests/conftest.py` blanks it per test on purpose, with
`tests/test_hermetic_env_blanking.py` gating that it stays blanked. Reusing it
would have been a collision that one of those two facts would have punished.
The new name follows the existing test-only convention `HERMES_TEST_TMP_ROOT`.

**Explicit must win over the fallback**, not merge with it. Under the runner
the fallback names the session's *hermetic* home; treating that as the
forbidden root would refuse tests for using the sandbox they were given.

Computed with the probed venv's python calling the production resolver rather
than re-derived in shell, so the "which profile dir belongs to which root"
unwrapping keeps exactly one implementation. Fail-soft throughout: if the
probe prints nothing, the variable is not forwarded and the fence falls back
to today's behavior.

### Pinned

- `tests/scripts/test_run_tests_script.py
  ::test_run_tests_hands_the_gateway_fence_the_real_store_root` — asserts the
  forwarding line is present AND that `HERMES_HOME=` does not appear in the
  `exec env -i` block, so "fix it by forwarding HERMES_HOME" cannot pass.
- `tests/hermes_cli/test_gateway_spawn_fence.py
  ::test_the_real_store_root_comes_from_the_runners_env_var_first` — explicit
  wins, absent falls back, and the name is not `HERMES_REAL_HOME`.

`tests/conftest.py`'s comment claiming the runner "forwards only
HERMES_RUN_SLOW_PET_TESTS and HERMES_E2E_BROWSER" was already stale before
this change (`HERMES_TEST_TMP_ROOT` landed 2026-09-01) and is corrected, with
a note on why a variable carrying the operator's real root into the hermetic
env is not a hole in the sandbox: nothing production reads it; it is the
FORBIDDEN path, handed to the fence so the fence can refuse it.

---

## 4. Verification

Both runs from `X:\wt\test-env` with `HERMES_PYTHON` and `HERMES_TEST_VENV`
explicitly unset, i.e. proving the probe finds the shared venv on its own.

### Targeted lane

```
scripts/run_tests.sh tests/scripts \
    tests/hermes_cli/test_cli_contract_dump.py \
    tests/hermes_cli/test_gateway_spawn_fence.py \
    tests/hermes_cli/test_gateway.py \
    tests/hermes_cli/test_gateway_verbs.py \
    tests/hermes_cli/test_gateway_windows.py -j 6
```

`▶ venv: X:/Eternia/.venvs/hermes-test`,
`▶ real store root handed to the gateway fence: X:\Eternia\.hermes`, then
**21 files, 192 tests passed, 0 failed in 40.0s** (6 workers; subprocess CPU-wall
172.6s, P50 6.00s, max 31.38s on `test_doc_cite_adjacency.py`).

One red on the first attempt, and it was mine: adding the comment to
`tests/conftest.py` shifted `_shared_monkeypatch_pin_tripwire` from line 594 to
603, and `docs/agent-runtime-harness/06-office-and-board.md:1060` cites it by
line. `tests/scripts/test_doc_cite_adjacency.py` caught it as the single
unwaived failure, naming the doc, the cite and the symbol. The citation was
updated; the gate then reported 0 unwaived and 0 stale waivers. Worth
recording that the gate worked exactly as designed on a change that had
nothing to do with docs.

### Full validated lane

```
scripts/run_tests.sh tests/agent_runtime tests/hermes_cli tests/cli tests/state
```

**1100 files, 12715 tests passed, 1 failed, in 1968.9s (32.8 min, 8 workers.)**

Two files went red during the run; one of them un-redded itself and the other
is not this branch's.

**`tests/cli/test_worktree.py` — a load timeout, cleared by the runner's own
retry.** `test_single_and_multi_worker_agree` blew the 30s per-test
`pytest-timeout` cap inside a `subprocess.run` of `git`, at 6-way parallelism.
It is timeout-shaped, not assertion-shaped, and the stack ends in
`threading.Thread.start` waiting on a lock — not in a `GatewayFenceViolation`.
The test builds *two* eight-worktree boards with real `git worktree add`,
commit, cherry-pick and `update-ref` calls, i.e. dozens of git subprocesses,
so it is load-sensitive by construction. `run_tests_parallel.py` re-ran it at
1-worker isolation (its single bounded retry) and it **passed**, which is what
takes it out of the final count. Recorded rather than ignored: this is the
file to look at first if the suite is ever run on a busier box.

**`tests/hermes_cli/test_web_server.py` — pre-existing, and left unfixed on
purpose.** `TestBuildSchemaFromConfig::test_no_single_field_categories`:

```
AssertionError: Category 'charsheet' has only 1 field(s) — should be merged
```

Classified rather than assumed, in three steps:

1. Neither `hermes_cli/web_server.py` nor `tests/hermes_cli/test_web_server.py`
   is in this branch's diff (`git status` on both: clean).
2. The assertion's only input is `DEFAULT_CONFIG`. `charsheet` is a top-level
   section in `hermes_cli/config_defaults.py:3094` holding exactly one key,
   `provider_timeout_seconds`, and `_CATEGORY_MERGE`
   (`hermes_cli/web_server.py:988`) has no entry folding it into a bigger
   category — so `_build_schema_from_config` emits a one-field category and
   the test rejects it. Nothing in that path reads an env var, an installed
   package or a venv.
3. It arrived in `0c9fb95410` (2026-09-02, *"feat(charsheet,serve): one writer
   per draft…"*), and `git merge-base --is-ancestor 0c9fb95410 HEAD` confirms
   that commit is an ancestor of this branch's HEAD. The red predates the
   branch by a day.

So it is red on `main` as committed, not caused here, and under this lane's
"fix only your own reds" constraint it is **rowed, not fixed**. The fix is
almost certainly one line — `"charsheet": "agent"` in `_CATEGORY_MERGE`, since
the config block's own comment calls these "Character-sheet authoring
(agent/charsheet/). Behavioural knobs only" and the agent-adjacent sections
(`context`, `skills`, `cron`, `checkpoints`, `goals`) already fold that way —
but "one line" is not a licence to widen a diff that is about the test
environment.

### Re-verification after the two rulings

The pin bump, the junction and the probe-list change all landed *after* the
targeted run above, so the affected files were re-run — and this is also the
end-to-end proof that the junction works, since nothing points at the venv any
more:

```
$ cd X:\wt\test-env
$ env -u HERMES_PYTHON -u HERMES_TEST_VENV \
      bash scripts/run_tests.sh tests/scripts tests/hermes_cli/test_gateway_spawn_fence.py -j 6
▶ venv: /c/Users/beast/.venvs/hermes-test
▶ real store root handed to the gateway fence: X:\Eternia\.hermes
=== Summary: 17 files, 156 tests passed, 0 failed (100% complete) in 58.9s (6 workers) ===
```

`▶ venv:` names the `$HOME` path, not the `X:` target: the probe carries no
site-local literal and still lands on the canonical environment.

---

## Rulings taken during the lane

Two items were filed as open questions and ruled by the operator before the
commit. Both are applied; recorded here because the *reasoning* is what the
next environment change needs, not just the outcome.

1. **The `[dev]` pytest pin moves to 9.0.3**, so the declared pin, the
   canonical venv and every past wave's numbers are one version rather than
   two. Applied in `pyproject.toml` with `uv.lock` regenerated (§1).
2. **No workstation-specific literal in the repo script.** The probe stays
   portable and this box links the venv into `~/.venvs/hermes-test` with a
   directory junction (§2), with a test to keep the literal out.

## Open questions for the operator

1. **Rebuilding the shared venv when the live install's pins move.** Nothing
   detects drift today. The recipe is in the probe comment in
   `scripts/run_tests.sh` and in §1 above; a `scripts/` helper that rebuilds
   from the live freeze and diffs is the obvious follow-up and was not built
   (out of the stated scope: runner, fence/conftest, docs).
2. **The `agent-browser --version` exemption is now live for the first time.**
   The real-store arm was inert under `run_tests.sh`, so on that path the
   exemption was never exercised either. The full-lane result above is the
   first measurement of both together, and it is green.

   Read this one alongside `main`, which moved while this lane ran: this
   branch was cut at `504953f6ad`, and `7b4a985261` ("fix(doctor): stub the
   browser resolver so tests stop spawning agent-browser") has since landed a
   test-side seam and re-measured the exemption — the 19 `test_doctor.py` reds
   are gone, but `dep_ensure.py`'s `_DEP_CHECKS` and `nous_subscription.py`
   still call `agent_browser_runnable` off `shutil.which` with no seam, so the
   exemption stays. Its notes are in
   `w10-hermes-field-notes-2026-09-03.md`. The two changes touch different
   regions of `_gateway_fence.py` and `git merge-tree` reports no conflict, but
   whoever lands this should read the merged comment block as one piece rather
   than assume both halves still say what they said apart.
