# Checkpoint 13: restored downstream seams and refreshed contracts

Supersedes checkpoint 12's unresolved contributor representation and regression inventory.
Hermes is still a review candidate, not delivered on main. Published ancestry remains intact.

## Recovery heads

- Hermes local main and origin/main: `34ad8ba33f2508ab10bb24a26f0377ddb62660cb`.
- Pinned upstream: `110baa095bc7135a0624557a9cc35df0f98ece0f` (no newer upstream added during repairs).
- Pushed candidate `codex/updater-history-safety-20260915`: `c7871382b9b798c249cd420d9842c94d0eb0f6bb`.
- Launcher local main and origin/main: `084a169fd802b80e2a0789a398f500f010bba44b`; updater refusal-before-stop is landed, no live rebuild/restart.
- Pushed Launcher contract companion `codex/hermes-upstream-contract-20260915`: `241391dd66a55d4375fee81609c9195d8240c1b7` (not landed).
- Original backup refs and 8-to-3 review mapping/tree-equivalence evidence are unchanged. The separate review series is not a replacement main ancestry.

## Repair commits

| Commit | Scope |
|---|---|
| e7decd9359 | Restore profile rebind argument ownership and boot resolution receipts; use profile-qualified MCP connection keys; wake parked servers through canonical loop owner; move gateway-only mixin back to gateway; keep plugin discovery read-only; preserve cache and persistence contracts across upstream moves. |
| ada935be38 | Static tool scanner handles literal registration tables and package registrars without executing handlers; regenerate inventory; exclude two new kanban tools on native lanes. |
| a72caf5845 | Represent both case-sensitive contributor identities using a separate case-variants directory; update release/audit/CI readers without changing authorship or overwriting the existing credit. |
| 4c626d8332 | Add upstream profile create --clone-channels to the producer CLI fixture. |
| 46ac2b7224 | Update regression probes to canonical skill/MCP/terminal owners; distinguish independent upstream contracts; select explicit autonomous completion mode in the isolated backlog probe. |
| 5bd358e923 | Advertise all nine Local llama RPC methods with exact access tiers in transport tests; regenerate additive realm response fixtures; document prospective Discord thread identity. |
| 133dda1152 | Preserve bounded persona exclusion after cronjob became cronjob_manage; unbounded opt-in still resolves the tool. |
| b887f2efd7 | Follow session-persistence row construction; honor the existing gateway-lookalike test marker while retaining other backend refusals. |
| c7871382b9 | Move real dummy gateway ownership tests into the gateway test area; retain the root live-system guard. Move mutation targets onto upstream's shared plugin loader. |

The fork's original profile rebind exclusion and HERMES_PROFILE_RESOLUTION receipt were missing from the merge and are restored without replacing upstream's supervisor/SSH profile protections. Upstream read-only SQLite opens intentionally no longer write WAL frames; fixtures now explicitly establish their own checkpoint and test cache invalidation without requiring those retired writes. Timing attribution probes use controlled clocks to avoid unrelated discovery latency masking attribution.

Attribution: the verified `agent@Agents-Mac-mini.local` / @skip-agent record now lives under `contributors/emails/case-variants/`; `agent@agents-Mac-mini.local` continues crediting @momomojo. CI contributor and case-collision jobs passed on 4c626d8332. Git author identities remain untouched.

## Exact proof and gaps

All Python regression runs below use `scripts/run_tests.sh`, with per-file isolated homes. Windows uses the private integration-worktree venv; Linux uses the private /var/tmp proof checkout and Python 3.11. Operator runtime configuration was not changed.

| Log under ignored qa-artifacts | Result |
|---|---|
| profile-credit-scanner-repair.log | 89 passed, 1 platform skip: contributor map, profile override, CLI entrypoint, boot receipt, static scan, manifest. |
| tool-manifest-final.log | 39 passed: static scan, live registry parity, native inventory ratchet. |
| skills-rpc-contract-final.log | 106 passed: skill resolution, active-profile skill roots, office RPC on both transports. |
| rpc-removal-contract-repair.log | 235 passed, 4 failed on missing Local llama tier fixture entries; those four pass in the subsequent skills/RPC run. Includes 15 projection-removal, 22 version-authority, 65 terminal envelope, 82 subscribe, 5 relay-doc tests. |
| completion-backlog-explicit-mode.log | 2 passed: actual shell child completions, loopback turn sink, all CLI/poller/post-turn cases including foreign ownership and consumed events. |
| llama-wire-gateway-recheck.log | 127 passed, 1 response fixture failure; all 48 Local llama and 52 gateway completion tests passed. |
| response-permission-final.log | Response producer fixture 18 passed; initial new permission test used the wrong lane setup. |
| renamed-cron-permission-final.log | All 19 tool visibility tests passed, including bounded exclusion/unbounded inclusion of renamed scheduling tool. |
| launcher-response-fixture-sync.log | 46 Flutter tests passed across response/frame consumers and manifest text checks. Prior CLI companion run: 150 passed. |
| linux-original-ci-failures-recheck.log | 46 formerly failing files: 1,044 passed, 5 failed in 63.7 seconds. Four failures are dummy gateway spawn fences, one is moved persistence-source check. |
| relay-guard-selected-final.log | Windows selected repair witnesses: 2 passed. An earlier whole guard-file Windows run disappeared without a completion log; not counted as passing. |
| linux-relay-guard-final.log | Follow-up exposed the additional CLI-specific spawn fence and one existing status-shaped fake argv needing the explicit lookalike marker. Both addressed in c7871382b9; final recheck pending when this note was written. |
| final-real-llama.log | First attempt failed during the executable --help inspection's 10-second timeout, before model load. A separate inspection completed successfully in 1.5 seconds. |
| final-real-llama-retry.log | Exit 0, complete receipt: config/scan/start/load, production runner text route, auxiliary local route, full agent with 2 API calls and tool events, unload/reload at changed context, stop and runtime_closed=true. Only its owned isolated runtime was stopped. |

Real-probe receipt: `qa-artifacts/upstream-sync-final-llama-retry-20260915/runtime-receipt.json`. Initial failure and successful retry are both retained.

CI on 133dda1152 killed 19 selected mutants but two loader mutants survived because the memory loader now delegates to `plugins/plugin_loader.py`; the old fork helpers were no longer on that path. Claims now target the actual publish and failed-exec cleanup sites. The failed-exec claim is renamed from sh-loader-rollback-leaves-the-parent-attribute to sh-loader-failed-exec-leaves-module-published to reflect upstream's publish-after-success design. A fresh actual mutation run is required; listing candidates alone is not proof. Full CI Python, Windows and JS checks remain in progress/unverified, and the ci-reviewed human label remains unapplied.

## Installer and delivery handoff

Contract companion mirrors the exact producer bytes: CLI adds --clone-channels, realm sync adds levels and workspace-delta skill counts. No installer code was implemented. Continue to use upstream local_runtime bootstrap primitives while preserving the fork's Local llama manager as sole lifecycle/ownership/revision/receipt authority. Install, validate and activate remain separate operations; active-turn exclusion and exact loaded-model routing are mandatory.

Do not squash-merge the integration candidate: it contains both original published main and pinned upstream as ancestors and is intended for history-preserving fast-forward delivery. The review consolidation stays separate. Before final delivery, finish remaining regression/mutation/CI checks, merge the audit notes, and obtain the concrete maintenance decision needed for a primary checkout imported by a live editable service. The user's no-restart constraint still applies. Do not force main or silently change delivery rules.
