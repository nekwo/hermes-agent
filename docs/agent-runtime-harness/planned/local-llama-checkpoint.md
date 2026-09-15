# Local llama implementation checkpoint

Updated: 2026-09-14. Owner: Codex. Feature **not delivered**.
Canonical design: [local-llama-agent-console.md](local-llama-agent-console.md).

## Resume here

- Hermes branch: `codex/local-llama-runtime`; Launcher branch:
  `codex/local-llama-console`. Locate them with `git worktree list`; preserve every
  dirty worktree. Launcher queue row is TAKEN by this implementation.
- H0 complete. H1/H2 now include owning-serve binding/shutdown, RPC dispatch and
  authorization, local provider selection/readiness, whole-turn leases, scoped
  context/generation/auxiliary routing, and resident actor invalidation.
- Real production manager/RPC proof passed: configuration, scan, empty startup,
  load 8192, runner-resolved text inference, unload, load 4096, stop. Isolated
  artifacts: `qa_artifacts/local-llama-runtime-proof/runtime-receipt.json`.
- Full agent-loop probe passed, including a real terminal tool call and response
  (two API calls), same-model auxiliary compression routing, exact 8192 context,
  zero fallback entries, reload 4096 and stop. Final local artifact directory:
  `qa_artifacts/local-llama-agent-aux-proof/`. Launcher L1 is delegated to Sol.
  DTOs are frozen; do not change field names without coordinating.
- Next: complete failure/recovery tests, full agent and remote/served-wire proof,
  integration review, Launcher focused tests and Stage C. Keep the final gate open.
- The new modules are checkpoint code, not a production-ready claim. Do not
  land the runtime feature on main until the remaining contracts and proof pass.

## H0 evidence

Ran `scripts/probe_local_llama.py` against the operator's installed b10809 CUDA
binary and existing Qwen 27B abliteration Q4_K_M GGUF. Executable SHA256:
`577b3116ecab660645e244b4452c5077b8527452c64883835ce4bba476a4e289`.
Local raw receipts/logs: `qa_artifacts/local-llama-h0-verified/` in the Hermes
worktree (not committed; contain machine paths). Re-run using the script's explicit
`--executable`, `--model`, `--output`, and `--context` options; it creates its own
unused loopback port and owned process tree.

Verified with **one slot / 8192 context**: empty router startup, unauthenticated
`/models` returns 401, load, observed readiness, text answer `READY`, structured
`echo(text="hello")` tool call, tool-result round trip, unload to observed
`unloaded`, preset refresh, reload with **4096 context**, final unload and owned
process shutdown. Probe exited 0. Template reports `supports_tools` and
`supports_tool_calls`. Both load AND unload return acceptance before completion;
the manager must poll authoritative model state. `?reload=1` supports preset
refresh while the empty router remains running.

Earlier probes exposed two incorrect assumptions and were corrected: immediate
`/props` after accepted load returns 503; immediate reload after accepted unload
returns 400 while the worker still exits. The default **four slots / 32768 each**
caused severe memory pressure and a 180-second tool-call timeout. Explicit one-slot
configuration completed the tool round trip quickly. Do not reintroduce default
slot count or conflate command acceptance with readiness.

## Foundation proof and caveats

`scripts/run_tests.sh tests/agent_runtime/test_local_llama_process.py
tests/agent_runtime/test_local_llama_config.py
tests/agent_runtime/test_local_llama_manager.py` exited 0: **21 passed**.
All three files hit the runner's 300-second parallel timeout and passed its
automatic one-worker retry (process 126.7s, manager 8.8s, config 8.5s). This is
retry-green evidence, not a clean timing result. Diagnose the process test's long
duration and use a bounded serial confirmation after changes; never hide retries.
Python compile check passed for the five foundation modules.

Tests currently cover process-tree ownership versus unrelated processes, failed
launch, strict configuration validation, root comment preservation, nonblocking
start, idempotency, revision/busy guards, lease exception cleanup, restart receipt
lookup, and credential omission from read projections. Missing proof includes
catalog malformed/split cases, failure cleanup/reconciliation, real manager/router
integration, RPC auth/dispatch, provider paths, and every Launcher/Q1 obligation.

No live Hermes config was changed. No runtime feature commit has landed on main.
The pre-existing runtime-skill preload ceiling hook failure remains separately
queued; it is unrelated to this feature. Existing Launcher personal memory files
and the spatial-voice worktree remain outside scope.

## RPC/provider checkpoint proof

Focused catalog, manager, provider, persona-set-model, and readiness files passed
64 tests. Provider visibility initially failed its old additive-key allowlist;
the new `local_llama` block is now explicitly excluded from the legacy shape
comparison. Follow-up wrapper run of `test_provider_visibility_v2.py`,
`test_profile_runner.py`, and `test_local_llama_rpc.py` passed **114 tests**, exit 0.
RPC plus existing authorization focused run previously passed **27 tests**.
All commands used `scripts/run_tests.sh`, `-j 1 --file-timeout 120`.

The existing read tier permits UNKNOWN callers to read sanitized status; console
methods still refuse them, and all local-model methods refuse peer callers. This
preserves transport authorization policy. Model settings and logs are console tier.
`thinking` capability currently advertises only `auto`; no unverified override is
sent. Operator model paths are never committed. No cloud fallback is configured.

## Recovery / agent checkpoint

The real agent probe exposed Hermes's 64K context floor. Managed local models now
share the explicit local-context exception, keyed by their requested provider and
verified loaded context. Compression's floor exception is limited to the SAME
local model with the same verified window. We never inflate the context budget.

`test_local_llama_config.py`, `test_local_llama_manager.py`,
`test_local_llama_process.py`, and `test_compression_feasibility.py` passed **37
tests**, exit 0. The process ownership test took 8.9s in this serial run (no retry).
`test_local_llama_gateway.py` passed **2 tests**, exit 0, over real TLS/device
authentication and the real serve loop: console config persistence/idempotency,
read-tier control/path refusals, manager teardown. This verifies the remote wire
on loopback, not a separate physical machine or firewall.

Shutdown is now scoped to the serve that acquired ownership; a non-owning serve
cannot shut down another manager. Health reconciliation checks owned-process exit
and repeated HTTP health failures, discarding observations invalidated by a
concurrent lifecycle change. Missing/corrupt saved settings return typed errors;
failed construction releases ownership. Missing replacement weights preserve the
currently loaded model. No final visual acceptance or main landing yet.

## Baseline failure outside this feature

`test_response_contract_fixture.py` has an existing producer/fixture mismatch:
`realm_sync_status_remote_unreachable.json` omits the producer's `levels` key.
Confirmed with the same focused wrapper command on clean Hermes main `9015185e4e`:
17 passed, 1 failed, exit 1. The feature worktree has the identical failure.
Do not regenerate unrelated response fixtures as part of local llama. The
companion `test_stream_contract_fixture.py` passed 21 tests; the expanded
`test_persona_set_model.py` passed 40 including local selection while off.

Latest recovery tail: operation fingerprints are SHA256 digests (bounded even for
large catalogs), unknown mutation keys and non-integer preset revisions are
rejected, manager ownership contention is typed, and shutdown cannot spawn a
late router after binary probing. Receipt write failure rolls back the queued
in-memory operation before any process starts. Focused manager/router tests:
**13 passed**, exit 0, no retry. Launcher review fixes and Stage C remain open.
## Landing checkpoint — 2026-09-14

Operator requested landing the current implementation. Latest backend review adds
read-tier selected-host catalog fields (`configured` and model `context_length`).
Manager and real TLS gateway tests passed 15 through scripts/run_tests.sh.
Launcher product review passed 89 focused tests and its final QA callback test
passed; the normal Windows build and operator provider/settings inspection passed.
Full five-control Stage C acceptance and a second physical remote host remain
unverified. Earlier pending-landing notes above are historical checkpoints.

The improved installation panel and official llama.cpp download/version wizard
are outside this landing and await the operator's next plan.
