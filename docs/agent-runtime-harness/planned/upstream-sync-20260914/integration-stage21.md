# Stage 21 — approved pinned register and final candidate proof

The operator approved the separate pinned upstream register ("pinned sounds good to make future syncs easier"). That decision is implemented; no approval remains pending.

Final candidate and pushed PR/recovery branches: `5eace800f00212dc685ac9fcff001e48fe00e8c0`.
Policy commit: `c99cebe69d`; canonical coverage follow-up: `5eace800f0`.
Hermes main is still the original `34ad8ba33f2508ab10bb24a26f0377ddb62660cb`; no runtime maintenance has occurred.

## Policy accounting

The initial 71 upstream assertions included five existing grandfathered assertions whose files moved. Those five entries now follow their renamed files without adding debt. Two retired MCP source checks were already replaced by behavior/signature proof and were removed. The original closed register decreases from 78 to 76 entries.

The separate `tests/upstream_source_assertions.json` contains the remaining 66 unchanged upstream assertions and SHA256 hashes of each entire enclosing AST function, pinned to `110baa095bc7135a0624557a9cc35df0f98ece0f`. Validation rejects unregistered assertions, stale entries, changed/missing/duplicate functions, changed baseline, duplicate keys and inconsistent/excess counts. Formatting-only changes preserve hashes. There is no automatic refresh command and this does not label source inspections as behavioral proof. Original closed registers were not expanded.

## Final local proof

- `final-policy-compat-proof.log`: **1,191 passed, 0 failed**, three files, 168.2s through `scripts/run_tests.sh`. Includes the full tombstone registry, installation-stamp behavior, and all 32 source-policy/mutation checks.
- `compat-pointer-final.log`: no first-party dependency on any of the 2,091 temporary plugin-compat pointers. CI on a27 found that the restored stamp test imported the old config facade. The test now calls `hermes_cli.install_method`; the retired-test coverage audit respects the same compat manifest and still checks the live defining module.
- `patched-real-llama.log` and `patched-real-llama-20260915/runtime-receipt.json`: **complete=true**, real model on isolated runtime, two agent API calls, terminal tool start/finish with `echo LOCAL_LLAMA_PROOF`, unload/reload, stop, **runtime_closed=true**. Used isolated Python 3.12.14 / SQLite 3.53.1 and exact cryptography 50.0.0 pin; pip check passed. No operator service was stopped.
- A disposable venv-upgrade trial demonstrated that Python 3.12.14's `venv --upgrade --without-pip` changes an existing 3.12.5 venv to SQLite 3.53.1 while preserving an installed site-packages witness. This is feasibility evidence, not proof that the live venv has been upgraded. Its package inventory, backup, dependency refresh and health verification remain required during the approved maintenance window.

## CI and delivery

PR #1 has been pushed to the final candidate, replacing the a27 run. The a27 run's compatibility-pointer failure is repaired and verified locally; the source-policy decision is also implemented. Await the exact-head CI result and address any actual failures before main delivery. Do not use the pending status from stage 20 as the current policy status.

Remaining work: exact-head CI; preserve/check all worktrees again; stage a permanent patched interpreter and backup the live venv; perform the previously authorized brief managed-Hermes-only maintenance; fast-forward main and push without force; verify current runtime receipts, unchanged config hashes and local/origin main equality. Launcher main is already delivered and must not be restarted. Installer remains design-only with the existing ownership/receipt/revision/lease contract and the runtime qualification addendum from stage 20.
