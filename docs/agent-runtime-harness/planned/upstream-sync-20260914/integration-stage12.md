# Checkpoint 12 — actual Launcher updater and integration regression repairs

## Delivery state

Hermes local main and origin/main remain `34ad8ba33f2508ab10bb24a26f0377ddb62660cb`.
The history-preserving candidate is pushed on `codex/updater-history-safety-20260915`
(latest completed checkpoint `9473a79daf`; additional regression work continues).
Draft PR: https://github.com/nekwo/hermes-agent/pull/1 . This candidate is **not ready
to land**: broad regression and mutation checks remain unresolved. Earlier statements
that candidate verification was complete are superseded by this checkpoint.

The actual user-facing updater is the Eternia Launcher Flutter button. Stage 11's
desktop handoff work covered Hermes's separate Electron desktop. Both paths now
have protections, but they must not be described as the same updater.

Launcher local main and origin/main contain `084a169fd802b80e2a0789a398f500f010bba44b`:
the controller checks fast-forward eligibility before touching the runtime supervisor
or stopping the service. Divergence/local-only commits refuse with a preservation
message. The panel no longer recommends reset/discard as recovery. Tests exercise
the real controller and make touching the runtime on a divergent checkout fail.
Verification: 37 tests passed; focused Flutter analysis found no issues. The task
worktree was removed normally after landing. The primary's 65 unrelated dirty paths
were preserved, as were the other registered worktrees. No app was rebuilt/restarted.

## Candidate repairs and evidence

- `731dbf967a`: canonical owners after upstream module moves (MCP registration,
  credential storage, terminal lifecycle, prompt building, bytecode sweeping,
  downstream postinstall, pet encoding); package `agent_runtime` in installed builds;
  retain upstream package pins; fix renderer test typing.
- `3a964f744c`: mutation anchors, UTF-8 Git output decoding, nine verified contributor
  mappings, and documentation separating Electron from Eternia Launcher.
- `585d8628ee`: Python 3.11 model scanning without `Path.is_junction`, preserving
  symlink/reparse exclusion; retain fork `result` notification default; redact short
  explicit JSON secret fields while preserving sibling arguments; scoped fault
  injection rather than shared-fixture undo; restored two upstream completion-test
  helpers and explicitly opted those tests into agent-turn delivery.
- `9473a79daf`: provider consent reads its canonical discovery module without loading
  the full plugin manager at CLI import; boot-capture test follows its moved owner.

Canonical test wrapper results (batches overlap; do not sum as unique tests):

| Log under ignored `qa-artifacts/` | Result |
| --- | --- |
| `compat-regression-final.log` | 73 passed |
| `updater-packaging-final.log` | 9 passed |
| `history-credentials-repair.log` | 106 passed |
| `completion-isolation-final.log` | 81 passed |
| `provider-import-repair.log` | 24 passed |
| `linux-compat-final.log` | 70 passed, 2 macOS skips |
| `wsl-updater-node-proof.log` | updater files 57 passed; separate gateway files initially 38 failed, then repaired in the Windows completion batch |
| `original-baseline-cache-timing.log` | original main: 69 passed on Linux |
| `linux-candidate-cache-timing.log` | candidate: 64 passed, 5 failed; used to distinguish changed behavior from baseline |

The 47 updater tests skipped in stage 11 were blocked by the Node build prerequisite,
not inherently by Windows. Isolated Linux Node 24.19.0 enabled all 47; combined with
10 real-Git history tests they passed. Linux proof lives under
`/var/tmp/hermes-sync-proof-20260915` with private Python 3.11/uv/cache/venv and a
separate baseline checkout. It does not use the operator runtime. Windows tests use
the integration worktree's isolated venv; aiohttp 3.14.3 was added there for gateway
tests. No runtime environment/configuration was modified.

GitHub run 34996615294 verified lint, Windows compatibility, lockfile, installer,
macOS and other checks. Nix run 34996613811 passed. JS renderer type checking and
its affected dot test passed locally. Full Python, mutation, attribution and review
gates must be rechecked on the eventual final head; green checks on older heads do
not certify later edits. The earlier full Python job reached its 30-minute timeout.

## Remaining work and credit conflict

Continue runtime invariant failures, cache fixture changes, mutation baseline claims,
and source/contract comparisons. Do not blindly relax a gate or remove a fork tool
to match upstream counts. Upstream made read-only SessionDB opens avoid redundant
writes; old tests requiring WAL churn are being updated to preserve the cache
invariants without requiring the retired write behavior.

Attribution revealed a tenth conflict on Linux: `agent@Agents-Mac-mini.local` is
verified as @skip-agent through original PR 88052, commit
`aa500613f8a33178fad2c61a95a582f240aa7a37`, and salvage PR 88126. Existing mapping
`agent@agents-Mac-mini.local` credits @momomojo. These collide on Windows but Git
treats them as distinct. Do not overwrite the existing contributor or invent a
mapping. A representation that preserves both identities is still required.

The CI workflow requires a maintainer's `ci-reviewed` label for sensitive workflow
and dependency changes. It has not been self-applied. Prepare a final review packet
before seeking that decision.

## Launcher contract and installer handoff

The current parser differs only by additive `profile create --clone-channels`.
194 command paths remain. Candidate fixture SHA-256 is
`4b3a970bad9c9f288efb77db37d716980ebb08c3275a399e60db6dfec0eec32e`.
Launcher companion `codex/hermes-upstream-contract-20260915` at `534a3f7f4` is
pushed with the matching fixture and sync note; 150 conformance tests passed.
Keep its delivery coordinated with the Hermes candidate. No installer was implemented.
The earlier installer handoff remains applicable: upstream bootstrap/model primitives
do not replace the fork's Local llama ownership, revision, receipt and active-turn
contracts. Reverify those contracts on the final candidate before Launcher implements.

The original 8-to-3 Local llama review series and exact tracked-tree equivalence
remain separate from published-main delivery. No published commit was folded away.
Recovery branches remain pushed. Updating the live editable Hermes checkout still
requires a safe maintenance decision, because the running process imports primary
source; the user's no-restart/no-runtime-change constraint has not been overridden.
