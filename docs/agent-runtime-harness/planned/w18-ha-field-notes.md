# w18/ha field notes — the CI reds of slices 1 to 4 (2026-09-06)

Lane `ha` of wave 18. Scope: the 16 test failures across 11 files in slices 1 to 4
of CI run `33969282189`, the first main-push run whose slices reached a test since
2026-08-04. Slices 5 to 8 are lane `hb`; the three non-test jobs are lane `hc`.
The CI log was read as the oracle for every one of them
(`gh run view --repo nekwo/hermes-agent --job <id> --log-failed`; job ids
`101314848597` / `101314848630` / `101314848583` / `101314848658`).

One commit per file, eleven in all.

## The count, and what the log actually said

The row's read of 16 tests over 11 files is exact for these four slices. Two
things sat beside them that are NOT in the row and are NOT fixed here:

- three FLAKY files — failed once, passed on retry. Slice 2
  `tests/agent_runtime/test_serve_stream_lane_parity.py`, slice 3
  `tests/run_agent/test_run_agent.py::TestExecuteToolCalls::test_sequential_tool_calls_run_without_delay`
  and `tests/tools/test_browser_eval_supervisor_path.py::TestEvaluateRuntimeDomNodeCrashRetry::test_reference_chain_crash_retries_without_by_value`.
  The runner prints them under `⚠ FLAKY`, AGENTS.md calls a FLAKY report a bug to
  fix, and all three read as timing-sensitive. Unrowed today.
- two failures on this Windows box that are green on CI and are environmental
  here: `tests/tools/test_image_generation.py::TestManagedGatewayErrorTranslation`
  (2 tests, `tools/fal_common.py:50 ImportError` — the optional `fal` client is
  not in the shared test venv), and four `tests/test_install_*` files whose reds
  are byte-identical before and after this lane's changes (baselined by stash).

## Six causes, and only two of them are Linux

The row's guess was "most read as Linux-vs-Windows assumptions". Measured, the
split is the other way round: **six of the eleven reproduce on Windows and were
simply never run**, because hermes CI had not reached a test since 2026-08-04 and
`tests/scripts/`, `tests/plugins/` and the root `tests/*.py` files sit outside the
four-directory validated scope that anybody runs by hand.

### 1. A patch that names a retired loader (2 files, both host-independent)

`96cfc09a34` moved two read paths off `load_config` onto `load_config_readonly`
(a `load_config()` at import time scaffolds the home — the eager-tool-discovery
audit). Two test files kept patching the retired name:

- `tests/tools/test_image_generation.py::TestModelResolution` — the fallback case
  stayed green because an unpatched empty home also answers "no config", so only
  the precedence case (config beats `FAL_IMAGE_MODEL`) red, and only where the env
  var is set. `188c511bae`.
- `tests/plugins/dashboard_auth/test_self_hosted_provider.py::TestPluginRegister`
  — every config value went nowhere. The env-only cases stayed green on their env
  vars, `test_env_overrides_config` could not tell "env wins" from "config never
  read", and the two config-only cases red with `'NoneType' object has no
  attribute 'args'` (the plugin skipped registration). `9765564afa`.

Both are the same lesson: a `patch()` target is a claim about the code under test,
and nothing in the tree checks that claim. A green run over a stale target is
silence, not evidence.

### 2. Text-mode newline translation, twice (both were the Windows accident)

- `test_persona_skill_policy.py` asserted the literal `preload 9 / 200 B` for a
  body of `"# small\n"`. Text mode makes that 9 bytes on Windows and 8 on Linux;
  CI read `preload 8 / 200 B`. The same test's *next* case already measured off
  the file and said why. `caffc04c1c`.
- `test_harness_tool_inventory.py` wrote every mutated artifact with a plain
  `write_text`, which re-writes the whole file CRLF on Windows while the check
  compares it against a rendering that came through universal newlines. Every
  mutation case therefore red on LINE ENDINGS, whatever it had mutated — the
  exact vacuity the module docstring says those cases exist to prevent. Under
  that cover, `test_a_mutated_skill_block_reds_the_check` was renaming the FIRST
  `` `agent_chat_send` `` in SKILL.md, which is prose on line 35 and the same
  occurrence `test_a_manual_that_names_an_unregistered_tool_reds` already owns.
  `splice_skill` regenerates only what lies between the markers, so that rename
  leaves the artifacts byte-identical and reds through `cross_check` with no
  DRIFT in stderr. Both repaired: the mutation lands inside the block, and all
  five writes carry `newline=""`. `9463780a78`.

### 3. Two real product duplicates / residues (host-independent)

- `agent_runtime/realm_sync.py::_ledger_time` and `store.py::_stamp` were
  byte-identical bodies. `realm_sync` already imports from `store`, so the body
  folded down to `store.ledger_time` (public, now that it has an out-of-module
  consumer) and `realm_sync` binds it under its own spelling — which keeps
  `tests/mutation_claims.json`'s `_ledger_time` needle intact rather than moving
  a claimed line off its claimed spelling. `43cbb598ef`.
- `scripts/install.ps1` carried a UTF-8 BOM on byte 0. Upstream's copy at
  `126ff7071` has none and no other `.ps1` in the tree has one; it arrived with
  the 2026-07-31 merge `b9721809e6`, so it is conflict residue. The test that has
  said so since `97249cfc8a` had never once been run by CI. `30f1ec6635`.

### 4. A shallow clone (Linux-only, but not a platform fact)

`test_doc_cite_report.py` resolved `HEAD~3` in the checkout it lives in.
`.github/workflows/ci.yml` passes no `fetch-depth`, so `actions/checkout`'s
default of 1 applies and the runner's clone holds ONE commit — `git rev-parse
HEAD~3` exits 128 there and works on every developer box. The assertion is about
`_classify_shas`, not about this checkout's history, so the fixture now builds a
throwaway four-commit repo and points `report.REPO_ROOT` at it. `48a690da4c`.

### 5. Three genuine Windows assumptions in production code or in a rule

- **`hermes_cli/windows_env._normalize_segment` used `os.path.normcase`**, which
  binds to `posixpath` off Windows where it is the identity — no casefold, no
  `/`→`\` fold. What it compares is always a Windows registry PATH. Production
  only reaches it behind a `winreg` gate, but the module's tests deliberately run
  everywhere through a fake `winreg` (its first docstring line), and on Linux they
  compared raw strings: `add_user_path_entry` re-added a segment already on PATH.
  Now `ntpath.normcase`. `ce3dc7f465`.
- **`agent_runtime/repo_context`'s support exclude was directory-only.**
  `_link_local_support_dir` makes a junction on Windows and an `os.symlink`
  everywhere else; git walks a junction as a DIRECTORY and records a symlink as a
  BLOB, and `.EterniaBackendVirtualEnv/` matches only the first. On POSIX the link
  stayed untracked, so every agent diff carried `?? .EterniaBackendVirtualEnv`
  and — through `_worktree_is_reapable`'s `git status --short` check — the
  count-cap GC never reaped anything. **Both** of that file's CI failures are that
  one character. `d2137867b7`.
- **`tests/test_hermetic_env_blanking.py`'s `HERMES_AUTH_HOME` witness** pointed
  at `hermes_cli/auth.py` after `e567a9ff00` routed that read through
  `hermes_constants.get_hermes_auth_home()`. Repointed, per the test's own
  instruction. `cee66b50fd`.

### 6. The one that was a design fault, not a repair

`tests/test_env_gap_registry.py::test_every_skip_row_still_describes_a_real_gap`
red on all four parametrisations and instructed the deletion of **all 52 rows** in
the four `_ENV_GAP_SKIPS` registries.

The probes are not wrong. Every one of them interrogates a mechanism by
measurement, exactly as `tests/_env_gap_fence.py` demands, and on the Linux runner
every one of them correctly answers "this gap is not present here". What was wrong
is the verdict `stale_skip_rows` draws from that: it reads "probe False" as "the
row rotted, delete it", which is a single-host reading of a ledger that describes a
different host. Acting on it would have dropped the fence for the host that has the
gaps — and the registries' own docstring already carries the honest form of the
rule ("probe FALSE -> the test RUNS, and **if it now passes** the ledger fails"),
whose second half the implementation never had.

Running the fenced nodes to supply that second half does not help either: on Linux
those nodes DO pass, so the docstring's own rule still says "delete", and the
contract turns out to be single-host by construction. So the scope is what moved.
The verdict is now issued only on a host where at least one row of any of the four
registries fires, asked across all four rather than per directory — per directory
would leave `tests/agent`'s single row unjudgeable the moment it went stale, which
I measured and closed. Where nothing fires, the ledger asserts that the ledger is
well formed instead (every probe answered a real `bool` on a host whose imports and
syscalls differ from the one it was written on). Neither branch skips. `23a90c6063`.

Stated limit, in the code: a registry holding rows for two host classes at once
cannot be judged this way. Measured 2026-09-06 — all 52 rows fire on the Windows
dev box and none fires on the Linux runner, so nothing is mixed today; splitting a
registry that becomes mixed is the repair.

## Reds and killing mutations

Every commit records the red it was written against and one killing mutation. The
four that needed the Linux host simulated on Windows did it by measurement rather
than by argument:

| file | red reproduced as | killing mutation |
|---|---|---|
| `test_doc_cite_report.py` | `git rev-parse HEAD~3` in a one-commit clone exits 128 | ancestry verdict forced to `"ok"` |
| `test_harness_tool_inventory.py` | old prose mutation written LF-preserving reproduces CI's stderr verbatim | `splice_skill` returns the committed text |
| `test_persona_skill_policy.py` | `_write_skill_package` writing LF-preserving reads `preload 8 / 200 B` | verifier reports `st_size + 1` |
| `test_windows_env.py` | the old helper under `posixpath.normcase` answers False | the `normcase` call dropped |
| `test_repo_context_observation.py` | the link materialized as a blob reproduces `?? .EterniaBackendVirtualEnv` | the trailing slash restored |
| `test_env_gap_registry.py` | four scenarios driven against the real registries | a probe returning `''`; `tests/agent`'s only row stale while the others fire |

## What the next reader should not have to rediscover

1. **A green test over a stale `patch()` target is silence.** Two of eleven were
   this, both from one refactor, and both would have been caught the day
   `96cfc09a34` landed by anything that ran the file.
2. **`write_text` in a test is a line-ending decision.** Where the code under test
   compares bytes, a plain `write_text` on Windows manufactures a difference and
   the assertion stops being about the mutation. `newline=""` is the default a
   fixture that plants bytes should have.
3. **`os.path` in code that reasons about the OTHER platform's paths is a bug on
   both hosts** — it is merely invisible on one of them. `ntpath` / `posixpath`
   are the spellings that mean what such code means.
4. **A gate whose verdict is host-relative has to say which host.** The env-gap
   ledger was correct code with an unstated premise, and the premise only became
   visible when a second host finally ran it.
