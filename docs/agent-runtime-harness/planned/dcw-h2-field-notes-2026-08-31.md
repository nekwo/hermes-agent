# W1-H2 field notes — test isolation: the gateway hole and the pollution reds

Running record for the decision-close wave's W1-H2 stage (hermes repo, branch
`feat/dcw-h2-test-isolation`). Started from `0c744aa586`, rebased onto
`301946bc57` once W1-H1 and W1-H3 landed. Everything below was measured on this
Windows 10 workstation on 2026-08-31 unless dated otherwise.

## 0. The gateway escape: found in 7 seconds, not 13 minutes

The plan said "find the path by which `tests/hermes_cli` reaches
`_cold_start_windows_gateway_after_update` unmocked". The 08-30 baseline had
caught it only as two stray lines at the end of a 738s run. Rather than repeat
that run, the whole chain was reproduced from ONE file with a recording probe
(a `-p` plugin wrapping `subprocess.Popen`/`run` and `atexit.register`
process-wide, refusing instead of executing):

```
tests/hermes_cli/test_update_autostash.py:187
  test_cmd_update_skips_stash_restore_when_reset_fails
    -> hermes_cli/main.py:9482        cmd_update
    -> hermes_cli/update_cmd.py:3234  _cmd_update_impl
         atexit.register(_resume_windows_gateways_after_update,
                         {"resume_needed": True, "profiles": {},
                          "unmapped_pids": [], "unmapped": [],
                          "cold_start_if_installed": True})
-- pytest process exit --
    -> update_cmd.py:2990 _refresh_windows_gateway_launchers
         run: C:\WINDOWS\system32\schtasks.EXE /Query /TN Hermes_Gateway_alice
    -> update_cmd.py:2851 _cold_start_windows_gateway_after_update
    -> gateway_windows.py:990 _spawn_detached
         Popen: C:\Python312\python.exe -m hermes_cli.main
                --profile alice gateway run
```

7.5s, one file. The argv is character-for-character the one the 08-30 baseline
recorded.

### Why every existing guard missed it

Three separate reasons, each a lesson of its own:

1. **`atexit` is a window no fixture covers.** `tests/conftest.py`'s
   `_live_system_guard` already classifies that exact argv correctly — its
   backend-spawn arm would have refused it. But it is an autouse *fixture*: the
   wrappers go on at setup and come off at teardown, and the caller here was
   parked on `atexit`. The guard was not wrong; it was not present.
2. **The child came up on the OPERATOR's profile, not a tempdir.** Two facts
   compound. The ambient environment on this box already has
   `HERMES_HOME=X:\Eternia\.hermes`, and `X:\Eternia\.hermes\active_profile`
   reads `alice`. Importing `hermes_cli.main` runs `_apply_profile_override()`
   at module scope (main.py:736), which follows that marker and rewrites
   `os.environ["HERMES_HOME"]` to the real `profiles/alice`.
   `_hermetic_environment` then redirects HERMES_HOME per test with
   `monkeypatch`, which restores the **pre-test** value at teardown — the real
   profile. So at interpreter exit the process was pointed at the operator's
   live store, and `is_installed()` found their real `Hermes_Gateway_alice`
   Scheduled Task.
3. **Nothing failed.** `_cold_start_windows_gateway_after_update` is
   best-effort and swallows its own exceptions. Even a crashing spawn would
   have printed nothing anyone would read.

### The fence

`tests/hermes_cli/_gateway_fence.py` plus three seats in the directory
conftest:

* **L1, the root cause** — autouse `_no_windows_gateway_pause_token` defaults
  `_pause_windows_gateways_for_update` to `None` for this directory, so the
  handler is never parked and no test reads this machine's gateway table or
  Scheduled Task. Five files here call `cmd_update`/`_cmd_update_impl` and only
  `test_update_venv_health.py` patched the seam. Same shape as the existing
  `_suppress_concurrent_hermes_gate`. Opt out with
  `@pytest.mark.real_windows_gateway_pause` — taken by exactly one test, the
  one that is ABOUT the pause, and which mocks every process-touching seam
  itself.
* **L2, the chokepoint** — `gateway_windows._spawn_detached` replaced at
  conftest import, never restored.
* **L3, the backstop** — `subprocess.Popen/run/call/check_call/check_output`,
  `os.system` and `os.popen` wrapped once at conftest import and never undone.
  Refuses a hermes backend argv (`gateway`/`serve`/`dashboard` after a hermes
  entry point), the `gateway.vbs`/`gateway.cmd` launchers, a mutating
  `schtasks` verb, and a hermes invocation pointed at the real store root.

Installed at conftest **import**, not in a fixture. That is the entire point.

### One over-refusal, and it is its own finding

L3's first cut refused any argv naming the real store root. In the full run
that fired 56 times on one command —
`X:\Eternia\.hermes\profiles\alice\node\agent-browser.CMD --version`, reached
from `hermes doctor` through a path resolved at import time out of the
operator's live profile — and turned 44 tests across 8 files red. A
`--version` probe starts nothing, so the arm was narrowed to argv that would
boot our own runtime. **The over-catch is a real find for a different lane:
this suite resolves an executable out of a live profile home.** It is pinned
as an allowed case with the reasoning written at the site, not deleted.

## 1. The pollution classes

Baseline (launcher notes §19, hermes `00fa94dd75`): 40 failed / 4327 passed,
39 of them order-dependent. Re-measured at `0c744aa586`: the same 40, same
modules, same tests. Nothing had drifted — only one commit touched these trees
in between.

Six named mechanisms. Two accounted for the bulk; the last three only became
visible once the first ones were fixed, which is itself the most useful thing
in this record.

### A — two `hermes_cli.main` module objects (16 reds)

`test_skills_subparser.py` does `del sys.modules['hermes_cli.main']` then
`import hermes_cli.main`, to prove the parser still builds, and never puts the
original back. The process then holds TWO `hermes_cli.main` namespaces:

* every test file that did `from hermes_cli.main import X` at COLLECTION time
  holds a function whose `__globals__` is the FIRST, now orphaned;
* `sys.modules["hermes_cli.main"]` is the SECOND, which is what
  `patch("hermes_cli.main.…")` and `monkeypatch.setattr(cli_main, …)` reach.

So the stub lands where the function under test never looks, and **production
runs for real**. The `npm error code EJSONPARSE` in the 08-30 baseline output
was not a mock misfiring — it was a genuine `npm run build`. Likewise every
`_cmd_update_impl` helper stub in `test_update_venv_health` silently did
nothing.

This is why the class is "deterministic, but only in a full run": it depends on
running after ONE file, not on timing, and alphabetical order does the rest.
One file, 16 reds — `test_web_ui_build`(8), `test_update_venv_health`(4),
`test_update_interrupted_recovery`(2), `test_update_autostash`(1),
`test_update_concurrent_quarantine`(1).

**Fix:** autouse `_sys_modules_identity_is_restored` restores the IDENTITY of
any module the session already had if a test replaced or dropped it. Two
halves, and the second was NOT optional:

* the `sys.modules` row, and
* **the parent package attribute.** `import hermes_cli.main` binds `main` on
  the `hermes_cli` package object, and `from hermes_cli import main` reads
  THAT, not `sys.modules`. With only the first half, `test_web_ui_build` went
  green and every update module stayed red — `update_cmd._m()` and the update
  tests both use the `from hermes_cli import main` spelling.

Deliberate non-coverage, each recorded at the site: newly imported modules are
LEFT alone (lazy imports are normal); a non-`str` key is skipped
(`test_setup_openclaw_migration.py` mocks
`importlib.util.spec_from_file_location`, so production's
`sys.modules[spec.name] = module` files a module under a MagicMock — tripping
over it ERRORed two green tests in `test_state_db_guard.py`).

### B — the dashboard stayed in OAuth mode for the rest of the run (4 reds)

`hermes_cli.web_server.app` is a module-level FastAPI instance, so `app.state`
is ONE mutable mapping for the whole session, and the auth middleware branches
on `app.state.auth_required`. The gate tests set it True and stop there on
purpose — the assertion that a fail-closed public bind RECORDS the flag is the
last statement. Everything downstream is then 401ed, because gated mode does
not honour the legacy session token, and the
`HEADERS = {"X-Hermes-Session-Token": _SESSION_TOKEN}` idiom is module-level in
a dozen files here. `test_dashboard_auth_gate.py` alone reds the 4 tests in
`test_env_custom_keys.py` + `test_env_export_line_lifecycle.py` the full run
reports. It reads as "the endpoint broke", never as "the process has been in
OAuth mode since three files ago".

**Fix:** autouse `_web_server_app_is_pristine` resets `app.state` to the
mapping as it stood the first time this directory saw the module.

**The reset is at SETUP, and that is load-bearing.** `monkeypatch` is
instantiated by the root conftest's first autouse fixture, so its undo runs
after anything declared here — and
`monkeypatch.setattr(app.state, "bound_port", 9119, raising=False)` undoes
itself with a `delattr`. A teardown-time restore removed that key first and
monkeypatch's undo died with `KeyError: 'bound_port'`, turning a green test
into a teardown ERROR. Resetting on the way IN leaves every pending undo intact
and gives the same guarantee.

### C — the xAI label fixture gap (1 red, not order-dependent)

The 40th red, and the only one that reproduced in isolation. `get_label("xai")`
has no `_LABEL_OVERRIDES` entry on purpose — that IS the claim being guarded:
`xai` takes its name from the models.dev catalog, `xai-oauth` from the override
table, and the bug was the two collapsing onto one string. The catalog resolves
`HERMES_HOME/models_dev_cache.json` and then the network; under the hermetic
home neither answers here, so `get_provider` falls to the Hermes overlay whose
name is `_LABEL_OVERRIDES.get("xai", "xai")` — the lowercase id. The file never
provided the catalog it reads. Seeded in a fixture; the OAuth half still comes
from production, so a real collapse still reds.

### D — reload keeps the module and replaces every class in it (1 red)

The reload sites were checked early and CLEARED, and that judgement was half
right in a way worth recording. `importlib.reload` re-executes into the SAME
module `__dict__`, so attribute patching still reaches a pre-reload function —
which is why reload is not behind class A. But it also mints a fresh object for
every CLASS in the module. A `from hermes_cli.main import _UpdateOutputStream`
binding taken at collection is then a different class from the one
`_install_hangup_protection` instantiates, and
`isinstance(sys.stdout, _UpdateOutputStream)` answers False —
`test_update_hangup_protection::test_wraps_stdout_and_stderr_with_mirror`,
reproducible from either reload site alone.

**Fix:** both reloads of `hermes_cli.main` were redundant and are gone.
`test_env_export_prefix`'s own comment already said `get_env_path()` derives
from HERMES_HOME at CALL time — which is why it patches nothing — and the
curator notice resolves its state at call time too. `hermes_constants` and
`agent.curator` stay reloaded: no red measured behind those. Dropping the two
lines also removes a second hazard they carried — a reload of `hermes_cli.main`
re-runs `_apply_profile_override()` and `load_hermes_dotenv` mid-test.

### E — `include_router` stacks a lifespan the route-restore never undoes (2 reds)

`app.include_router(r)` does not only append routes: FastAPI wraps
`app.router.lifespan_context` in a `merged_lifespan` that also enters `r`'s.
The two fixtures that remount plugin API routes mid-session
(`test_web_server.py`'s example-plugin fixture,
`test_project_plugin_rce_bypass.py`) restore `app.router.routes` and nothing
else, so the wrappers stack for the rest of the run. When one of those routers
is a MagicMock its lifespan yields an AsyncMock, and the next test to start the
app dies inside FastAPI's `{**(maybe_nested_state or {})}` with
`TypeError: AsyncMock.keys() returned a non-iterable (type coroutine)` — a
message naming nothing involved and pointing at site-packages.
`test_project_plugin_rce_bypass.py` alone reds
`test_web_server_boot_handshake.py::test_lifespan_warmup_is_synchronous`.

**Fix:** the pristine lifespan is restored beside `app.state`, on the way in.
Routes are deliberately left alone — the fixtures that append them already put
the list back, and resetting the list on the way in would drop a router
legitimately mounted by a later import.

### F — a path constant frozen at import (2 reds, only visible after B)

`PAIRING_DIR = get_hermes_dir("platforms/pairing", "pairing")` runs at module
scope in `gateway/pairing.py`, and a bare `PairingStore()` reads it. Every test
that builds one shares a single directory: whichever HERMES_HOME was current
the FIRST time some test imported the module — normally another test's tmp
home, long since deleted, and if that first import ever happens outside a
hermetic test, the live store's own `platforms/pairing`, which `__init__` will
`mkdir` and write pending/approved JSON into. `test_pairing.py` +
`test_gateway_pairing_verbs.py` ahead of `test_dashboard_admin_endpoints.py`
reds two of its pairing tests, because `data["pending"][0]` is another file's
leftover request (`assert 'global-1' == 'user1'`); reversed, all 54 pass.

**This red did not exist in the baseline.** While every dashboard pairing
request was 401ed by class B, the polluting rows were never written. One
pollution class was hiding another — which is why the count went 40 → 4 → 1
rather than straight to zero, and why "fix, re-measure, repeat" was the only
honest way through.

**Fix:** re-pinned per test — the treatment `tests/conftest.py` already gives
`hermes_state.DEFAULT_DB_PATH`, for exactly this reason.

## 2. Corrections to earlier readings

* **"The reload sites are not the cause" was half wrong.** True for attribute
  patching (class A), false for class identity (class D). Recorded rather than
  quietly amended, because the first bisect (reload site + `test_web_ui_build`,
  green) is what made the wrong half look settled.
* **A return-value assertion is not a fence test.** The first L1 regression
  test asserted `_pause_windows_gateways_for_update() is None`, and it stayed
  GREEN with the fence reverted: under a hermetic HERMES_HOME
  `_profile_suffix()` is empty, the task name resolves to `Hermes_Gateway`, and
  this operator has only `Hermes_Gateway_alice` registered. The escape's
  firing depends on which task name the process happens to resolve. The claim
  is now the seam itself — the callable bound at `hermes_cli.main` is the
  conftest default, not production's — which is red on any host.
* **`test_web_ui_build` needing node/npm** — the 08-30 note already corrected
  this and it holds: node v20.17.0 and npm 10.8.2 are present, all 8 pass in
  isolation, and they are class A.
* **`web_server._SESSION_TOKEN` being rotated** — nothing mutates it. The 401s
  are the auth MODE (class B), not the token.

## 3. Fence red-proof

Each layer reverted in the worktree on its own, then restored.

| layer | revert | result |
|---|---|---|
| L1 | conftest default removed | `test_the_pause_seam_is_defaulted_for_every_test_in_this_directory` FAILED (1 failed, 25 passed). Separately, with the recording probe: `test_update_autostash.py` brings back `atexit.register(_resume_windows_gateways_after_update, {... cold_start_if_installed: True})` and `schtasks /Query /TN Hermes_Gateway_alice`. With L1 in place the probe log is not even created. |
| L2 | `_install_spawn_detached_stub()` removed | `test_spawn_detached_is_fenced_for_the_whole_session` FAILED (1 failed, 25 passed). Nothing spawned: L3 and the root conftest guard both caught the Popen. |
| L3 | `_install_spawn_wrappers()` removed | `test_popen_refuses_the_measured_argv`, `test_subprocess_run_refuses_the_measured_argv`, `test_fence_survives_a_teardown_of_every_monkeypatch`, `test_fence_holds_on_a_background_thread` all FAILED (4 failed, 22 passed). |

L3's revert was run with the external recording probe loaded in place of the
fence, and the probe logged 3 refused `Popen` calls. That substitution is
deliberate and is the honest note here: those four tests carry
`live_system_guard_bypass` (the mark IS their claim — unmarked, the root
conftest's guard raises first and they would pass with this fence deleted), so
a bare L3 revert on this host would have started the operator's gateway, which
is the exact event the fence exists to prevent. Nothing was spawned at any
point in this stage; a `Win32_Process` sweep for `python … gateway … run`
returned empty afterwards.

## 4. Numbers

| run | result |
|---|---|
| 08-30 baseline (`00fa94dd75`, launcher notes §19) | 40 failed, 4327 passed, 100 skipped, 1 xfailed — 738.74s |
| 08-31, fence in, real-store arm too wide | 84 failed (40 pollution + 44 fence over-refusal), 4308 passed, 100 skipped — 818.28s |
| 08-31, classes A/B/C fixed | 4 failed, 4389 passed, 100 skipped, 1 xfailed — 724.62s |
| 08-31, classes D/E fixed, rebased onto `301946bc57` | 1 failed, 4393 passed, 100 skipped, 1 xfailed — 790.45s |
| **08-31 EXIT** | **0 failed, 4394 passed, 100 skipped, 1 xfailed — 749.15s (12:29)** |

Command throughout: `python -m pytest tests/hermes_cli -q --timeout=300`.
The 100 skips are the pre-existing environment tiers (`_ENV_GAP_SKIPS`, the
Node-floor web-build prerequisite, the local-model probe); the 1 xfail is the
fenced Slack 50-slash known defect. No skip or xfail was added by this stage.

## 5. Rows for the orchestrator

Three queue rows are CLOSED by this branch and should be deleted at landing:

* `- **`tests/hermes_cli` full run carries 39 order-dependent failures at `00fa94dd75`** ·`
* `- **`test_xai_provider_labels` asserts a models.dev catalog label with no catalog seeded under`
* `- **A `tests/hermes_cli` run spawns a REAL gateway against the operator's live alice profile** ·`

Two NEW rows are owed, both product-side and both out of this stage's scope:

* **`plugins/memory/__init__.py` registers discovered provider submodules in
  `sys.modules` without binding them on the parent package** — the loader does
  `spec_from_file_location` + `sys.modules[full_name] = mod` and skips the step
  real import machinery performs, so `from plugins.memory import honcho` fails
  and `importlib.import_module` will not repair it (it short-circuits on the
  sys.modules row). The suite is fenced against it; production is not.
* **`hermes doctor` probes an executable resolved out of a live profile home**
  — `X:\Eternia\.hermes\profiles\alice\node\agent-browser.CMD --version`, a
  path bound at import time, executed 56 times in one suite run. Read-only
  today, but it is the suite reaching into the operator's store.

Worth a third row if the orchestrator wants it: `gateway/pairing.py`'s
module-scope `PAIRING_DIR` is the same import-time-frozen-path shape as
`hermes_state.DEFAULT_DB_PATH`, and both are now re-pinned by test fixtures
rather than resolved at call time in production.
