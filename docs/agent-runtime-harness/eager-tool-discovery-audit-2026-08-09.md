# Eager tool discovery: a read-only projection that writes — audit, 2026-08-09

## Verdict

Confirmed by execution, and wider than filed: `model_tools`' module-scope
`discover_builtin_tools()` fires on **any** import of the agent_runtime
persona/snapshot/harness tree — not just the `running_work` collector — and its
constructor chain **creates `state.db`, scaffolds an entire HERMES home
(11 directories), writes `SOUL.md`, and runs `recover_abandoned_delegations()`**
in whatever home ambient resolution picks, in every cold process, before any
verb runs. The measured cost is **~0.9 s per cold process** (warm discovery
cache) on top of the import itself. The mutation "teeth" are real in mechanism
but have never bitten on this box: every live `async_delegations` table is
empty, the recovery UPDATE only touches rows whose owner is provably dead (the
same reclassification serve boot performs), and `apply_wal_with_fallback` never
downgrades an existing WAL database. The suite does **not** write into the live
install when `HERMES_HOME` is unset (the conftest sandbox catches everything,
including collection-time firing); it **does** mutate whatever home an operator
exports `HERMES_HOME` to, at *collection time*, before any fixture runs. The
named test really is false-green, for the reason filed.

**Recommendation: split the finding in two.** (1) Move the delegation-restore
I/O out of `ProcessRegistry.__init__` to the four owning entry points — small,
surgical, retires the mutating-import class, and the repo already did exactly
this migration for MCP discovery (#16856); schedule it **now-ish** (next
hygiene wave). (2) Full lazy builtin discovery is a **perf-scheduled refactor**
(~0.9 s off every cold harness/CLI process), worth a slot in the startup-perf
workstream, not an emergency. Nothing here justifies interrupting in-flight
work tonight: no wire consumer depends on the side effect (da656b7e6), no live
data has been corrupted, and the blast radius of a hasty lazy-discovery change
is the whole tool registry.

---

## 1. The chain, confirmed by execution

Fresh process, audit hook on `sqlite3.connect` / `os.mkdir` / write-mode
`open`, `HERMES_HOME` pointed at an empty directory, then
`from agent_runtime import running_work; running_work.build_running_work()`.
Captured stack (abridged; full run under "Evidence", §5):

```
running_work._collect_chat_turns
  from . import mission_chat_turns                       (lazy, collector body)
    agent_runtime/mission_chat_turns.py:13  from .persona_assignments import ...
    agent_runtime/persona_assignments.py:31 from .tool_visibility import ...
    agent_runtime/tool_visibility.py:11     from model_tools import get_toolset_for_tool
    model_tools.py:197                      discover_builtin_tools()   [MODULE SCOPE]
    tools/registry.py:114                   importlib.import_module("tools.close_terminal_tool")
    tools/close_terminal_tool.py:19         from tools.process_registry import process_registry
    tools/process_registry.py:2105          process_registry = ProcessRegistry()  [module-scope singleton]
    tools/process_registry.py:197           restore_undelivered_completions(self.completion_queue)
    tools/async_delegation.py:370           recover_abandoned_delegations()
    tools/async_delegation.py:315 → 206 → 140   _transaction() → _connect() → sqlite3.connect(state.db)
```

Exactly as filed. Two side effects the filing did NOT include, both captured on
the same run:

- **`_connect()` → `_initialize_schema()` → `hermes_state.apply_wal_with_fallback`
  → `resolve_journal_mode()` → `hermes_cli.config.load_config_readonly()` →
  `_load_config_impl()` → `ensure_hermes_home()`** — the "read-only" config
  load scaffolds the ENTIRE home: `cron/ sessions/ logs/ logs/curator/
  memories/ pairing/ hooks/ image_cache/ audio_cache/ skills/` plus a written
  **`SOUL.md`**. A second instance of the same defect class (a read path that
  materializes state), living in config, not tools.
- **`discover_builtin_tools()` itself writes `cache/tool_discovery_cache.json`**
  (creating `cache/`) under `get_hermes_home()` whenever any scanned file's
  mtime changed.

Net filesystem product of one read-only `build_running_work()` in a cold
process against an empty home: `state.db` (with schema + ALTER upgrades),
`SOUL.md`, `cache/tool_discovery_cache.json`, and 11 directories.

## 2. Blast radius — every path that fires this

The trigger is `import model_tools`, wherever it comes from. Verified module
scope (AST-checked, not grep-trusted): `tool_visibility.py:11` →
`persona_assignments.py:31` → and from there the entire tree. Non-test,
module-scope importers of `persona_assignments`/`tool_visibility` include:

- **`hermes_cli/harness.py` (lines 52, 136)** — so **every `hermes harness *`
  CLI verb**, including every read-only projection and the launcher's CLI
  snapshot lane, fires the full chain at import, before argument parsing.
- **`agent_runtime/snapshot.py` (30, 57)** — every snapshot build.
- ~25 further `agent_runtime` modules (`mission_chat_turns`, `persona_runtime`,
  `runtime_hud`, `office_store`, `flow_graph`, `status`, `continuity`, …),
  `hermes_cli/harness_parts/persona_commands.py`, `tools/agent_chat_tool.py`,
  `tools/board_tool.py`.
- Direct module-scope importers of `model_tools`: `run_agent.py`,
  `batch_runner.py`, `agent_runtime/tool_visibility.py`. (`cli.py`, gateway,
  TUI, ACP import it lazily/explicitly at startup — the MCP-discovery
  precedent, `model_tools.py:199-210`.)

So the honest statement is: **any process that touches agent_runtime, the
harness CLI, run_agent, or batch_runner pays the chain at import time.** The
`running_work` collector was merely the trigger observed in an otherwise
tool-free process. "Zero other callers" is disproven by execution, not grep.

**Other module-scope singletons.** AST sweep over `tools/ agent/ gateway/ cron/
agent_runtime/` for module-level class instantiations, then constructor
inspection + the dynamic audit run: `ProcessRegistry` is the **only**
module-scope singleton that performs durable I/O in `__init__`.
(`FileStateRegistry`, `ToolRegistry`, `RelayHostRegistry`,
`PlatformRegistry`, `DebugSession`s, codecs — all in-memory.) The second
instance of the *class* of defect is not another singleton; it is
`load_config_readonly() → ensure_hermes_home()` (§1). So the ruling is: fix
the one singleton, AND pin the behaviour so the pattern cannot return — a full
"ban the pattern" campaign has exactly two known instances and both are named
here.

## 3. Harm, characterized honestly

**Which home?** `async_delegation._db_path()` resolves
`get_hermes_background_work_home()` (head-home authority: `HERMES_HEAD_HOME`
env → recorded contextvar → ambient `HERMES_HOME` → platform default
`%LOCALAPPDATA%\hermes`). A read-only verb writes to the operator's REAL home
whenever it runs in a process whose env resolves there — i.e. **always in
production lanes** (that is where state.db is *supposed* to live; the writers
would create it anyway) and, more corrosively, in **any ad-hoc process**
(probe scripts, doctor runs, one-off `python -c` invocations) where an empty
`state.db` + `SOUL.md` + home skeleton silently materializes in whatever
directory the env happened to name. The stale-home/phantom-scaffold forensics
this codebase has already paid for (`profiles\alice\agent_runtime` trap) are
exactly what this manufactures.

**Does recovery mutate during a read?** Yes, by design of the chain:
`recover_abandoned_delegations()` UPDATEs any `running`/`finalizing` row whose
`owner_pid` is dead, recycled, **or missing** to `state='unknown',
delivery_state='pending'` — which re-queues an "outcome unknown" delivery that
the next gateway/serve drain will inject into the owning chat. A projection
with teeth, mechanically. Mitigations that are actually in the code: liveness
is checked with the same `host_start_time` recycle guard the projection uses;
the reclassification is identical to what serve boot legitimately performs;
and `restore_undelivered_completions` enqueues into the *fresh singleton's*
in-process queue, which nothing drains in a projection process (delivery
claims happen only at drain, so no durable delivery state is burned).
**Observed harm on this box: zero.** Read-only sweep of every live `state.db`
(`X:\Eternia\.hermes`, `profiles\base`, `profiles\alice`,
`profiles\launcher-qa`, `%LOCALAPPDATA%\hermes`): every `async_delegations`
table is **empty** — no rows ever mutated, no recover-produced `unknown` rows
anywhere. (`profiles\neko\state.db` predates the table entirely.) Also checked:
`apply_wal_with_fallback` explicitly never live-downgrades an existing WAL
database, so no journal-mode flip risk from the read path.

**Cost, measured (C:\Python312, warm OS cache):**

| measurement | time |
| --- | --- |
| `import agent_runtime.running_work` alone | 0.11–0.12 s |
| first `build_running_work()` in a cold process (drags the chain) | 1.36 s (warm discovery cache) / 1.77 s (cold) |
| second `build_running_work()` (chain resident) | 0.004 s |
| `import model_tools` | 1.51 s warm / 2.39 s cold cache |
| `discover_builtin_tools()` isolated, warm cache | **0.889 s** |
| `import hermes_cli.harness` (includes the chain) | 1.30 s |

Discovery is the dominant share of the `model_tools` import, and
`hermes_cli/harness.py` pays it on every CLI invocation — **this is a
startup-perf item for the same lane the 2026-08-09 Mission Control work just
took from ~6.95 s to ~1.55 s.** ~0.9 s of any cold harness CLI process is
eager tool discovery for verbs that may never execute a tool.

**The false-green, confirmed under pytest** (audit-hook plugin logging every
`state.db` connect with test attribution):
`test_the_projection_never_creates_the_state_db_it_reads` runs
`build_running_work()`, which **does** create a `state.db` — in the autouse
`_hermetic_environment` per-test tempdir
(`...\pytest-...\hermes_test\state.db`), because the `home` fixture stubs only
`running_work._head_home` while `async_delegation._db_path()` resolves the
ambient env. The assert then checks the stubbed `home`, where nothing was ever
going to be written. The projection's delegation lane was even observed
*reading back* (mode=ro) the very `state.db` its own import chain had just
created. **Correction to the filing:** the side effect lands in the hermetic
per-test tempdir, NOT the operator's real home — `tests/conftest.py` sandboxes
`HERMES_HOME` at module import (session tempdir when unset) and per-test
(autouse fixture), so **running the suite has NOT been writing into the live
install** on this box. The real exposure: with `HERMES_HOME` **exported** in
the shell, pytest **collection alone** (verified with `--collect-only` on
`tests/agent_runtime/test_board_agent_tools.py`) creates `state.db` + the full
scaffold + `SOUL.md` in the exported home and runs delegation recovery against
its table — before any fixture exists. Several memory-note probe recipes on
this box recommend exporting `HERMES_HOME`; anyone following them into pytest
mutates that home at collection.

**Live-install evidence found (archived here, nothing deleted):** the
platform-default home `C:\Users\beast\AppData\Local\hermes` shows
`cache\tool_discovery_cache.json` rewritten **2026-08-09 22:27:57** — i.e. some
process ran eager discovery with `HERMES_HOME` unset tonight (this session's
probes and pytest runs all had it set or sandboxed; multiple agent sessions
were active on this box tonight, so attribution is not possible). Its
`state.db` mtime is 2026-08-02 with an empty `async_delegations` table, so no
delegation state was touched there tonight. All other live-home `state.db`
mtimes are consistent with normal serve/persona activity.

## 4. The plan

**What is weak.** Three named instances of one class — *state materialized as
an import/read side effect*:

- **A. `model_tools.py:197 discover_builtin_tools()` + :215 `discover_plugins()`
  at module scope** — importing a name from `model_tools` executes ~75 tool
  module imports and every one of their module bodies.
- **B. I/O in `ProcessRegistry.__init__` (`process_registry.py:197`)** — the
  constructor of a module-scope singleton opens/creates a database and runs a
  mutating recovery sweep. This is the component with teeth.
- **C. `load_config_readonly()` → `ensure_hermes_home()`
  (`hermes_cli/config.py:3265`)** — a reader that scaffolds the home and
  writes `SOUL.md`.

**Why it recurs.** Module-scope side effects are invisible to every caller and
to static reasoning; each new module-scope importer of anything in
agent_runtime silently inherits all three. The repo has already been bitten by
this exact shape once (module-scope MCP discovery freezing gateway heartbeats,
#16856) and fixed it by the exact migration proposed below — the precedent and
the target pattern are already in `model_tools.py:199-210`.

**Target shape.**

1. **B first (small, now).** Delete the `restore_undelivered_completions` call
   from `ProcessRegistry.__init__`; expose
   `process_registry.restore_durable_completions()`; call it explicitly at the
   entry points that own a drain loop — the same four that already run MCP
   discovery explicitly: `gateway/run.py`, `cli.py`, `tui_gateway/server.py`,
   `acp_adapter/server.py`. Constructor becomes pure in-memory. Blast radius:
   four call sites plus any test that asserted restore-at-import (none found
   by behavior; verify at implementation). This alone makes the import chain
   write-free *except* the discovery cache, because §1's config/scaffold chain
   is only reached through `_connect()`.
2. **A second (perf-scheduled).** Make discovery lazy-but-idempotent:
   `registry.ensure_builtin_tools()` (lock-guarded, memoized) called at the
   top of every registry-consuming API (`get_tool_definitions`,
   `get_toolset_for_tool`, `handle_function_call`, `check_tool_availability`,
   and `ToolRegistry`'s own query methods, which closes the "someone queries
   `registry` directly" hole). Convert the two import-time snapshot constants
   — `TOOL_TO_TOOLSET_MAP`, `TOOLSET_REQUIREMENTS` (`model_tools.py:224-226`)
   — to PEP 562 module `__getattr__` lazies; their non-test consumers are
   `batch_runner.py:55/65` (`ALL_POSSIBLE_TOOLS`) and `hermes_cli/banner.py`.
   Entry points that genuinely want eager tools (gateway, cli, tui, acp) call
   `ensure_builtin_tools()` at startup exactly like MCP discovery. **The
   dangerous class — code that works only because someone else imported
   first** — is precisely the `sys.modules.get` residency probes in
   `running_work` (`_module()`, documented) and any test relying on a sibling
   test's import; the fixture-based `discover_builtin_tools()` calls already
   present in tests keep those honest. Same treatment for
   `discover_plugins()`.
3. **C filed alongside**: `resolve_journal_mode()`'s config read should use a
   loader that does not `ensure_hermes_home()` (or `ensure_hermes_home` should
   be explicit at entry points, same pattern). Not load-bearing for A/B but it
   is the remaining writer on the read path.

**How it cannot regress — the behavioural pin.** A source grep is banned
(`tests/test_no_source_grep_assertions.py`, ledger closed) and would be a lie
anyway. The honest pin, landed with this audit as a **strict xfail** so it
self-promotes the day the defect is fixed: spawn a fresh interpreter, point
`HERMES_HOME` at an empty temp home (no `HERMES_HEAD_HOME`), run
`build_running_work()`, assert **no `state.db` exists afterward**. It fails
today (documented defect), and the moment lazy discovery / the constructor fix
lands it XPASSes strictly, forcing promotion to a real always-on invariant.
The same subprocess harness extends naturally to "no files at all appear"
once A+B+C are all retired.

**Fixing the false-green fixture.** The in-process test cannot be made honest
by re-pointing the fixture: by the time it runs, any earlier test in the file
has already constructed the singleton, so the side effect is unrepeatable in
that process — order-dependence was the mechanism of the false green. Landed
now: the existing test's claim is re-scoped in its docstring to what it
actually pins (the projection's DIRECT reads never create the store — the
`mode=ro` open in `_collect_delegations`), and the subprocess pin above
carries the whole-process claim.

## 5. Evidence (runtime artifacts, preserved verbatim)

- Chain stack trace: §1 (captured live via `sys.addaudithook`, 2026-08-09
  ~22:15 EDT, fresh `C:\Python312\python.exe`, `PYTHONPATH=X:\Eternia\hermes-agent`).
- Files created by one cold `build_running_work()` against an empty home:
  `state.db`, `SOUL.md`, `cache/tool_discovery_cache.json`.
- Pytest single-test audit log (HERMES_HOME unset, sandbox active):

  ```
  connect during tests/agent_runtime/test_running_work.py::test_the_projection_never_creates_the_state_db_it_reads:
    C:\Users\beast\AppData\Local\Temp\pytest-of-beast\pytest-66873\test_the_projection_never_crea0\hermes_test\state.db
  SOUL.md write during <same test>: ...\hermes_test\SOUL.md
  connect during <same test>: file:...\hermes_test\state.db?mode=ro   (projection reading back its own side effect)
  ```

- Collection-time audit log (`HERMES_HOME` exported to a marker dir,
  `pytest --collect-only tests/agent_runtime/test_board_agent_tools.py`):

  ```
  connect during <collection>: ...\marker_home\state.db
  SOUL.md write during <collection>: ...\marker_home\SOUL.md
  ```

  Marker home afterward: `SOUL.md audio_cache cache cron hooks image_cache
  logs memories pairing sessions skills state.db`.

- Live-home sweep (read-only, 2026-08-09 22:3x EDT): all `async_delegations`
  tables empty; `recover`-produced `unknown` rows: 0 everywhere;
  `%LOCALAPPDATA%\hermes\cache\tool_discovery_cache.json` mtime
  2026-08-09 22:27:57 (unattributed HERMES_HOME-less process tonight);
  `%LOCALAPPDATA%\hermes\state.db` mtime 2026-08-02 10:17.
