# W1-H2 field notes — test isolation: the gateway hole and the pollution reds

Running record for the decision-close wave's W1-H2 stage (hermes repo, branch
`feat/dcw-h2-test-isolation`, off `origin/main` `0c744aa586`). Everything here
was measured on this Windows 10 workstation on 2026-08-31 unless dated
otherwise.

## 0. The gateway escape: found in 7 seconds, not 13 minutes

The plan said "find the path by which `tests/hermes_cli` reaches
`_cold_start_windows_gateway_after_update` unmocked". The 08-30 baseline had
caught it only as two stray lines at the end of a 738s run. Rather than repeat
that run, the whole chain was reproduced from ONE file with a recording probe
(a `-p` plugin that wraps `subprocess.Popen`/`run` and `atexit.register`
process-wide and refuses instead of executing):

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

Three separate reasons, and each is a lesson of its own:

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
  one that is ABOUT the pause.
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
39 of them order-dependent. Re-measured here at `0c744aa586`: the same 40,
same modules, same tests. Nothing had drifted — only one commit touched these
trees in between.

Three named mechanisms account for all of them.

### Class A — two `hermes_cli.main` module objects

`tests/hermes_cli/test_skills_subparser.py` does
`del sys.modules['hermes_cli.main']` then `import hermes_cli.main`, to prove
the parser still builds, and never puts the original back. The process then
holds TWO `hermes_cli.main` namespaces:

* every test file that did `from hermes_cli.main import X` at COLLECTION time
  holds a function whose `__globals__` is the FIRST, now orphaned;
* `sys.modules["hermes_cli.main"]` is the SECOND, which is what
  `patch("hermes_cli.main.…")` and `monkeypatch.setattr(cli_main, …)` reach.

So the stub lands where the function under test never looks, and **production
runs for real**. The `npm error code EJSONPARSE` in the 08-30 baseline output
was not a mock misfiring — it was a genuine `npm run build`. Likewise every
`_cmd_update_impl` helper stub in `test_update_venv_health` silently did
nothing.

This is also why the class is "deterministic, but only in a full run": it
depends on running after ONE file, not on timing, and alphabetical ordering
does the rest. One file, 16 reds — `test_web_ui_build`(8),
`test_update_venv_health`(4), `test_update_interrupted_recovery`(2),
`test_update_autostash`(1), `test_update_concurrent_quarantine`(1).

**Fix (class, not caller):** autouse `_sys_modules_identity_is_restored`
restores the IDENTITY of any module the session already had if a test replaced
or dropped it. Two halves, and the second was NOT optional:

* the `sys.modules` row, and
* **the parent package attribute.** `import hermes_cli.main` binds `main` on
  the `hermes_cli` package object, and `from hermes_cli import main` reads
  THAT, not `sys.modules`. With only the first half, `test_web_ui_build` went
  green and every update module stayed red — because `update_cmd._m()` and the
  update tests both use the `from hermes_cli import main` spelling.

Deliberate non-coverage, each recorded at the site:

* newly imported modules are LEFT alone — lazy imports are normal, and
  un-importing them would be its own pollution;
* `importlib.reload` is out of scope — it mutates the module in place, so
  identity and every binding survive. Covering it here would only look like an
  answer. (14 reload sites exist in this directory; none is behind any red
  measured in this stage — see §3.)
* a non-`str` key is skipped: `test_setup_openclaw_migration.py` mocks
  `importlib.util.spec_from_file_location`, so production's
  `sys.modules[spec.name] = module` files a module under a MagicMock. Tripping
  over it ERRORed two green tests in `test_state_db_guard.py`.

### Class B — the dashboard stayed in OAuth mode for the rest of the run

`hermes_cli.web_server.app` is a module-level FastAPI instance, so `app.state`
is ONE mutable mapping for the whole session, and the auth middleware branches
on `app.state.auth_required`. The gate tests set it True and stop there on
purpose — the assertion that a fail-closed public bind RECORDS the flag is the
last statement. Everything downstream is then 401ed, because gated mode does
not honour the legacy session token, and the
`HEADERS = {"X-Hermes-Session-Token": _SESSION_TOKEN}` idiom is module-level in
a dozen files here.

`test_dashboard_auth_gate.py` alone reds the 4 tests in
`test_env_custom_keys.py` + `test_env_export_line_lifecycle.py` that the full
run reports. It reads as "the endpoint broke", never as "the process has been
in OAuth mode since three files ago".

**Fix:** autouse `_web_server_app_state_is_pristine` resets `app.state` to the
mapping as it stood the first time this directory saw the module.

**The reset is at SETUP, and that is load-bearing.** `monkeypatch` is
instantiated by the root conftest's first autouse fixture, so its undo runs
after anything declared here — and
`monkeypatch.setattr(app.state, "bound_port", 9119, raising=False)` undoes
itself with a `delattr`. A teardown-time restore removed that key first and
monkeypatch's undo died with `KeyError: 'bound_port'`, turning a green test
into a teardown ERROR. Resetting on the way IN leaves every pending undo
intact and gives the same guarantee.

### Class C — the xAI label fixture gap (not order-dependent)

The 40th red, and the only one that reproduced in isolation. `get_label("xai")`
has no `_LABEL_OVERRIDES` entry on purpose — that IS the claim being guarded:
`xai` takes its name from the models.dev catalog, `xai-oauth` from the override
table, and the bug was the two collapsing onto one string. The catalog resolves
`HERMES_HOME/models_dev_cache.json` and then the network; under the hermetic
home neither answers here, so `get_provider` falls to the Hermes overlay whose
name is `_LABEL_OVERRIDES.get("xai", "xai")` — the lowercase id. The file never
provided the catalog it reads. Seeded in a fixture; the OAuth half still comes
from production, so a real collapse still reds.

## 2. Things that were checked and are NOT the cause

* **`importlib.reload` of `hermes_constants` / `hermes_cli.main` /
  `agent.curator`.** The obvious suspect, and wrong. Reload re-executes into
  the SAME module `__dict__`, so a function imported before the reload still
  resolves its globals through the updated namespace; identity, and therefore
  every `from X import f` binding, survives. Bisected directly:
  `test_curator_recent_run_notice.py` and `test_env_export_prefix.py` — the two
  files that reload `hermes_cli.main` — placed in front of
  `test_web_ui_build.py` leave it green.
* **`test_web_ui_build` needing node/npm.** The 08-30 note already corrected
  this and it holds: node v20.17.0 and npm 10.8.2 are present, and all 8 pass
  in isolation. They are Class A.
* **`web_server._SESSION_TOKEN` being rotated.** Nothing mutates it. The 401s
  are the auth MODE (Class B), not the token.

## 3. Numbers

| run | result |
|---|---|
| 08-30 baseline (`00fa94dd75`, launcher notes §19) | 40 failed, 4327 passed, 100 skipped, 1 xfailed — 738.74s |
| 08-31 measurement, fence in, real-store arm too wide (`7a5f4ff6ba`) | 84 failed (40 pollution + 44 fence over-refusal), 4308 passed, 100 skipped — 818.28s |
| 08-31 exit run | filled in at §5 |

## 4. Fence red-proof

Filled in at the end of the stage.

## 5. Exit run

Filled in at the end of the stage.

## 6. Queue / plan rows

Filled in at the end of the stage.
