# Root Node N3 Burn-In Ledger

Date: 2026-07-03/2026-07-04

Scope: doc-08/doc-09 N3 burn-in only. `root_node_mode` was enabled only for the live burn-in window, then restored to `false`. This pass did not flip the default flag and did not delete the legacy tower.

Final status after the burn-in window:

```text
open_tasks=0 running_runs=0 open_incidents=0 dirty=clean runtime_health=True
root_node_mode=false
```

## Ledger

| Row | Goal shape | Task id | Duration | Unattended | Outcome | Evidence |
| --- | --- | --- | ---: | --- | --- | --- |
| 01 | Single hermes-agent root-mode pytest | `task_d19640b0` | 61s | yes | done, archived `20260703T125902073111Z_archive_ready` | `proof_observed_0fd2f1ab67`; 2 runs, 0 incidents |
| 02 | Single hermes-agent QA-node pytest | `task_d93b8811` | 74s | yes | done, archived `20260703T130314484711Z_archive_ready` | `proof_observed_0f1f85fe2b`; 2 runs, 0 incidents |
| 03 | Single hermes-agent root-authoring pytest | `task_deea0cf7` | 53s | yes | done, archived `20260703T130434966920Z_archive_ready` | `proof_observed_6b96091fe3`; 2 runs, 0 incidents |
| 04 | QA-in-graph with Mission Control screenshot | `task_7eaca4e5` | 164s | yes | done, archived `20260703T130730676382Z_archive_ready` | `proof_observed_32ac5260c3`, `proof_observed_bc66d4773c`, `screenshot_trace_task_7eaca4e5_n3_burnin_04_qa_pytest_mission_control_screenshot_bcbd8834`; 3 runs, 0 incidents |
| 05 | Cross-stack `depends_on` no-edit status stages | `task_b0cf1fe2` | 62s | yes | done, archived `20260703T130857950879Z_archive_ready` | 3 runs, 0 incidents; no Proof rows because `git status --short` is not captured by the self-test proof recorder |
| 06 | Single EterniaLauncher no-edit status stage | `task_1268f482` | 41s | yes | done, archived `20260703T131020105543Z_archive_ready` | 2 runs, 0 incidents; no Proof rows because `git status --short` is not captured by the self-test proof recorder |
| 07 | Single EterniaBackend no-edit status stage | `task_dbfa47e8` | 37s | yes | done, archived `20260703T131122757181Z_archive_ready` | 2 runs, 0 incidents; no Proof rows because `git status --short` is not captured by the self-test proof recorder |
| 08 | Cross-stack dependency evidence | `task_585c465a` | 37070s | no | done after targeted restart, archived `20260703T232925567381Z_archive_ready` | 6 runs, 0 incidents; daemon was killed by an interrupted shell and later restarted with `--task task_585c465a`, which reaped two orphan runs |
| 09 | QA-in-graph forced steer round trip | `task_fc937bb8` | 273s | yes | done, archived `20260703T233521336974Z_archive_ready` | `proof_observed_2b1fa7b187`, `proof_observed_74b115d844`, `proof_observed_761ae87a22`, `screenshot_trace_task_fc937bb8_n3_burnin_09_qa_roundtrip_6eefd054`; 4 runs, 0 incidents |
| 10 | Chaos drill: daemon stop mid-turn, targeted restart | `task_d75547fd` | 119s | no | done after intentional stop/restart, archived `20260703T233750107570Z_archive_ready` | `proof_observed_6f28c73002`; `daemon stop` cancelled `run_787754bdd093` and `run_eba710c2ac01`, targeted restart completed the task; 0 incidents |

## Notes

- Rows 01-07 and 09 had no manual ticks, task unblocks, or incident closes.
- Row 08 is explicitly not an unattended success because the operator interrupted the controlling shell, leaving the targeted daemon dead until manual restart.
- Row 10 is the required chaos drill. It intentionally used `harness daemon stop` and targeted restart, so it is not counted as unattended even though it reached `done` after restart.
- Rows 05-07 proved root authoring and cross-repo routing to terminal completion, but their chosen `git` status commands did not produce `Proof` rows. The verifier should treat that as a burn-in evidence-strength gap, not as hidden evidence.
- Result: N3 burn-in ledger is complete, but it is not a 10/10 unattended green ledger. The default flag must remain `false`, and legacy-tower deletion remains blocked pending independent verification and a stronger clean burn-in sweep.
