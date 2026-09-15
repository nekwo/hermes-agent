# Planned — a runner for the live-log observability consumers

**Status: proposed, unbuilt.** Nothing in this file describes shipped code.
The receipts it concerns are live and censused in
[`../07-observability.md`](../07-observability.md); what is missing is anything
that runs their consumers on a schedule.

## The gap

Three of the four consumers of this runtime's live-log receipts execute only
when a human remembers to type the command.

| consumer | what it reads | runner today |
|---|---|---|
| `tool/mission_chat_latency_audit.dart` (launcher) | `[MissionChatTiming]` in the diag log joined to hermes turn records by `turn_id` | none — `dart run` by hand |
| `tool/mission_boot_receipt_audit.dart` (launcher) | `[MissionBoot]` receipts in the diag log | none — `dart run` by hand |
| `scripts/core_cache_demote_census.py` (hermes) | `snapshot_core_cache … core_source=rebuilt` in `agent.log` | none — `python` by hand |
| `tool/test_quality/check_producer_contracts.py` (launcher) | committed fixture bytes, both repos | **CI**, `hermes-cli-contract` job (`.github/workflows/ci.yml:42`, step `:82-85`) |

## Evidence

* Repo-wide grep over `EterniaLauncher/.github`, `EterniaLauncher/.githooks`,
  `hermes-agent/.github` and `hermes-agent/scripts/run_tests.sh` for
  `mission_chat_latency_audit`, `mission_boot_receipt_audit` and
  `core_cache_demote_census`: **zero hits.** The only fixture/contract consumer
  wired anywhere is `check_producer_contracts.py`.
* `tool/mission_boot_receipt_audit.dart:18-24` states the reason plainly and it
  is a correct reason, not an oversight: the tool reads MACHINE STATE
  (`%TEMP%\eternia_launcher_diag.log`), "no CI job has ever observed a live boot
  (there is no hermes install in CI), so wiring this into a suite would only
  ever assert over a file that does not exist. It belongs to the QA lane: run it
  after any live boot."
* That reasoning rules out CI. It does not rule out a local runner, and the
  campaign's own history is the argument for one: the boot receipts were honest
  and release-visible for days while nothing read them, which is how the
  2026-08-21 convergence defect survived (`mission_boot_receipt_audit.dart:5-16`).

## Shape

One command in the QA lane that runs all three against the operator's live
machine state and reports a combined verdict. Read-only by construction — every
one of the three already writes nothing and is safe to run against a live
serve.

The exit-code semantics must be preserved, not flattened. Each tool
deliberately distinguishes "nothing to measure" from "measured and broken"
(exit 3 vs 2 on the boot audit; 3 vs 4 vs 2 on the chat audit; 1 vs 2 on the
census), and a wrapper that collapsed them into pass/fail would destroy the
distinction the tools exist to draw. The wrapper reports each tool's own code.

Open question for the operator, not settled here: whether an empty scan should
fail the wrapper. On a machine that has taken no turn since the last build,
exit 3 is the honest and expected answer — so the wrapper's own verdict
probably needs a "nothing to measure" state distinct from both pass and fail.

## Gate

This is worth building when, and only when, one of these holds:

* a receipt-visible defect reaches the operator a second time after its
  consumer already existed and was not run; or
* the QA lane acquires a post-live-session step that this can hang off.

Until then the honest state is: the consumers exist, they fail loudly when run,
and running them is a human act. That is recorded in `07-observability.md`
under Open rows.
