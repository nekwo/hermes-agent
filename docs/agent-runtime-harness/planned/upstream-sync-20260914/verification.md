# Verification and delivery state

## History/review results

Audit stage-one commit: `c7abb31cc3` on `codex/upstream-audit-20260914`, pushed.
Local review branch: `codex/local-llama-review-20260914`, pushed at
`5104732788a545579aa8498f211467a55679b9e8`.

| Original commits | Consolidated review commit |
| --- | --- |
| `9015185e4e` | `1445fa9d69f9173d85ca4b4f23b0bc441143f375` |
| `0ca2571141`, `5a46bf18d6`, `6b3106f2a5`, `9e5b306b94`, `9ec37ba8ba`, `31bf01536a` | `595b775ea0185464b8084f6bc2a7fac0c070eb3a` |
| `34ad8ba33f` | `5104732788a545579aa8498f211467a55679b9e8` |

All 1,164 earlier fork-only commits retain their original IDs and ancestry in
the review branch. `original-to-consolidated.csv` maps every one of the 1,172
original commits, including authors and coauthor trailers. Full group snapshots
and source IDs are in `local-llama-consolidation.json` and commit messages.

`git diff --exit-code 34ad8ba33f 5104732788` exited 0 with no output.
Both trees are `5f5a34ad214d695f5c889a0798e220d175cc91ad`. This proves tracked
file bytes, modes and paths at the final tip, not independent viability of every
intermediate commit. No new upstream changes were introduced into the review.

## Baseline regression commands

Executed from the fresh audit worktree through Git Bash and the canonical shared
test venv. Commands below are the exact wrapper arguments (logs are adjacent).

```bash
scripts/run_tests.sh tests/agent_runtime/test_local_llama_config.py tests/agent_runtime/test_local_llama_catalog.py tests/agent_runtime/test_local_llama_manager.py tests/agent_runtime/test_local_llama_process.py tests/agent_runtime/test_local_llama_provider.py tests/agent_runtime/test_local_llama_router.py tests/agent_runtime/test_local_llama_rpc.py tests/agent_runtime/test_local_llama_gateway.py tests/agent_runtime/test_serve_rpc_authorization.py tests/agent_runtime/test_profile_runner.py tests/agent_runtime/test_config.py tests/hermes_cli/test_persona_set_model.py tests/hermes_cli/test_provider_visibility_v2.py tests/hermes_cli/test_auth_codex_provider.py tests/hermes_cli/test_runtime_provider_resolution.py tests/hermes_cli/test_config.py tests/agent/test_compression_feasibility.py -j 2 --file-timeout 180
```

Exit 0: **327 passed, zero final failures, 14 discovered files, 103.6 seconds**.
Three specified paths were incorrect and not discovered; the follow-up below
ran their actual locations, so no coverage is claimed from the nonexistent paths.

One file was **FLAKY**, failed on attempt one and passed automatic retry:
`test_local_llama_manager.py::test_busy_and_stale_guards_are_server_enforced`.
Expected `operation_busy`, got `stale_revision`. The queued start can transition
after status returns and before stop admission, advancing the revision. Both are
refusals; this result does not prove the busy arm. Preserve that distinction and
make the fixture synchronize with the running operation before testing the busy
arm during integration; do not weaken the assertion to accept either outcome.
Raw first failure and retry are preserved in `baseline-tests.txt`.

```bash
scripts/run_tests.sh tests/agent_runtime/test_persona_set_model.py tests/test_provider_visibility_v2.py tests/run_agent/test_compression_feasibility.py -j 1 --file-timeout 180
```

Exit 0: **67 passed, zero failures, 3 files, 32.9 seconds**, no retry.
Together: **394 passed**, with the one explicitly named retry above.

## Real installed llama/model/tool probe

Ran the existing `scripts/probe_local_llama_runtime.py` with the canonical test
venv Python, `--executable` and `--model` pointing to the same installed b10809
CUDA binary and Qwen 27B Q4_K_M GGUF recorded by the prior local proof, and
`--output X:/wt/hermes-upstream-audit-20260914/qa-artifacts/upstream-sync-baseline-llama`.
Exact machine paths remain in that isolated config and local logs, not published
defaults. The probe creates its own free loopback port, HERMES_HOME, runtime root,
manager ownership, process tree and receipts; existing runtime config is untouched.

Exit **0**, receipt `complete=true`. Verified configuration, scan, empty start,
8192-context load, runner-resolved text inference, actual terminal tool execution
and result (two API calls), same-model/same-endpoint auxiliary routing, no fallback,
lease release, unload, 4096-context reload, stop, runtime closure. Machine-path-free
summary: `real-probe-summary.json`; raw receipt hash:
`ad5114cac96852bc409bc0189fdc7036fd85f38e4f93d4985350112101123946`.
The probe overwrites its `load` receipt key on reload; the executed script asserts
both context sizes, but the final receipt alone is not two separately named load
records. Raw local output also remains in `qa-artifacts/upstream-sync-baseline-llama.log`.

## Not completed / not claimed

- Full 7–15-commit fork reconstruction: **not constructed**. Fifteen provisional
  review themes and the bounded three-commit Local llama example are provided.
- Upstream integration branch SHA: **none**. Only an object-level merge preview
  was produced; `conflict-ledger.csv` has all 187 unresolved paths. No resolution
  has been selected or tested. Do not mistake the preview tree for a candidate.
- Integration-source tests, corrected upstream sync gate, dependency reconciliation,
  full downstream regression, Launcher contract checks and updated-source installer
  validation: **not run / still required**.
- Main update: **none**. Original functionality remains in unchanged published main.
  These are recovery/review checkpoints, not a completed synchronization or delivery.
- No primary service was restarted or reconfigured. Only the isolated probe's
  own process tree was started and stopped. Existing test worktrees are retained.

Continue from the original snapshot, preserve both merge parents, resolve the
ledger by dependency order, and record behavioral evidence per seam. Do not start
the installer until a combined candidate passes and its backend contract is verified.
