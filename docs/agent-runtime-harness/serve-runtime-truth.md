# Serve Runtime Truth — interpreter chain + read-model/snapshot freshness

> Operator-forensics reference. Two runtime facts that repeatedly look like bugs
> during live-serve diagnosis but are **normal and healthy**. Documented here so
> future forensics stop chasing them. Nothing in this doc is a code change — it
> retires two false diagnostic tells.

---

## 1. The interpreter process chain is a standard Windows venv trampoline — nothing to pin

When the Launcher (or an operator) starts a harness process on Windows, the
process tree looks like this:

```text
launcher  ->  <venv>\Scripts\hermes.exe        (pip console-script stub)
          ->  <venv>\Scripts\python.exe         (venv launcher stub, ~100 KB)
          ->  <base>\Python311\python.exe        (the actual interpreter image
                                                  named by pyvenv.cfg `home`)
```

This is **standard Windows CPython venv behaviour**, not a Hermes re-exec:

- `hermes.exe` is the setuptools/pip **console-script stub** for the `hermes`
  entry point. It launches the venv Python and runs the entry function.
- `<venv>\Scripts\python.exe` (and `pythonw.exe`) are themselves thin **venv
  launcher stubs**. Each stub spawns the base interpreter it was built from
  (the path in `<venv>\pyvenv.cfg` under `home = …`) with the **same command
  line**, so the base-Python process inherits the venv context.
- The base `Python311\python.exe` grandchild is the real interpreter image. It
  still reports `sys.executable` as the **venv** python, and `site`/`sys.path`
  still resolve the venv's `site-packages` **because** of `pyvenv.cfg`. There is
  no leakage to the system environment.

Consequences for forensics:

- **A `python.exe` (or `pythonw.exe`) under `C:\…\Python311\` in the process
  tree is HEALTHY**, not a hijacked/mis-pinned interpreter. It is the venv
  trampoline's target, running with the venv's packages.
- **There is NO hermes-level re-exec and NOTHING to "pin" here.** The venv is
  already the effective environment; the base-Python image is an implementation
  detail of how Windows venvs launch.
- The only place this matters cosmetically is process listings: a venv run
  yields **two** PIDs with identical command lines (stub + real interpreter),
  which is confusing in `gateway status`. The gateway already collapses this —
  see the explanation and `_filter_venv_launcher_stubs` in
  [`hermes_cli/gateway.py`](../../hermes_cli/gateway.py) (the comment block at
  ~L558–573): "if a PID in our result is the PARENT of another PID in our
  result, and both are pythonw.exe, the parent is the launcher stub — drop it,
  keep the child."

**Retire the tell:** "there's a system-Python311 process in the tree, the venv
must be broken / something re-execed outside the venv." No — it is the venv
trampoline working as designed.

---

## 2. Frozen `read_model.db` / `snapshot.json` mtimes during a live serve are NORMAL

### Who writes each file

- **`snapshot.json`** is written **only** by `harness snapshot`
  (`_cmd_snapshot` → `agent_runtime.snapshot.write_snapshot`, which
  `atomic_json_write`s `paths.snapshot_path()`). Nothing else writes it.
- **`read_model.db`** is additionally written by:
  - **`harness read-model rebuild`** (`_cmd_rebuild_read_model` →
    `Projector(...).full_rebuild()`),
  - and `write_snapshot`'s dual-write when `agent_runtime.read_model.enabled`
    is set (`ReadModel().apply_full_rebuild(...)`).

### Why a live serve leaves both files untouched

A live `harness serve` feeds Mission Control from **in-memory** stream/status
lanes, and it replays `harness snapshot --json` / `harness status --json` from a
short-lived **RAM cache** instead of re-dispatching the command. See
[`hermes_cli/harness_parts/serve.py`](../../hermes_cli/harness_parts/serve.py):

- `_CACHEABLE_ARGV` = `{("harness","status","--json"), ("harness","snapshot","--json")}`.
- On a cache hit the `_run` path returns the cached stdout payload **without
  dispatching** the command — so `write_snapshot` is never called and
  `snapshot.json` is not rewritten.
- The cache is keyed on a cheap runtime-state fingerprint (events.jsonl + the
  rotation manifest/live slice, the per-session turn store, scope pointers,
  store dirs, the boards subtree, the `running_work` stores, and the resolved
  chat-scope SessionDB files) and bounded
  by a TTL, `_READ_CACHE_MAX_AGE_SECONDS = 20.0`. A replayed response stamps
  `served_from_cache` + `cache_age_ms` on its exit frame; the payload's parity
  envelope keeps the honest original `generated_at`.

So during an idle-but-live serve, no `snapshot` dispatch reaches disk (it is
served from cache). **The two files' mtimes stay frozen. That is expected.**

### Retire the old tell

> ~~"`read_model.db` mtime < session start ⇒ no serve is running."~~ **FALSE.**

A frozen `read_model.db` (or `snapshot.json`) mtime says nothing about whether a
serve is live. A healthy live serve serves Mission Control entirely from RAM
(in-memory lanes + the ≤20s cache) and only rewrites those files when something
actually **dispatches** a write (an explicit
`harness snapshot` / `harness read-model rebuild`, or a cache miss/TTL expiry
that forces a real snapshot build). To check liveness, look at the serve process
/ its stream frames — **not** at these file mtimes.

---

*Cross-references:* the read-model/snapshot data layer is specified in
[05 — Runtime Data: Enterprise-Grade Storage & Access](05-runtime-data-enterprise-storage.md)
and [12 — Read-Path Freshness Hardening](12-read-path-freshness-hardening.md);
the serve cache is described in [harness-serve-design.md](harness-serve-design.md).
